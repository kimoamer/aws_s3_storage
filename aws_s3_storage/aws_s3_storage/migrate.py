# Copyright (c) 2026, Innomate LLC
# For license information, please see license.txt
"""Migrate files that already live on local disk into S3.

The app only sends *new* uploads to S3; files that existed before installation
stay on disk. This tool moves them in resumable batches so you can actually
reclaim server disk space.

Recommended first run — upload and rewrite records, but keep the local copies so
you can verify counts/sizes before anything is deleted:

    bench --site <site> execute \
        aws_s3_storage.aws_s3_storage.migrate.run_migration \
        --kwargs '{"batch_size": 100, "delete_local": 0}'

Every migrated File gets its ``s3_key`` set, so the pending query naturally
excludes it — the job is safe to stop and resume at any time. Files that can't be
migrated (missing content) are skipped for the rest of the run instead of being
retried in a loop.
"""

import mimetypes
import os

import frappe
from frappe.utils import cint

from aws_s3_storage.aws_s3_storage import s3_utils

_PENDING_FILTERS = {
	"is_folder": 0,
	"s3_key": ["in", ["", None]],
	"file_url": ["like", "%/files/%"],
}


def _pending_local_files(limit, exclude=None):
	filters = dict(_PENDING_FILTERS)
	if exclude:
		filters["name"] = ["not in", list(exclude)]
	return frappe.get_all("File", filters=filters, pluck="name", limit=limit, order_by="creation asc")


def count_pending():
	return frappe.db.count("File", dict(_PENDING_FILTERS))


@frappe.whitelist()
def start_migration(batch_size=100, delete_local=1):
	"""Enqueue the migration as a background job (System Manager only)."""
	frappe.only_for("System Manager")
	frappe.enqueue(
		"aws_s3_storage.aws_s3_storage.migrate.run_migration",
		queue="long",
		timeout=0,
		batch_size=cint(batch_size),
		delete_local=cint(delete_local),
	)
	return {"pending": count_pending()}


def run_migration(batch_size=100, delete_local=1):
	"""Migrate all pending local files to S3 in batches, committing between them."""
	batch_size = cint(batch_size) or 100
	delete_local = cint(delete_local)
	totals = {"migrated": 0, "skipped": 0, "missing": 0, "failed": 0}
	# Names that were processed but did not migrate — excluded from later batches so a
	# batch of only missing/failed files can never loop forever.
	done_not_migrated = set()

	while True:
		names = _pending_local_files(batch_size, exclude=done_not_migrated)
		if not names:
			break

		for name in names:
			try:
				result = migrate_file(name, delete_local=delete_local)
			except Exception as e:
				frappe.db.rollback()
				result = "failed"
				frappe.logger().error(f"S3 migration failed for File {name}: {e}")
			totals[result] = totals.get(result, 0) + 1
			if result != "migrated":
				done_not_migrated.add(name)

		frappe.db.commit()

	frappe.logger().info(f"S3 migration finished: {totals}")
	return totals


def migrate_file(name, delete_local=1):
	"""Migrate a single File record to S3. Returns migrated/skipped/missing."""
	doc = frappe.get_doc("File", name)

	if doc.is_folder or doc.get("s3_key") or s3_utils._extract_key(doc.file_url):
		return "skipped"

	old_main_url = doc.file_url

	# Read the local content (get_content reads from disk for non-S3 files).
	try:
		content = doc.get_content()
	except Exception:
		frappe.logger().error(f"S3 migration: content missing for File {name} ({doc.file_url})")
		return "missing"

	if isinstance(content, str):
		content = content.encode("utf-8")

	settings = frappe.get_single("S3 Settings")
	bucket = settings.bucket_name
	s3 = s3_utils.get_s3_client()

	new_key = s3_utils._new_key(doc.file_name, doc.is_private)
	content_type = mimetypes.guess_type(doc.file_name)[0] or "application/octet-stream"
	s3.put_object(
		Bucket=bucket,
		Key=new_key,
		Body=content,
		ContentType=content_type,
		**s3_utils._upload_extra_args(settings, content),
	)
	if not s3_utils.object_exists(new_key, expected_size=len(content), s3=s3, bucket=bucket):
		raise RuntimeError(f"Upload verification failed for {new_key}")

	update = {"file_url": s3_utils._build_file_url(new_key), "s3_key": new_key}

	# Migrate a local thumbnail too, if any.
	old_thumb_url = None
	thumb_url = doc.get("thumbnail_url")
	if thumb_url and "/files/" in thumb_url and not s3_utils._extract_key(thumb_url):
		thumb_path = _full_path(thumb_url)
		if thumb_path and os.path.exists(thumb_path):
			with open(thumb_path, "rb") as f:
				thumb_bytes = f.read()
			thumb_key = (
				s3_utils._swap_prefix(new_key, doc.is_private).rsplit("/", 1)[0]
				+ "/"
				+ os.path.basename(thumb_url)
			)
			thumb_type = mimetypes.guess_type(thumb_url)[0] or "image/png"
			s3.put_object(
				Bucket=bucket,
				Key=thumb_key,
				Body=thumb_bytes,
				ContentType=thumb_type,
				**s3_utils._upload_extra_args(settings, thumb_bytes),
			)
			update["thumbnail_url"] = s3_utils._build_file_url(thumb_key)
			update["s3_thumbnail_key"] = thumb_key
			old_thumb_url = thumb_url

	doc.db_set(update, update_modified=False)
	# The File record now points at S3, but the linked document's own Attach /
	# Attach Image field still holds the old local URL — repoint it too.
	_update_attached_field(doc, old_main_url, update["file_url"])
	frappe.db.commit()

	if delete_local:
		# A shared upload (several File rows pointing at one local file) must keep the
		# local copy until the last referencing record has been migrated.
		_remove_local_if_unreferenced("file_url", old_main_url)
		if old_thumb_url:
			_remove_local_if_unreferenced("thumbnail_url", old_thumb_url)

	return "migrated"


