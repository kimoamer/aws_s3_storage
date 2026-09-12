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

import hashlib
import json
import os
import socket
import uuid

import frappe
from frappe.utils import cint, get_datetime, now_datetime

# Key under which the owner id is stored in site_config.json. Deliberately *not*
# in the database: that is the whole point (see the module docstring).
OWNER_CONFIG_KEY = "aws_s3_storage_owner"

OWNER = "owner"
UNCLAIMED = "unclaimed"
FOREIGN = "foreign"

# Environment variable an operator can set per deployment — in the systemd unit,
# the container spec, the orchestrator — i.e. *outside* everything that a disk or
# directory copy carries with it. When set it is the instance identity outright.
# This is the only thing that distinguishes a bit-identical clone of a server
# (same machine id, same hostname, same site, same site_config.json) from the
# original; nothing derived from the machine can, because nothing on it differs.
INSTANCE_ENV_VAR = "AWS_S3_STORAGE_INSTANCE"

# The lease object: the one piece of ownership state that lives in the bucket
# itself, so two environments sharing a bucket can see each other. Outside
# SERVABLE_PREFIXES, so it is never reachable through the download endpoint.
LEASE_KEY = ".aws_s3_storage/owner.json"
_LEASE_CACHE_KEY = "aws_s3_storage:owner_lease"
# How long a lease check is trusted before going back to the bucket. This is
# also the bound on how long a server that has just lost ownership can still act
# on its old answer, so it is short: a destructive operation is already several
# S3 calls, one more is not what makes it slow.
LEASE_CHECK_INTERVAL = 60
# How often the holder re-stamps its own heartbeat while checking.
LEASE_HEARTBEAT_INTERVAL = 600
# A lease older than this is *reported* as stale in the UI so an administrator
# can judge whether the other server is really gone. Nothing is granted or
# withdrawn on the strength of it — see the note above _lease_state.
LEASE_STALE_AFTER = 3600

LEASE_OK = "ok"
LEASE_CONFLICT = "conflict"
LEASE_UNVERIFIED = "unverified"

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


def _machine_id():
	"""The host's own identity, as the OS records it.

	Regenerated by cloud-init / systemd-firstboot / Docker when a machine is
	provisioned from an image, so a properly built new server gets a new one. A
	raw block-level copy of a disk does not — see INSTANCE_ENV_VAR.
	"""
	for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
		try:
			with open(path) as f:
				value = f.read().strip()
			if value:
				return value
		except OSError:
			continue
	return ""


def instance_id():
	"""Identity of the *running server*, derived rather than stored.

	Deliberately not written into the database or the site directory: both are
	copied wholesale by the clone this is meant to detect. It is recomputed from
	the host each time, so a copy running on another machine answers differently.

	``AWS_S3_STORAGE_INSTANCE`` overrides it outright, which is what a deployment
	should set when the machine itself cannot be relied on to differ.
	"""
	explicit = (os.environ.get(INSTANCE_ENV_VAR) or "").strip()
	if explicit:
		return explicit[:64]

	raw = "|".join([_machine_id(), socket.gethostname() or "", current_site()])
	return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


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
# The lease in the bucket
# ---------------------------------------------------------------------------
# Everything above compares values that are *on this server*. That is enough for
# a database restored onto a different site, and not enough for a copy of the
# whole server that kept the site name: there the database id, the
# site_config.json id and the site name all agree, because all three were
# copied. Two environments then both believe they are the owner, and neither can
# see the other — because nothing they compare lives anywhere they share.
#
# The bucket is the thing they share. The lease is a small object in it naming
# the server that currently holds ownership. Before anything destructive, an
# environment checks that the lease still names it:
#
#   * lease names this instance      -> proceed
#   * lease names anything else      -> stand down, whatever its age
#   * lease missing / unreadable     -> stand down
#
# Taking a lease away from the server that holds it is ALWAYS an explicit human
# action (claim_storage). There is deliberately no rule by which one environment
# takes over from another on its own, and in particular no timeout:
#
#   the heartbeat is written when the lease is checked, and the lease is only
#   checked on the destructive path, so a perfectly healthy production site that
#   simply has not deleted anything for a while has an old heartbeat. A timeout
#   would read that as "production is gone" and hand the lease to whoever asked
#   next — which, in the scenario this whole module exists for, is the test copy.
#   An old heartbeat means "nobody has needed to delete anything", never "that
#   server is gone".
#
# The cost is that a server whose identity legitimately changes (a rename, a
# rebuilt container) needs someone to press the button. That is the right way
# round: a blocked delete is recoverable, a silent takeover is not. Deployments
# where the identity changes routinely should pin it with AWS_S3_STORAGE_INSTANCE.
#
# Writes to the lease are conditional (If-None-Match to create, If-Match to
# replace) so two servers racing cannot both believe they won.


