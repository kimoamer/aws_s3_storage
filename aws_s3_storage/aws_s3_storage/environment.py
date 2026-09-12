# Copyright (c) 2026, Innomate LLC
# For license information, please see license.txt
"""Which environment owns the bucket, and what a *copy* of this database may do to it.

The problem this solves
-----------------------
Restore a production database onto a test site and every ``File`` record comes
with it: the same S3 keys, the same bucket name, the same credentials. Nothing in
the application can tell the two sites apart, so the test site is not working on
a copy of the files — it is working on *the files*. Deleting an attachment there,
flipping it private, or letting a scheduled job run, all reach into production's
bucket. Worse, the "is this object still used?" check only ever looks at the
database it is running in, so the test site can conclude a deletion is safe while
production is still serving the attachment.

A checkbox cannot fix this. Any flag stored in the database is copied along with
the database, so the marker that says "this is production" arrives on the test
site saying exactly the same thing.

How ownership is decided
------------------------
Ownership is a match between two values that live in *different places*:

* ``storage_owner_id`` in **S3 Settings** — inside the database, so it travels
  with a restore.
* the same id under ``aws_s3_storage_owner`` in **site_config.json** — a file on
  disk. ``bench backup`` does not put it in the dump and ``bench restore`` does
  not write it, so it stays behind.

They match only on the site that claimed the bucket:

===========================================  ==================  ===============
Situation                                    Database / on disk  State
===========================================  ==================  ===============
Fresh install                                none / none         ``unclaimed``
The site that claimed the bucket             ``X`` / ``X``       ``owner``
Production's database restored onto test     ``X`` / none        ``foreign``
Restored over a site that claimed its own    ``X`` / ``Y``       ``foreign``
Production restored onto itself              ``X`` / ``X``       ``owner``
Whole site directory copied (config too)     ``X`` / ``X``       ``foreign`` [*]
===========================================  ==================  ===============

[*] the claim also records the site name, so a copy that carries site_config.json
    along is still caught by the name it now answers to.

Claiming happens in ``after_install`` / ``after_migrate`` and **only from the
unclaimed state** — a restored database already carries an owner id, so running
``bench migrate`` on the test site can never make it the owner by accident.

What a foreign environment may do
---------------------------------
Reads are never blocked: a test site can still open production's attachments
(what it is actually allowed to *fetch* is up to the IAM policy behind the
credentials, which is the layer this code cannot enforce — see the README).

Everything that modifies the bucket is refused, and the refusal is reported
rather than swallowed:

* new uploads fall back to the site's own local disk, so the test site keeps
  working and its files are its own;
* deletions drop the ``File`` record but leave the object alone;
* privacy moves, scope moves, thumbnails, the deletion queue, backup sync,
  and the migration jobs all stand down.

Give the test site its own bucket and claim it (``claim_storage``) and it is a
normal owner again — of that bucket.

Per-file ownership
------------------
``File.s3_owner`` records which environment uploaded each object, which is the
second line of defence for the one case the environment check cannot cover: an
environment that has *deliberately* adopted a bucket that already holds another
environment's files. An empty ``s3_owner`` means "unknown", and the owner may
touch it — that is every file uploaded before this field existed.
"""

import uuid

import frappe
from frappe.utils import cint, now_datetime

# Key under which the owner id is stored in site_config.json. Deliberately *not*
# in the database: that is the whole point (see the module docstring).
OWNER_CONFIG_KEY = "aws_s3_storage_owner"

OWNER = "owner"
UNCLAIMED = "unclaimed"
FOREIGN = "foreign"

# Inherited-file policy values (S3 Settings -> inherited_storage_policy).
POLICY_READ_ONLY = "Read Only"
POLICY_FULL_ACCESS = "Full Access"


def _settings(settings=None):
	return settings if settings is not None else frappe.get_single("S3 Settings")