def _remove_local_if_unreferenced(field, url):
	if not url or frappe.db.exists("File", {field: url}):
		return
	path = _full_path(url)
	try:
		if path and os.path.exists(path):
			os.remove(path)
	except Exception as e:
		frappe.logger().error(f"S3 migration: could not remove local file {path}: {e}")


def _update_attached_field(doc, old_url, new_url):
	"""Repoint the linked document's Attach / Attach Image field at the new URL.

	Only touches the field when it still holds the exact old local URL, so it can
	never clobber an unrelated value.
	"""
	if not (doc.attached_to_doctype and doc.attached_to_name and doc.attached_to_field):
		return
	try:
		current = frappe.db.get_value(doc.attached_to_doctype, doc.attached_to_name, doc.attached_to_field)
		if current == old_url:
			frappe.db.set_value(
				doc.attached_to_doctype,
				doc.attached_to_name,
				doc.attached_to_field,
				new_url,
				update_modified=False,
			)
	except Exception as e:
		frappe.logger().error(
			f"S3 migration: could not update {doc.attached_to_doctype}.{doc.attached_to_field}"
			f" for {doc.attached_to_name}: {e}"
		)


# ---------------------------------------------------------------------------
# Reclaim local space after a delete_local=0 migration
# ---------------------------------------------------------------------------


@frappe.whitelist()
def start_cleanup(batch_size=200):
	"""Enqueue deletion of local copies of already-migrated files (System Manager)."""
	frappe.only_for("System Manager")
	frappe.enqueue(
		"aws_s3_storage.aws_s3_storage.migrate.cleanup_migrated_local_files",
		queue="long",
		timeout=0,
		batch_size=cint(batch_size),
	)
	return {"ok": True}


def cleanup_migrated_local_files(batch_size=200):
	"""Delete on-disk copies of files that are already migrated and verified in S3.

	Use this after a ``delete_local=0`` migration. It never removes a file another
	(not-yet-migrated) File record still points at locally, and only after
	confirming the S3 object exists.
	"""
	batch_size = cint(batch_size) or 200
	removed = 0
	last = ""
	while True:
		rows = frappe.get_all(
			"File",
			filters={"is_folder": 0, "s3_key": ["not in", ["", None]], "name": [">", last]},
			fields=["name", "file_name", "is_private", "s3_key"],
			order_by="name asc",
			limit=batch_size,
		)
		if not rows:
			break
		for row in rows:
			last = row.name
			if _delete_migrated_local_copy(row):
				removed += 1
		frappe.db.commit()

	frappe.logger().info(f"S3 local cleanup finished: removed {removed} file(s)")
	return {"removed": removed}


def _delete_migrated_local_copy(row):
	if not row.file_name:
		return False
	local_url = ("/private/files/" if row.is_private else "/files/") + row.file_name
	path = _full_path(local_url)
	if not path or not os.path.exists(path):
		return False
	# A sibling still on local disk needs the file — keep it.
	if frappe.db.exists("File", {"file_url": local_url}):
		return False
	# Confirm the object is really in S3 before deleting the only local copy.
	try:
		if not s3_utils.object_exists(row.s3_key):
			return False
	except Exception:
		return False
	try:
		os.remove(path)
		return True
	except Exception as e:
		frappe.logger().error(f"S3 cleanup: could not remove {path}: {e}")
		return False


# ---------------------------------------------------------------------------
# Read-only audit of stale local links
# ---------------------------------------------------------------------------

_LINK_FIELDTYPES = ("Attach", "Attach Image", "Text Editor", "HTML Editor", "Code")


@frappe.whitelist()
def audit_local_links():
	"""Report fields that still contain local ``/files/`` links (read-only).

	Attach / Attach Image fields are repointed automatically during migration, but
	links embedded in rich-text / HTML / Print Format content are **not** rewritten.
	This lists where such links remain so they can be reviewed manually — it changes
	nothing.
	"""
	frappe.only_for("System Manager")

	fields = frappe.get_all(
		"DocField",
		filters={"fieldtype": ["in", _LINK_FIELDTYPES]},
		fields=["parent as doctype", "fieldname", "fieldtype"],
	)
	fields += frappe.get_all(
		"Custom Field",
		filters={"fieldtype": ["in", _LINK_FIELDTYPES]},
		fields=["dt as doctype", "fieldname", "fieldtype"],
	)

	findings = []
	for f in fields:
		try:
			meta = frappe.get_meta(f.doctype)
			if meta.issingle or getattr(meta, "is_virtual", 0):
				continue
			count = frappe.db.count(f.doctype, {f.fieldname: ["like", "%/files/%"]})
			if count:
				findings.append(
					{"doctype": f.doctype, "fieldname": f.fieldname, "fieldtype": f.fieldtype, "rows": count}
				)
		except Exception:
			continue

	findings.sort(key=lambda x: x["rows"], reverse=True)
	return findings


def _full_path(file_url):
	if not file_url:
		return None
	if file_url.startswith("/private/files/"):
		return frappe.get_site_path("private", "files", os.path.basename(file_url))
	if file_url.startswith("/files/"):
		return frappe.get_site_path("public", "files", os.path.basename(file_url))
	return None