def _lease_state(status, reason=None, holder=None, stale=False):
	return frappe._dict(status=status, reason=reason, holder=holder or {}, stale=stale)


def _read_lease(settings):
	"""``(lease, etag)``; ``(None, None)`` when the object does not exist yet.

	The ETag is what makes the next write conditional, so it travels with the
	content rather than being fetched again.
	"""
	from aws_s3_storage.aws_s3_storage import s3_utils

	s3 = s3_utils.get_s3_client()
	try:
		response = s3.get_object(Bucket=settings.bucket_name, Key=LEASE_KEY)
	except Exception as e:
		code = getattr(e, "response", {}).get("Error", {}).get("Code")
		if code in s3_utils._NOT_FOUND_CODES:
			return None, None
		raise

	etag = response.get("ETag")
	try:
		return json.loads(response["Body"].read().decode("utf-8")), etag
	except Exception:
		# Something wrote there that is not ours. That is not "no lease" — treat it
		# as a lease we do not hold, so we stand down rather than overwrite it.
		return {"owner_id": "", "instance": "", "site": "", "heartbeat_at": None}, etag


def _lease_payload(settings):
	return {
		"owner_id": recorded_owner_id(settings),
		"instance": instance_id(),
		"site": current_site(),
		"host": socket.gethostname() or "",
		"heartbeat_at": now_datetime().isoformat(),
	}


def _put_lease(settings, payload, if_match=None, if_none_match=None):
	"""Write the lease. Returns ``(written, failure)``.

	``failure`` is "precondition" when somebody else got there first, or
	"unconditional" when the endpoint does not support conditional writes at all
	(older S3-compatible services) and the write was therefore not attempted.
	"""
	from botocore.exceptions import ClientError, ParamValidationError

	from aws_s3_storage.aws_s3_storage import s3_utils

	kwargs = {
		"Bucket": settings.bucket_name,
		"Key": LEASE_KEY,
		"Body": json.dumps(payload, indent=1).encode("utf-8"),
		"ContentType": "application/json",
	}
	if if_match:
		kwargs["IfMatch"] = if_match
	if if_none_match:
		kwargs["IfNoneMatch"] = if_none_match

	try:
		s3_utils.get_s3_client().put_object(**kwargs)
		return True, None
	except ParamValidationError:
		# botocore too old to send the condition. Refusing beats racing.
		return False, "unconditional"
	except ClientError as e:
		code = (e.response.get("Error", {}) or {}).get("Code")
		if code in ("PreconditionFailed", "ConditionalRequestConflict", "412"):
			return False, "precondition"
		if code in ("NotImplemented", "InvalidRequest", "InvalidArgument"):
			return False, "unconditional"
		raise


def _write_lease(settings, if_match=None, expect_absent=False):
	"""Take or refresh the lease, refusing to overwrite a change we did not see.

	Writing the lease is a write to the bucket like any other, so Read-Only Mode
	stops it here rather than at each call site.
	"""
	if read_only_mode(settings):
		raise LeaseReadOnly("Read-Only Mode is on: this site writes nothing to the bucket.")

	payload = _lease_payload(settings)
	written, failure = _put_lease(
		settings,
		payload,
		if_match=if_match,
		if_none_match="*" if expect_absent else None,
	)
	if written:
		return payload
	if failure == "precondition":
		raise LeasePreconditionFailed("another server wrote the ownership lease first")
	raise LeaseConditionUnsupported(
		"this endpoint does not support conditional writes, so the ownership lease "
		"cannot be taken safely (two servers could both believe they won)"
	)


class LeaseError(Exception):
	pass


class LeasePreconditionFailed(LeaseError):
	"""Somebody else wrote the lease first. Never a reason to write it anyway."""


class LeaseConditionUnsupported(LeaseError):
	"""The endpoint cannot do conditional writes, so no write was attempted."""


class LeaseReadOnly(LeaseError):
	"""Read-Only Mode: the bucket is not written to, the lease included."""


def _lease_age(lease):
	try:
		return (now_datetime() - get_datetime(lease.get("heartbeat_at"))).total_seconds()
	except Exception:
		return None