def current_site():
	return getattr(frappe.local, "site", None) or ""


def local_owner_id():
	"""The owner id recorded on this server's disk (site_config.json)."""
	try:
		return (frappe.conf.get(OWNER_CONFIG_KEY) or "").strip()
	except Exception:
		return ""


def recorded_owner_id(settings=None):
	"""The owner id recorded in the database (and therefore copied with it)."""
	return (_settings(settings).get("storage_owner_id") or "").strip()


def ownership(settings=None):
	"""Decide whether this environment owns the configured bucket.

	Returns a dict with ``state`` (owner / unclaimed / foreign) and, when the
	answer is "foreign", a ``reason`` that says which of the checks disagreed so
	an administrator can see *why* the site was locked out.
	"""
	settings = _settings(settings)
	db_id = recorded_owner_id(settings)
	disk_id = local_owner_id()
	owner_site = (settings.get("storage_owner_site") or "").strip()
	site = current_site()

	info = frappe._dict(
		state=UNCLAIMED,
		reason=None,
		owner_id=db_id,
		owner_site=owner_site,
		local_owner_id=disk_id,
		site=site,
	)

	if not db_id:
		# Nobody has claimed the bucket yet: a fresh install, or an install from
		# before this field existed. Nothing here proves the database was moved.
		info.reason = "No environment has claimed this storage yet."
		return info

	if not disk_id:
		info.state = FOREIGN
		info.reason = (
			f"This database is owned by environment '{db_id}'"
			+ (f" (site '{owner_site}')" if owner_site else "")
			+ ", but this server holds no ownership token on disk — "
			"the database was restored here from another site."
		)
		return info

	if disk_id != db_id:
		info.state = FOREIGN
		info.reason = (
			f"This server owns storage '{disk_id}', but the restored database is "
			f"owned by '{db_id}'. The database and the server belong to different environments."
		)
		return info

	if owner_site and site and owner_site != site:
		# The ids match, so site_config.json came along too — a copy of the whole
		# site directory, not just the database. The site it now answers to is the
		# only thing left that tells the copy apart from the original.
		info.state = FOREIGN
		info.reason = (
			f"Storage '{db_id}' was claimed by site '{owner_site}' but this site is "
			f"'{site}'. The site directory looks like a copy."
		)
		return info

	info.state = OWNER
	return info


def state(settings=None):
	return ownership(settings).state


def is_owner(settings=None):
	return ownership(settings).state == OWNER


def is_foreign(settings=None):
	return ownership(settings).state == FOREIGN


# ---------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------


def claim(settings=None, force=False, adopt_existing_files=False):
	"""Record this environment as the owner of the configured bucket.

	Writes a new id to both places that have to agree. Refuses to overwrite an
	existing claim unless ``force`` — otherwise a ``bench migrate`` on a restored
	database would quietly make the test site the owner, which is the one thing
	this module exists to prevent.
	"""
	settings = _settings(settings)
	info = ownership(settings)

	if info.state == OWNER and not force:
		return info

	if info.state == FOREIGN and not force:
		frappe.throw(
			"This environment does not own the configured storage. "
			+ (info.reason or "")
			+ " Use 'Take Ownership of This Storage' if that is intended."
		)

	owner_id = uuid.uuid4().hex
	_write_local_owner_id(owner_id)

	frappe.db.set_single_value("S3 Settings", "storage_owner_id", owner_id)
	frappe.db.set_single_value("S3 Settings", "storage_owner_site", current_site())
	frappe.db.set_single_value("S3 Settings", "storage_owner_claimed_at", now_datetime())
	frappe.clear_document_cache("S3 Settings", "S3 Settings")

	if adopt_existing_files:
		adopt_untagged_files(owner_id)

	frappe.logger().info(f"aws_s3_storage: storage claimed by {owner_id} (site {current_site()})")
	return ownership()


