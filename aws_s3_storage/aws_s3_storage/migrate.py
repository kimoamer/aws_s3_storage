# Copyright (c) 2026, Innomate LLC
# For license information, please see license.txt
"""Migrate files that already live on local disk into S3.

The app only sends *new* uploads to S3; files that existed before installation
stay on disk. This tool moves them in resumable batches so you can reclaim server
disk space.

Recommended flow (also the default of the S3 Settings buttons):

    1. Migrate with delete_local=0  -> uploads + rewrites records, keeps local
    2. Verify sample public/private files, run audit_local_links, review failures
    3. cleanup_migrated_local_files -> deletes the verified local copies

Every migrated File gets its ``s3_key`` set, so the pending query naturally
excludes it — the job is safe to stop and resume. Files whose content is missing
are skipped for the rest of a run instead of looping. Uploads stream from disk
(multipart), so a large file does not have to fit in memory.

Attachments that an app rewrites in place by path (see
``s3_utils.is_local_only_file``) are always skipped: they have to stay on disk.
So are files outside the configured doctype scope (see ``s3_utils.in_scope``) —
with "Limit S3 Storage to Specific Doctypes" on, the migration only ever touches
attachments of the listed doctypes, and the pending count reflects that.
"""

import mimetypes
import os
import re
import time

import frappe
from frappe.utils import cint, now_datetime

from aws_s3_storage.aws_s3_storage import s3_utils

STATUS_DOCTYPE = "S3 Migration Status"
ERROR_DOCTYPE = "S3 Migration Error"
# Each background job works for at most this long, then re-enqueues itself, so a run
# of thousands of files never hits the platform's per-job timeout (~1500s).
_TIME_BUDGET_SECONDS = 1000
_MAX_FILES_PER_JOB = 400
_STATUS_FIELDS = (
	"status",
	"total_files",
	"processed_files",
	"migrated_files",
	"failed_files",
	"missing_files",
	"skipped_files",
	"started_at",
	"finished_at",
	"last_file",
	"error_message",
)

_PENDING_FILTERS = {
	"is_folder": 0,
	"s3_key": ["in", ["", None]],
	"file_url": ["like", "%/files/%"],
}


def _pending_filter_sets(settings=None):
	"""The File filters describing what is still to migrate, honouring the scope.

	One dict per query rather than a single OR: both halves of a restricted scope
	("attached to one of these doctypes" and "attached to nothing") then stay plain
	indexed lookups that ``frappe.db.count`` can run as well. The sets are mutually
	exclusive, so nothing is counted twice. An empty list means the configured scope
	covers no file at all.
	"""
	settings = settings if settings is not None else frappe.get_single("S3 Settings")
	if not s3_utils.is_scope_restricted(settings):
		return [dict(_PENDING_FILTERS)]

	filter_sets = []
	doctypes = sorted(s3_utils.scoped_doctypes(settings))
	if doctypes:
		filter_sets.append({**_PENDING_FILTERS, "attached_to_doctype": ["in", doctypes]})
	if cint(settings.get("include_unattached_files")):
		# "is not set" covers both NULL and '' — a File attached to nothing may carry
		# either, depending on how it was created.
		filter_sets.append({**_PENDING_FILTERS, "attached_to_doctype": ["is", "not set"]})
	return filter_sets


def _pending_local_files(limit, exclude=None, settings=None):
	names = []
	for filters in _pending_filter_sets(settings):
		filters = dict(filters)
		if exclude:
			filters["name"] = ["not in", list(exclude)]
		names += frappe.get_all(
			"File", filters=filters, pluck="name", limit=limit - len(names), order_by="creation asc"
		)
		if len(names) >= limit:
			break
	return names


def count_pending(settings=None):
	return sum(frappe.db.count("File", filters) for filters in _pending_filter_sets(settings))


# ---------------------------------------------------------------------------
# Status (single doctype) + concurrency lock
# ---------------------------------------------------------------------------


def _set_status(**values):
	for field, value in values.items():
		frappe.db.set_single_value(STATUS_DOCTYPE, field, value)
	frappe.db.commit()


def _migration_active():
	return frappe.db.get_single_value(STATUS_DOCTYPE, "status") in ("Queued", "Running")