def check_lease(settings=None, refresh=False):
	"""Whether this server still holds the bucket's ownership lease.

	Cached for ``LEASE_CHECK_INTERVAL`` seconds, per bucket *and per server and
	recorded owner*: the cache is the site's Redis, shared by every worker and
	every machine running this site, so a decision made by one server must never
	be reused by another. The short window is also the bound on how long a server
	that has just lost ownership can keep acting on the old answer.
	"""
	settings = _settings(settings)
	if not settings.bucket_name:
		return _lease_state(LEASE_UNVERIFIED, "No bucket is configured.")

	cache_key = _lease_cache_key(settings)
	if not refresh:
		try:
			cached = frappe.cache().get_value(cache_key)
		except Exception:
			cached = None
		if cached:
			return _lease_state(
				cached.get("status"), cached.get("reason"), cached.get("holder"), cached.get("stale")
			)

	result = _evaluate_lease(settings)
	try:
		frappe.cache().set_value(cache_key, dict(result), expires_in_sec=LEASE_CHECK_INTERVAL)
	except Exception:
		pass
	return result


def _lease_cache_key(settings):
	"""Scoped so one server's answer can never be served to another.

	frappe.cache() is the site's Redis, which several application servers of the
	same site share. A key naming only the bucket would let a second server read
	the first one's "yes".
	"""
	scope = "|".join(
		[
			settings.bucket_name or "",
			(settings.get("endpoint_url") or "").strip(),
			recorded_owner_id(settings),
			instance_id(),
		]
	)
	return f"{_LEASE_CACHE_KEY}:{hashlib.sha256(scope.encode('utf-8')).hexdigest()[:24]}"


def _evaluate_lease(settings, depth=0):
	mine = instance_id()

	try:
		lease, etag = _read_lease(settings)
	except Exception as e:
		# Cannot see the bucket's lease -> cannot rule out a second live writer.
		# Destructive work waits; uploads are unaffected, and a failed delete is
		# queued rather than lost.
		return _lease_state(
			LEASE_UNVERIFIED,
			f"Could not read the ownership lease from the bucket ({e.__class__.__name__}). "
			f"Grant the IAM user s3:GetObject and s3:PutObject on '{LEASE_KEY}'.",
		)

	if lease is None:
		# Never create the lease from here, and never treat its absence as
		# permission. Every copy of a server passes the local checks — that is the
		# whole reason the lease exists — so "whoever asks first when the bucket is
		# empty" would hand ownership to the test copy just as readily as to
		# production, which is exactly the outcome this is supposed to prevent.
		# A bucket with no lease is initialised once, deliberately, from the
		# environment that really owns it (Take Ownership of This Storage) or by
		# the app's own install on a site that has never had one.
		return _lease_state(
			LEASE_UNVERIFIED,
			"The bucket carries no ownership lease yet, so which server owns it cannot be "
			"established. On the site that really owns this bucket, use 'Take Ownership of "
			"This Storage' once to record it. Until then nothing here is deleted, moved or "
			"overwritten in the bucket.",
		)

	holder_owner = (lease.get("owner_id") or "").strip()
	if holder_owner and holder_owner != recorded_owner_id(settings):
		return _lease_state(
			LEASE_CONFLICT,
			f"The bucket's ownership lease belongs to environment '{holder_owner}'"
			+ (f" on site '{lease.get('site')}'" if lease.get("site") else "")
			+ ", not to this one. Two environments are pointed at the same bucket.",
			lease,
		)

	if (lease.get("instance") or "") == mine:
		if _refresh_heartbeat(settings, lease, etag) is False:
			# The conditional refresh was rejected, which means the lease changed
			# between our read and our write: somebody took ownership while we were
			# looking at it. We have direct evidence we no longer hold it, so this
			# call must not answer "yes" — re-read once and let the new content
			# decide.
			if depth == 0:
				return _evaluate_lease(settings, depth=1)
			return _lease_state(
				LEASE_CONFLICT,
				"Ownership of this bucket changed while this check was running. "
				"Nothing will be deleted, moved or overwritten from here until it settles.",
			)
		return _lease_state(LEASE_OK, holder=lease)

	# Held by another server. Age is reported, never acted on: the heartbeat is
	# only written when the lease is checked, and the lease is only checked before
	# a destructive operation, so an old one means "that server has had nothing to
	# delete", not "that server is gone". Handing the lease over on a timeout is
	# precisely how a test copy would acquire it.
	age = _lease_age(lease)
	stale = age is None or age > LEASE_STALE_AFTER
	when = lease.get("heartbeat_at") or "never"
	return _lease_state(
		LEASE_CONFLICT,
		"The bucket's ownership lease is held by another server"
		+ (f" (host '{lease.get('host')}', site '{lease.get('site')}')" if lease.get("host") else "")
		+ f", last seen {when}. Nothing in the bucket will be deleted, moved or overwritten from "
		"here. If that server is genuinely gone — decommissioned, renamed, rebuilt — use 'Take "
		"Ownership of This Storage' to move the lease deliberately. If it is still live, this "
		"site is a copy of it: give it its own bucket.",
		lease,
		stale=stale,
	)


