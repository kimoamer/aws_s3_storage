# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""Migrate files that already live on local disk into S3.

The app only sends *new* uploads to S3; files that existed before installation
stay on disk. This tool moves them in resumable batches so you can actually
reclaim server disk space.

Run it from the UI (S3 Settings -> Migrate Local Files) or the bench console:

    bench --site <site> execute \
        aws_s3_storage.aws_s3_storage.migrate.run_migration

Every migrated File gets its ``s3_key`` set, so the pending query naturally
excludes it — the job is safe to stop and resume at any time.
"""

import mimetypes
import os

import frappe
from frappe.utils import cint

from aws_s3_storage.aws_s3_storage import s3_utils


def _pending_local_files(limit):
	return frappe.get_all(
		"File",
		filters={
			"is_folder": 0,
			"s3_key": ["in", ["", None]],
			"file_url": ["like", "%/files/%"],
		},
		pluck="name",
		limit=limit,
		order_by="creation asc",
	)


def count_pending():
	return frappe.db.count(
		"File",
		{
			"is_folder": 0,
			"s3_key": ["in", ["", None]],
			"file_url": ["like", "%/files/%"],
		},
	)


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

	while True:
		names = _pending_local_files(batch_size)
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

		frappe.db.commit()

	frappe.logger().info(f"S3 migration finished: {totals}")
	return totals


def migrate_file(name, delete_local=1):
	"""Migrate a single File record to S3. Returns migrated/skipped/missing."""
	doc = frappe.get_doc("File", name)

	if doc.is_folder or doc.get("s3_key") or s3_utils._extract_key(doc.file_url):
		return "skipped"

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

	# Remember local paths before we rewrite the URLs.
	local_paths = [_full_path(doc.file_url)]

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
			local_paths.append(thumb_path)

	doc.db_set(update, update_modified=False)
	frappe.db.commit()

	if delete_local:
		for path in local_paths:
			try:
				if path and os.path.exists(path):
					os.remove(path)
			except Exception as e:
				frappe.logger().error(f"S3 migration: could not remove local file {path}: {e}")

	return "migrated"


def _full_path(file_url):
	if not file_url:
		return None
	if file_url.startswith("/private/files/"):
		return frappe.get_site_path("private", "files", os.path.basename(file_url))
	if file_url.startswith("/files/"):
		return frappe.get_site_path("public", "files", os.path.basename(file_url))
	return None