def _write_local_owner_id(owner_id):
	from frappe.installer import update_site_config

	update_site_config(OWNER_CONFIG_KEY, owner_id)
	# update_site_config refreshes frappe.conf in this process, but be explicit so a
	# subsequent ownership() call in the same request cannot read a stale value.
	try:
		frappe.conf[OWNER_CONFIG_KEY] = owner_id
	except Exception:
		pass


def adopt_untagged_files(owner_id=None):
	"""Tag every S3-backed File that has no recorded owner as owned by this one.

	Files uploaded before ``s3_owner`` existed carry no owner, which the guard
	reads as "unknown" and lets the owning environment modify. Stamping them makes
	that explicit, so a *different* environment that later adopts the same bucket
	is still kept away from them.
	"""
	owner_id = owner_id or recorded_owner_id()
	if not owner_id or not frappe.db.has_column("File", "s3_owner"):
		return 0

	count = frappe.db.count("File", {"s3_key": ["not in", ["", None]], "s3_owner": ["in", ["", None]]})
	if count:
		frappe.db.sql(
			"""
			UPDATE `tabFile`
			SET `s3_owner` = %(owner)s
			WHERE (`s3_key` IS NOT NULL AND `s3_key` != '')
			  AND (`s3_owner` IS NULL OR `s3_owner` = '')
			""",
			{"owner": owner_id},
		)
	return count


def claim_if_unclaimed():
	"""Called from after_install / after_migrate.

	Only ever claims from the unclaimed state, so running ``bench migrate`` on a
	restored copy of another site's database never transfers ownership.

	One ordering matters while upgrading from a version that predates this field:
	a database from that version carries *no* owner id, so it is unclaimed
	wherever it lands, and the first site to run this version claims the bucket.
	Upgrade the live site first. Once it has claimed, every later copy of its
	database is detected.
	"""
	try:
		if not frappe.db.exists("DocType", "S3 Settings"):
			return
		info = ownership()
		if info.state != UNCLAIMED:
			if info.state == FOREIGN:
				frappe.logger().warning(f"aws_s3_storage: running on a foreign environment — {info.reason}")
				print(f"aws_s3_storage: storage is owned by another environment — {info.reason}")
				print("aws_s3_storage: S3 objects will not be modified from this site.")
			return
		claim()
	except Exception as e:
		# Never fail an install or a migrate over this.
		frappe.logger().error(f"aws_s3_storage: could not claim storage ownership: {e}")


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


def read_only_mode(settings=None):
	return bool(cint(_settings(settings).get("read_only_mode")))


def may_modify_storage(settings=None):
	"""True when this environment is allowed to change anything in the bucket.

	The single question every mutating code path asks. Reads never go through it.
	"""
	settings = _settings(settings)
	if read_only_mode(settings):
		return False
	return ownership(settings).state != FOREIGN


def may_modify_object(file_doc=None, settings=None, owner=None):
	"""True when this environment may change *this particular* object.

	On top of the environment check, an object explicitly stamped with another
	environment's id is left alone — the case that matters once an environment has
	deliberately adopted a bucket that already held someone else's files. An
	object with no recorded owner counts as this environment's.
	"""
	settings = _settings(settings)
	if not may_modify_storage(settings):
		return False

	if (settings.get("inherited_storage_policy") or POLICY_READ_ONLY) == POLICY_FULL_ACCESS:
		return True

	if owner is None and file_doc is not None:
		try:
			owner = file_doc.get("s3_owner")
		except Exception:
			owner = None

	owner = (owner or "").strip()
	if not owner:
		return True

	return owner == recorded_owner_id(settings)


def owner_stamp(settings=None):
	"""The value to write into ``File.s3_owner`` for a new upload."""
	return recorded_owner_id(settings) or None


def blocked_reason(settings=None):
	"""Why a mutation was refused, for logs and messages."""
	settings = _settings(settings)
	if read_only_mode(settings):
		return "S3 Read-Only Mode is on: this site never modifies the bucket."
	info = ownership(settings)
	if info.state == FOREIGN:
		return info.reason
	return "This object belongs to another environment."