def _refresh_heartbeat(settings, lease, etag):
	"""Re-stamp our own lease so an administrator can see this server is alive.

	The heartbeat's *age* grants nothing (see the note above), but the refresh's
	*outcome* does carry information: a rejected conditional write means the
	lease changed under us. Returns False in exactly that case, None otherwise —
	including when there was nothing to do.
	"""
	if read_only_mode(settings):
		return None
	age = _lease_age(lease)
	if age is not None and age < LEASE_HEARTBEAT_INTERVAL:
		return None
	try:
		_write_lease(settings, if_match=etag)
	except LeasePreconditionFailed:
		_clear_lease_cache(settings)
		return False
	except LeaseError as e:
		frappe.logger().warning(f"aws_s3_storage: could not refresh the ownership lease: {e}")
	except Exception as e:
		frappe.logger().warning(f"aws_s3_storage: could not refresh the ownership lease: {e}")
	return None


def heartbeat():
	"""Scheduler job: keep the owner's lease visibly current.

	Without it the only thing that ever refreshes the lease is a deletion, so an
	administrator looking at "last seen" on a quiet site would see a date from
	months ago and have no way to tell a live owner from an abandoned one.
	"""
	settings = frappe.get_single("S3 Settings")
	if not settings.bucket_name or read_only_mode(settings):
		return
	if ownership(settings).state != OWNER:
		return
	try:
		lease, etag = _read_lease(settings)
	except Exception:
		return
	if lease is None:
		return
	if (lease.get("instance") or "") != instance_id():
		return
	try:
		_write_lease(settings, if_match=etag)
	except Exception:
		pass


def _clear_lease_cache(settings=None):
	settings = _settings(settings)
	try:
		frappe.cache().delete_value(_lease_cache_key(settings))
	except Exception:
		pass


# ---------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------