@frappe.whitelist()
def get_migration_status():
	"""Return the current migration status (and recent errors) for the admin UI."""
	frappe.only_for("System Manager")
	doc = frappe.get_single(STATUS_DOCTYPE)
	status = {field: doc.get(field) for field in _STATUS_FIELDS}
	status["scope"] = s3_utils.scope_description()
	status["pending"] = count_pending()
	status["errors"] = get_migration_errors(limit=20)
	return status


@frappe.whitelist()
def get_migration_errors(limit=100):
	"""List files that failed to migrate, with the reason."""
	frappe.only_for("System Manager")
	return frappe.get_all(
		ERROR_DOCTYPE,
		fields=["file", "reason", "file_url", "error", "creation"],
		order_by="creation desc",
		limit=cint(limit) or 100,
	)


def _clear_errors():
	frappe.db.delete(ERROR_DOCTYPE)
	frappe.db.commit()


def _record_error(name, reason, error):
	"""Persist why a file did not migrate so the admin can see it in the UI."""
	try:
		file_url = frappe.db.get_value("File", name, "file_url")
		frappe.get_doc(
			{
				"doctype": ERROR_DOCTYPE,
				"file": name,
				"file_url": file_url,
				"reason": reason,
				"error": (error or "")[:2000],
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		frappe.logger().error(f"S3 migration: could not record error for File {name}")


@frappe.whitelist()
def reset_migration_status():
	"""Clear a stuck 'Running' status (e.g. after a worker crash)."""
	frappe.only_for("System Manager")
	_set_status(status="Idle", finished_at=now_datetime())
	return {"ok": True}


def _write_progress(totals, last_file):
	_set_status(
		processed_files=sum(totals.values()),
		migrated_files=totals["migrated"],
		failed_files=totals["failed"],
		missing_files=totals["missing"],
		skipped_files=totals["skipped"],
		last_file=last_file,
	)


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


@frappe.whitelist()
def start_migration(batch_size=100, delete_local=0):
	"""Enqueue the migration as a background job (System Manager only).

	Defaults to *not* deleting local files — reclaim disk space separately with
	``cleanup_migrated_local_files`` once you have verified the migration. Refuses
	to start if a migration is already running.
	"""
	frappe.only_for("System Manager")
	if _migration_active():
		frappe.throw("A migration is already running.")

	_set_status(
		status="Queued",
		total_files=count_pending(),
		processed_files=0,
		migrated_files=0,
		failed_files=0,
		missing_files=0,
		skipped_files=0,
		started_at=now_datetime(),
		finished_at=None,
		last_file=None,
		error_message=None,
	)
	_clear_errors()
	_enqueue_run(cint(batch_size), cint(delete_local), _TIME_BUDGET_SECONDS)
	return get_migration_status()


def _enqueue_run(batch_size, delete_local, time_budget):
	frappe.enqueue(
		"aws_s3_storage.aws_s3_storage.migrate.run_migration",
		queue="long",
		timeout=(time_budget + 300) if time_budget else 0,
		batch_size=batch_size,
		delete_local=delete_local,
		time_budget=time_budget,
	)


def _read_totals():
	doc = frappe.get_single(STATUS_DOCTYPE)
	return {
		"migrated": cint(doc.migrated_files),
		"failed": cint(doc.failed_files),
		"missing": cint(doc.missing_files),
		"skipped": cint(doc.skipped_files),
	}


def run_migration(batch_size=100, delete_local=0, time_budget=0, max_files=0, reset=1):
	"""Migrate pending local files to S3.

	* ``time_budget=0`` (default, e.g. ``bench execute``) runs to completion.
	* ``time_budget>0`` (the S3 Settings button) works that long, then re-enqueues.
	* ``max_files>0`` stops after that many files (the daily scheduled migration).
	* ``reset=0`` keeps the running counters (so the daily run shows cumulative
	  progress across days) and pauses on "Idle" instead of resetting.

	Counts accumulate in the status doctype; failed/missing files (recorded in the
	error log) are skipped so the run always makes progress and terminates.
	"""
	batch_size = cint(batch_size) or 100
	delete_local = cint(delete_local)
	time_budget = cint(time_budget)
	max_files = cint(max_files)
	reset = cint(reset)

	status = frappe.db.get_single_value(STATUS_DOCTYPE, "status")
	if reset and status not in ("Running", "Queued"):
		# Fresh standalone run (console, or first invocation): reset counters.
		_set_status(
			status="Running",
			started_at=now_datetime(),
			finished_at=None,
			error_message=None,
			total_files=count_pending(),
			processed_files=0,
			migrated_files=0,
			failed_files=0,
			missing_files=0,
			skipped_files=0,
			last_file=None,
		)
		_clear_errors()
	elif status == "Queued":
		_set_status(status="Running", started_at=now_datetime())
	else:
		# Resume (chained job, or the daily incremental run): keep the counters.
		_set_status(status="Running", started_at=now_datetime(), error_message=None)
		if not cint(frappe.db.get_single_value(STATUS_DOCTYPE, "total_files")):
			_set_status(total_files=count_pending())

	totals = _read_totals()
	# Persisted across jobs via the error log, so failed/missing files are not
	# retried forever.
	done_not_migrated = set(frappe.get_all(ERROR_DOCTYPE, pluck="file"))

	started = time.monotonic()
	processed_this_job = 0

	# Reuse one S3 client + settings for the whole job instead of rebuilding them
	# per file — matters a lot at tens of thousands of files.
	settings = frappe.get_single("S3 Settings")
	s3 = s3_utils.get_s3_client()

	def _stop():
		if time_budget and (
			processed_this_job >= _MAX_FILES_PER_JOB or (time.monotonic() - started) >= time_budget
		):
			return True
		return bool(max_files) and processed_this_job >= max_files

	try:
		while not _stop():
			names = _pending_local_files(batch_size, exclude=done_not_migrated, settings=settings)
			if not names:
				break

			for name in names:
				err = None
				try:
					result = migrate_file(name, delete_local=delete_local, s3=s3, settings=settings)
				except Exception as e:
					frappe.db.rollback()
					result = "failed"
					err = str(e)
					frappe.logger().error(f"S3 migration failed for File {name}: {e}")
				if result == "missing":
					err = "Local file not found on disk"
				totals[result] = totals.get(result, 0) + 1
				if result != "migrated":
					done_not_migrated.add(name)
					if result in ("failed", "missing"):
						_record_error(name, result.capitalize(), err)
				processed_this_job += 1
				if _stop():
					break

			frappe.db.commit()
			_write_progress(totals, names[-1])
	except Exception as e:
		frappe.db.rollback()
		_set_status(status="Failed", finished_at=now_datetime(), error_message=str(e))
		frappe.logger().error(f"S3 migration aborted: {e}")
		raise

	still_pending = bool(_pending_local_files(1, exclude=done_not_migrated, settings=settings))
	if time_budget and still_pending:
		# Chained mode: continue in a fresh job.
		_enqueue_run(batch_size, delete_local, time_budget)
	elif max_files and still_pending:
		# Daily quota reached but files remain: pause until the next scheduled run.
		_set_status(status="Idle", finished_at=now_datetime())
	else:
		_set_status(status="Completed", finished_at=now_datetime())

	frappe.logger().info(f"S3 migration job done: {totals} (+{processed_this_job} this job)")
	return totals


def scheduled_migration():
	"""Daily scheduler: migrate up to `daily_migration_limit` local files.

	Optionally deletes each local copy right after it is migrated and verified
	(``auto_delete_after_migration``), so disk frees up incrementally. Skips if a
	migration is already running or S3 storage isn't configured.
	"""
	settings = frappe.get_single("S3 Settings")
	if not cint(settings.get("enable_scheduled_migration")):
		return
	if not cint(settings.get("enabled")) or not settings.bucket_name:
		return
	if _migration_active():
		return

	limit = cint(settings.get("daily_migration_limit")) or 1000
	delete_local = cint(settings.get("auto_delete_after_migration"))
	run_migration(batch_size=100, delete_local=delete_local, max_files=limit, reset=0)


def migrate_file(name, delete_local=0, s3=None, settings=None):
	"""Migrate a single File record to S3. Returns migrated/skipped/missing.

	``s3``/``settings`` may be passed in so a batch reuses one client instead of
	building a new boto3 client (and re-reading settings) per file.
	"""
	doc = frappe.get_doc("File", name)

	if doc.is_folder or doc.get("s3_key") or s3_utils._extract_key(doc.file_url):
		return "skipped"

	# Some attachments are rewritten in place by path and must stay on disk
	# (ERPNext's reposting data file) — migrating them would break the owning app.
	if s3_utils.is_local_only_file(doc):
		return "skipped"

	settings = settings or frappe.get_single("S3 Settings")

	# Doctypes the admin kept out of S3 stay on disk. Checked here as well as in the
	# pending query, so a direct call (bench execute) cannot move a file the scope
	# excludes — and an out-of-scope file with no content is a skip, not an error.
	if not s3_utils.in_scope(doc, settings):
		return "skipped"

	old_main_url = doc.file_url
	local_path = _full_path(old_main_url)
	if not local_path or not os.path.exists(local_path):
		frappe.logger().error(f"S3 migration: content missing for File {name} ({old_main_url})")
		return "missing"

	bucket = settings.bucket_name
	s3 = s3 or s3_utils.get_s3_client()

	# Stream the file straight from disk (multipart for large files) instead of
	# loading it into memory.
	local_size = os.path.getsize(local_path)
	new_key = s3_utils._new_key(doc.file_name, doc.is_private)
	content_type = mimetypes.guess_type(doc.file_name)[0] or "application/octet-stream"
	s3.upload_file(local_path, bucket, new_key, ExtraArgs=_extra_args(settings, content_type))
	# Remove the new object if the record update below rolls back.
	s3_utils._delete_on_rollback(bucket, [new_key])
	if not s3_utils.object_exists(new_key, expected_size=local_size, s3=s3, bucket=bucket):
		raise RuntimeError(f"Upload verification failed for {new_key}")

	update = {"file_url": s3_utils._build_file_url(new_key), "s3_key": new_key}

	# Migrate a local thumbnail too, if any.
	old_thumb_url = None
	thumb_url = doc.get("thumbnail_url")
	if thumb_url and "/files/" in thumb_url and not s3_utils._extract_key(thumb_url):
		thumb_path = _full_path(thumb_url)
		if thumb_path and os.path.exists(thumb_path):
			thumb_key = (
				s3_utils._swap_prefix(new_key, doc.is_private).rsplit("/", 1)[0]
				+ "/"
				+ os.path.basename(thumb_url)
			)
			thumb_type = mimetypes.guess_type(thumb_url)[0] or "image/png"
			s3.upload_file(thumb_path, bucket, thumb_key, ExtraArgs=_extra_args(settings, thumb_type))
			s3_utils._delete_on_rollback(bucket, [thumb_key])
			update["thumbnail_url"] = s3_utils._build_file_url(thumb_key)
			update["s3_thumbnail_key"] = thumb_key
			old_thumb_url = thumb_url

	doc.db_set(update, update_modified=False)
	# The File record now points at S3, but the linked document's own Attach /
	# Attach Image field still holds the old local URL — repoint it too. A failure
	# here aborts the file so it is retried (and stays local) instead of drifting.
	_update_attached_field(doc, old_main_url, update["file_url"])
	frappe.db.commit()

	if delete_local:
		# A shared upload (several File rows pointing at one local file) must keep the
		# local copy until the last referencing record has been migrated.
		_remove_local_if_unreferenced("file_url", old_main_url)
		if old_thumb_url:
			_remove_local_if_unreferenced("thumbnail_url", old_thumb_url)

	return "migrated"


def _extra_args(settings, content_type):
	# ExtraArgs for upload_file: storage class + content type (Content-MD5 is not
	# valid for multipart uploads and is omitted).
	extra = s3_utils._upload_extra_args(settings)
	extra["ContentType"] = content_type
	return extra


def _remove_local_if_unreferenced(field, url):
	if not url or frappe.db.exists("File", {field: url}):
		return
	path = _full_path(url)
	try:
		if path and os.path.exists(path):
			os.remove(path)
	except Exception as e:
		frappe.logger().error(f"S3 migration: could not remove local file {path}: {e}")


def _is_stored_field(doctype, fieldname):
	"""True only if ``fieldname`` is a real, stored DB column on ``doctype``.

	Guards against attachments linked to a field that is not a queryable column
	(a deleted field, a layout/no-value field, a virtual field, or a field that
	lives in a child table rather than the parent) — querying those raises
	"Unknown column".
	"""
	from frappe.model import no_value_fields

	try:
		df = frappe.get_meta(doctype).get_field(fieldname)
	except Exception:
		return False
	return bool(df) and df.fieldtype not in no_value_fields and not df.get("is_virtual")


def _update_attached_field(doc, old_url, new_url):
	"""Repoint the linked document's Attach / Attach Image field at the new URL.

	Only touches the field when it is a real column that still holds the exact old
	local URL. A genuine update failure is re-raised so the caller rolls the file
	back rather than deleting the local copy while the document still points at it.
	"""
	if not (doc.attached_to_doctype and doc.attached_to_name and doc.attached_to_field):
		return
	# Nothing to repoint if the link doesn't map to a stored column — skip quietly.
	if not _is_stored_field(doc.attached_to_doctype, doc.attached_to_field):
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
	except Exception:
		frappe.logger().exception(f"S3 migration: failed to update linked field for File {doc.name}")
		raise


# ---------------------------------------------------------------------------
# Moving a file back to local disk
# ---------------------------------------------------------------------------


def move_file_to_disk(row):
	"""Bring one S3-backed File back to the site's files folder.

	Used by the ``move_local_only_files_to_disk`` patch and whenever an attachment
	moves to a doctype the S3 scope excludes. ``row`` is a File document or any
	dict-like row carrying name / file_name / file_url / s3_key / is_private and
	the ``attached_to_*`` fields.

	The object is dropped only after commit **and** only if nothing else points at
	it: an object shared with another File record — a deduplicated upload, an
	attachment copied by an Amend — stays in the bucket for the records that still
	use it, while this record becomes local.

	Only the file itself moves. A generated thumbnail keeps its own S3 object and
	key, so it is still served (and still removed with the record); it is a preview,
	not the attachment.
	"""
	key = row.get("s3_key") or s3_utils._extract_key(row.get("file_url"))
	content = s3_utils.read_file_from_s3(key)

	file_name, file_url = _write_local_copy(row, content)
	frappe.db.set_value(
		"File",
		row.name,
		{"file_name": file_name, "file_url": file_url, "s3_key": None},
		update_modified=False,
	)
	# The owning document still holds the S3 URL and is looked up by it — repoint
	# it to the local one. row.file_url is still the old (S3) value here.
	_update_attached_field(row, row.get("file_url"), file_url)

	s3_utils._delete_after_commit(s3_utils.get_bucket(), [key], check_references=True)
	return file_url


def _write_local_copy(row, content):
	folder = "private" if cint(row.get("is_private")) else "public"
	prefix = "/private/files/" if folder == "private" else "/files/"
	# Same sanitising Frappe applies in File.save_file_on_filesystem().
	file_name = re.sub(r"[/\\%?#]", "_", os.path.basename(row.get("file_name") or "")) or row.name

	if frappe.db.exists("File", {"file_url": prefix + file_name, "name": ["!=", row.name]}):
		# Another record already owns that name on disk — don't overwrite its content.
		stem, ext = os.path.splitext(file_name)
		file_name = f"{stem}-{row.name}{ext}"

	path = frappe.get_site_path(folder, "files", file_name)
	os.makedirs(os.path.dirname(path), exist_ok=True)
	with open(path, "wb") as f:
		f.write(content)

	return file_name, prefix + file_name


# ---------------------------------------------------------------------------
# Keeping a file on the storage its doctype asks for
# ---------------------------------------------------------------------------
# A file's scope can only be read from the document it is attached to, and that
# link is not always there when the file is written: a Web Form uploads its
# attachment before the document exists, and an attachment can be re-attached or
# copied onto another doctype long afterwards. So the decision is re-made
# whenever a File is saved, and the file follows it — into S3 once its doctype is
# in scope, back to local disk when it moves to a doctype that is not.


def reevaluate_scope(doc, method=None):
	"""File ``on_update`` hook: queue a move when the file is on the wrong storage.

	Only ever does something while the doctype scope is restricted — without it
	nothing can be out of place. The move itself runs in a background job after
	commit: it talks to S3, and must not slow down (or fail) the save that
	triggered it.
	"""
	if doc.get("is_folder") or not doc.get("name"):
		return

	# Cheapest possible gate: this runs on every File save. get_single_value is
	# cached, so the common (unrestricted) case costs one cache read.
	if not cint(frappe.db.get_single_value("S3 Settings", "restrict_to_doctypes")):
		return

	settings = frappe.get_single("S3 Settings")
	if not settings.bucket_name:
		return

	in_s3 = bool(doc.get("s3_key") or s3_utils._extract_key(doc.get("file_url")))
	if in_s3 == s3_utils.should_store_in_s3(doc, settings=settings):
		return

	# Uploading is only possible from a file that is actually on this server; an
	# external http(s) attachment has nothing to move.
	if not in_s3 and not _is_local_url(doc.get("file_url")):
		return

	frappe.enqueue(
		"aws_s3_storage.aws_s3_storage.migrate.move_file_for_scope",
		queue="short",
		enqueue_after_commit=True,
		file_name=doc.name,
	)


def move_file_for_scope(file_name):
	"""Background job: put one file on the storage its doctype scope asks for.

	Re-checks everything: the job runs after commit, and the record may have
	changed (or been deleted) again in the meantime.
	"""
	if not frappe.db.exists("File", file_name):
		return

	settings = frappe.get_single("S3 Settings")
	if not s3_utils.is_scope_restricted(settings) or not settings.bucket_name:
		return

	doc = frappe.get_doc("File", file_name)
	if doc.is_folder:
		return

	key = doc.get("s3_key") or s3_utils._extract_key(doc.file_url)
	belongs_in_s3 = s3_utils.should_store_in_s3(doc, settings=settings)

	if belongs_in_s3 and not key:
		if not _is_local_url(doc.file_url):
			return
		result = migrate_file(
			file_name,
			delete_local=cint(settings.get("auto_delete_after_migration")),
			settings=settings,
		)
		frappe.logger().info(f"S3 scope: {file_name} moved into S3 ({result})")
	elif key and not belongs_in_s3:
		move_file_to_disk(doc)
		frappe.db.commit()
		frappe.logger().info(f"S3 scope: {file_name} moved back to local disk")


def _is_local_url(file_url):
	return bool(file_url) and (file_url.startswith("/files/") or file_url.startswith("/private/files/"))


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
	confirming the S3 object exists **with a matching size**.
	"""
	batch_size = cint(batch_size) or 200
	removed = 0
	last = ""
	s3 = bucket = None
	while True:
		rows = frappe.get_all(
			"File",
			filters={"is_folder": 0, "s3_key": ["not in", ["", None]], "name": [">", last]},
			fields=["name", "file_name", "file_size", "is_private", "s3_key", "s3_thumbnail_key"],
			order_by="name asc",
			limit=batch_size,
		)
		if not rows:
			break
		if s3 is None:
			s3 = s3_utils.get_s3_client()
			bucket = s3_utils.get_bucket()
		for row in rows:
			last = row.name
			removed += _delete_migrated_local_copy(row, s3, bucket)
		frappe.db.commit()

	frappe.logger().info(f"S3 local cleanup finished: removed {removed} file(s)")
	return {"removed": removed}


def _delete_migrated_local_copy(row, s3, bucket):
	removed = 0
	if row.file_name:
		main_url = ("/private/files/" if row.is_private else "/files/") + row.file_name
		if _safe_remove_local(main_url, row.s3_key, s3, bucket, expected_size=row.file_size):
			removed += 1
	# Thumbnails live under public/files; the S3 thumb key's basename is the original
	# local thumbnail filename.
	if row.get("s3_thumbnail_key"):
		thumb_url = "/files/" + os.path.basename(row.s3_thumbnail_key)
		if _safe_remove_local(thumb_url, row.s3_thumbnail_key, s3, bucket, field="thumbnail_url"):
			removed += 1
	return removed


def _safe_remove_local(local_url, s3_key, s3, bucket, expected_size=None, field="file_url"):
	path = _full_path(local_url)
	if not path or not os.path.exists(path):
		return False
	# A record still pointing at the local file needs it — keep it.
	if frappe.db.exists("File", {field: local_url}):
		return False
	# Verify the object is in S3 (and, for the main file, that the size matches).
	try:
		if not s3_utils.object_exists(s3_key, expected_size=expected_size, s3=s3, bucket=bucket):
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