def report_blocked(operation, detail=None, settings=None):
	"""Log (and, in an interactive request, show) a refused S3 modification.

	Refusals are never silent: an administrator who deletes an attachment on a
	restored copy should be able to find out why the object is still in the bucket.
	"""
	reason = blocked_reason(settings)
	message = f"aws_s3_storage: refused to {operation} — {reason}"
	if detail:
		message = f"{message} ({detail})"
	frappe.logger().warning(message)

	try:
		if getattr(frappe.local, "request", None) and frappe.session.user != "Guest":
			frappe.msgprint(
				f"S3 storage was not modified: {reason}",
				title="S3 storage is protected",
				indicator="orange",
				alert=True,
			)
	except Exception:
		pass


def guard(operation, file_doc=None, settings=None, owner=None):
	"""``True`` when the caller may proceed; ``False`` (reported) when it may not."""
	if may_modify_object(file_doc=file_doc, settings=settings, owner=owner):
		return True
	report_blocked(operation, settings=settings)
	return False


# ---------------------------------------------------------------------------
# Admin UI
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_environment_status():
	"""Everything the S3 Settings form needs to explain the current state."""
	frappe.only_for("System Manager")
	settings = frappe.get_single("S3 Settings")
	info = ownership(settings)

	status = {
		"state": info.state,
		"reason": info.reason,
		"owner_id": info.owner_id,
		"owner_site": info.owner_site,
		"site": info.site,
		"has_local_token": bool(info.local_owner_id),
		"read_only_mode": read_only_mode(settings),
		"may_modify_storage": may_modify_storage(settings),
		"inherited_storage_policy": settings.get("inherited_storage_policy") or POLICY_READ_ONLY,
		"bucket": settings.get("bucket_name"),
	}

	if frappe.db.has_column("File", "s3_owner"):
		if info.owner_id:
			status["files_owned"] = frappe.db.count(
				"File", {"s3_key": ["not in", ["", None]], "s3_owner": info.owner_id}
			)
		status["files_untagged"] = frappe.db.count(
			"File", {"s3_key": ["not in", ["", None]], "s3_owner": ["in", ["", None]]}
		)
	status["blocked_deletions"] = frappe.db.count("S3 Deletion Queue", {"status": "Blocked"})
	status["pending_deletions"] = frappe.db.count("S3 Deletion Queue", {"status": "Pending"})
	return status


@frappe.whitelist()
def claim_storage(adopt_existing_files=0):
	"""Take ownership of the configured bucket from this environment (System Manager).

	Deliberate and explicit: this is what a genuinely new production site does
	after a move, and what a test site does *after* pointing itself at its own
	bucket. Taking ownership of a bucket that still holds another environment's
	live files is exactly the mistake this app is trying to prevent, so the button
	behind it warns first.
	"""
	frappe.only_for("System Manager")
	info = claim(force=True, adopt_existing_files=cint(adopt_existing_files))
	frappe.db.commit()
	return {"state": info.state, "owner_id": info.owner_id, "site": info.site}


@frappe.whitelist()
def block_inherited_deletions():
	"""Park every queued deletion so nothing inherited from a backup is executed.

	The deletion queue is a normal doctype, so its rows are restored along with
	everything else; each one is an instruction to delete an object that another
	environment asked for, against a database this environment cannot vouch for.
	"""
	frappe.only_for("System Manager")
	count = frappe.db.count("S3 Deletion Queue", {"status": "Pending"})
	if count:
		frappe.db.set_value(
			"S3 Deletion Queue",
			{"status": "Pending"},
			{"status": "Blocked", "last_error": "Blocked: inherited from another environment."},
			update_modified=False,
		)
		frappe.db.commit()
	return {"blocked": count}