def claim(settings=None, force=False, adopt_existing_files=False, bootstrap=False):
	"""Record this environment as the owner of the configured bucket.

	``bootstrap=True`` is the automatic call from ``after_install`` /
	``after_migrate``. It is *not* the same act as a person pressing the button,
	and the difference is the whole point of this argument:

	* it may **create** the lease when the bucket has none, conditionally, so a
	  brand-new site works without ceremony;
	* it may never **replace** a lease another server holds. Restoring an old
	  database with no owner id and running ``bench migrate`` would otherwise
	  take production's lease away, automatically, which is precisely the thing
	  the documentation promises only a human can do.

	The owner id is kept, not regenerated, when this site is already the
	verified owner: the id is stamped on every ``File.s3_owner``, so minting a
	new one would leave the owner unable to touch its own files.
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

	owner_id = info.owner_id if info.state == OWNER else uuid.uuid4().hex
	_write_local_owner_id(owner_id)

	frappe.db.set_single_value("S3 Settings", "storage_owner_id", owner_id)
	frappe.db.set_single_value("S3 Settings", "storage_owner_site", current_site())
	frappe.db.set_single_value("S3 Settings", "storage_owner_instance", instance_id())
	frappe.db.set_single_value("S3 Settings", "storage_owner_claimed_at", now_datetime())
	frappe.clear_document_cache("S3 Settings", "S3 Settings")

	if adopt_existing_files:
		adopt_untagged_files(owner_id)

	# Take the lease in the bucket too, so any other server pointed at it stands
	# down. This is what makes the claim reach beyond this machine — without it,
	# "take ownership" on a new server leaves the old one believing it is still
	# the owner, because nothing it can see has changed.
	_clear_lease_cache()
	lease_error = None
	fresh = frappe.get_single("S3 Settings")
	if fresh.bucket_name:
		try:
			if bootstrap:
				_create_lease_if_absent(fresh)
			else:
				_take_lease_explicitly(fresh)
		except LeaseReadOnly as e:
			lease_error = str(e)
		except Exception as e:
			lease_error = str(e)
			frappe.logger().error(f"aws_s3_storage: claimed locally but could not take the lease: {e}")
	_clear_lease_cache()

	frappe.logger().info(f"aws_s3_storage: storage claimed by {owner_id} (site {current_site()})")
	info = ownership()
	info.lease_error = lease_error
	return info


def _create_lease_if_absent(settings):
	"""Write the lease only if the bucket has none. Never replaces one.

	The automatic half of claiming. A lease that is already there belongs to
	whoever wrote it, and an install or a migrate is not a decision to take it
	from them — the site simply ends up in conflict, which is the honest answer.
	"""
	try:
		return _write_lease(settings, expect_absent=True)
	except LeasePreconditionFailed:
		frappe.logger().info(
			"aws_s3_storage: the bucket already carries an ownership lease; leaving it alone."
		)
		return None


def _take_lease_explicitly(settings, attempts=2):
	"""Move the lease to this server because a person asked for it.

	Still conditional, so two administrators pressing the button at the same
	moment cannot both succeed; the loser re-reads and retries once.

	Only a *missing capability* — an endpoint with no conditional writes — falls
	back to a plain write, and only here, because a person has explicitly asked.
	Losing the race to another server is the opposite situation: it is the
	condition doing its job, and repeating the write without it would hand this
	server an ownership it was just told it does not have.
	"""
	for attempt in range(attempts):
		lease, etag = _read_lease(settings)
		try:
			if lease is None:
				return _write_lease(settings, expect_absent=True)
			return _write_lease(settings, if_match=etag)
		except LeasePreconditionFailed:
			if attempt + 1 < attempts:
				continue
			raise RuntimeError(
				"another server took the ownership lease while this one was taking it. "
				"Check which server should own this bucket before trying again."
			) from None
		except LeaseConditionUnsupported:
			written, failure = _put_lease(settings, _lease_payload(settings))
			if written:
				frappe.logger().warning(
					"aws_s3_storage: took the ownership lease without a condition — this endpoint "
					"does not support conditional writes, so a simultaneous takeover elsewhere "
					"would not have been detected."
				)
				return _lease_payload(settings)
			raise RuntimeError(f"could not take the ownership lease ({failure})") from None


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

	A failure here is not cosmetic — an unclaimed site cannot modify the bucket
	at all — so it is reported on the migrate output and in the Error Log rather
	than only in a log file nobody reads.
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
		claim(bootstrap=True)
	except Exception as e:
		# Never fail an install or a migrate over this, but never hide it either:
		# until the claim succeeds this site will not write to, move or delete
		# anything in the bucket, and new uploads go to local disk.
		frappe.logger().error(f"aws_s3_storage: could not claim storage ownership: {e}")
		print(f"aws_s3_storage: COULD NOT CLAIM STORAGE OWNERSHIP — {e}")
		print(
			"aws_s3_storage: until this succeeds, S3 objects are not created, moved or "
			"deleted from this site and new uploads go to local disk. Check that "
			"site_config.json is writable, then use 'Take Ownership of This Storage' "
			"in S3 Settings."
		)
		try:
			frappe.log_error(
				title="aws_s3_storage: could not claim storage ownership",
				message=f"{e}\n\nS3 objects will not be modified from this site until this is resolved.",
			)
		except Exception:
			pass


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


def read_only_mode(settings=None):
	return bool(cint(_settings(settings).get("read_only_mode")))


def may_modify_storage(settings=None):
	"""True when this environment is allowed to change anything in the bucket.

	Proof of ownership is required, not merely the absence of proof to the
	contrary: ``unclaimed`` is refused along with ``foreign``. A database saved
	before ownership existed carries no owner id at all, so it lands unclaimed
	wherever it is restored — treating that as permission would let exactly the
	backup an administrator is most likely to experiment with modify live files.
	``after_install`` / ``after_migrate`` claim on the real site, so the unclaimed
	window there is the length of one migrate.

	Cheap: local values only. Destructive work goes through ``may_destroy``,
	which also consults the bucket.
	"""
	settings = _settings(settings)
	if read_only_mode(settings):
		return False
	return ownership(settings).state == OWNER


def may_destroy(file_doc=None, settings=None, owner=None):
	"""True when this environment may *destroy or overwrite* this object.

	Everything ``may_modify_object`` asks, plus the bucket's own lease — the only
	check that can see a second live server, because it is the only state the two
	of them share. Reserved for operations that cannot be undone from here:
	deleting an object, replacing one (a privacy move, a regenerated thumbnail),
	or moving a file out of S3.
	"""
	settings = _settings(settings)
	if not may_modify_object(file_doc=file_doc, settings=settings, owner=owner):
		return False
	return check_lease(settings).status == LEASE_OK


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
	if info.state == UNCLAIMED:
		return (
			"No environment has claimed this storage, so there is nothing to prove this "
			"site owns it. Run bench migrate on the site that does, or use 'Take "
			"Ownership of This Storage' here."
		)

	lease = check_lease(settings)
	if lease.status != LEASE_OK:
		return lease.reason

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


def guard(operation, file_doc=None, settings=None, owner=None, destructive=False, detail=None):
	"""``True`` when the caller may proceed; ``False`` (reported) when it may not.

	``destructive=True`` for anything that removes or replaces an object that is
	already in the bucket — those additionally require the lease.
	"""
	check = may_destroy if destructive else may_modify_object
	if check(file_doc=file_doc, settings=settings, owner=owner):
		return True
	report_blocked(operation, detail=detail, settings=settings)
	return False


# ---------------------------------------------------------------------------
# Admin UI
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_environment_status(check_bucket=0):
	"""Everything the S3 Settings form needs to explain the current state.

	``check_bucket`` re-reads the lease instead of using the cached answer — used
	when opening the "take ownership" dialog, where a stale answer is the one
	that matters.
	"""
	frappe.only_for("System Manager")
	settings = frappe.get_single("S3 Settings")
	info = ownership(settings)

	status = {
		"state": info.state,
		"reason": info.reason,
		"owner_id": info.owner_id,
		"owner_site": info.owner_site,
		"owner_instance": (settings.get("storage_owner_instance") or "").strip(),
		"instance": instance_id(),
		"site": info.site,
		"has_local_token": bool(info.local_owner_id),
		"read_only_mode": read_only_mode(settings),
		"may_modify_storage": may_modify_storage(settings),
		"inherited_storage_policy": settings.get("inherited_storage_policy") or POLICY_READ_ONLY,
		"bucket": settings.get("bucket_name"),
	}

	# The server this database was claimed from, compared with the server running
	# now. Informational only — a renamed host or a rebuilt container changes it
	# legitimately, which is why the lease and not this decides anything.
	status["instance_changed"] = bool(
		status["owner_instance"] and status["owner_instance"] != status["instance"]
	)

	if settings.get("bucket_name"):
		lease = check_lease(settings, refresh=cint(check_bucket))
		status["lease_status"] = lease.status
		status["lease_reason"] = lease.reason
		status["lease_holder"] = lease.holder
		status["lease_stale"] = bool(lease.stale)

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
	status["unattributed_deletions"] = frappe.db.count(
		"S3 Deletion Queue", {"requested_by_environment": ["in", ["", None]]}
	)
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
	return {
		"state": info.state,
		"owner_id": info.owner_id,
		"site": info.site,
		"instance": instance_id(),
		"lease_error": info.get("lease_error"),
	}


@frappe.whitelist()
def adopt_unattributed_deletions():
	"""Attribute deletion requests that record no environment to this one.

	Rows queued before the environment column existed cannot be told apart from
	rows a restored copy brought with it, so they are parked rather than run.
	This is the deliberate "yes, these are mine" — it is a person saying it, on
	the site that owns the bucket, which is the only place the answer is known.
	"""
	frappe.only_for("System Manager")
	settings = frappe.get_single("S3 Settings")
	owner_id = recorded_owner_id(settings)

	# Holding an owner id proves nothing: a restored copy holds the same one,
	# because it came out of the same database. Adoption turns a parked request
	# into a live deletion, so it takes the same proof a deletion does — the
	# environment checks *and* the bucket's lease.
	if not owner_id or not may_modify_storage(settings):
		frappe.throw(f"This site cannot adopt deletion requests: {blocked_reason(settings)}")

	lease = check_lease(settings, refresh=True)
	if lease.status != LEASE_OK:
		frappe.throw(f"This site cannot adopt deletion requests: {lease.reason}")

	# And only requests aimed at the bucket this site actually owns.
	names = frappe.get_all(
		"S3 Deletion Queue",
		filters={
			"requested_by_environment": ["in", ["", None]],
			"bucket": settings.bucket_name,
		},
		pluck="name",
	)
	for name in names:
		frappe.db.set_value(
			"S3 Deletion Queue",
			name,
			{"requested_by_environment": owner_id, "status": "Pending", "last_error": None},
			update_modified=False,
		)
	frappe.db.commit()
	return {"adopted": len(names)}


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
