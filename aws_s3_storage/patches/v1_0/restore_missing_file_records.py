# Copyright (c) 2026, Innomate LLC
# For license information, please see license.txt
"""Recreate File records for S3 objects that only a document field still points at.

An attachment normally has two halves: the object in the bucket and a File record
describing it. A few flows leave the second half behind — a Web Form upload whose
File was never created, a value written straight into an Attach field with
``db_set`` / SQL, or a File deleted while another document still held its URL.

The URL keeps working for nobody: ``download_file`` resolves a private key to its
File record and refuses the request when there is none, so the link 403s for every
user, not just for a Guest. The document, meanwhile, still shows the field.

This patch walks every stored Attach / Attach Image field, and for each S3 link
with no File record left:

* recreates the record, attached to the document that references it, so read
  permission flows from that document exactly as it does for a normal attachment,
* or reports the link when the object is gone from the bucket as well — that one
  cannot be repaired here, the file has to be uploaded again.

Nothing is written to S3 and no URL is rewritten: only the missing rows come back.
Idempotent — a second run finds nothing to restore. It can be re-run at any time
with::

    bench --site <site> execute aws_s3_storage.patches.v1_0.restore_missing_file_records.execute
"""

import frappe

from aws_s3_storage.aws_s3_storage import s3_utils
from aws_s3_storage.patches.v1_0.repair_s3_file_urls import _Bucket


def execute():
	if not frappe.db.has_column("File", "s3_key"):
		# App installed but custom fields not created yet — nothing can be in S3.
		return

	settings = frappe.get_single("S3 Settings")
	if not settings.bucket_name:
		return

	bucket = _Bucket(settings)
	restored, orphaned, failed = 0, [], []

	for doctype, fieldname in s3_utils._attach_fields():
		for row in _linked_rows(doctype, fieldname):
			key = s3_utils._extract_key(row.value)
			if not key or not key.startswith(s3_utils.SERVABLE_PREFIXES):
				continue

			if s3_utils._files_for_key(key):
				# A File record still covers this object; permissions already resolve.
				continue

			where = f"{doctype} {row.name}.{fieldname}"

			if not s3_utils.object_exists(key, s3=bucket.s3, bucket=bucket.name):
				# Nothing to point a record at — recreating one would only turn a 403
				# into a 404. Report it so the field can be cleared or re-uploaded.
				orphaned.append(f"{where}: object is not in the bucket ({key})")
				continue

			try:
				_restore_file(doctype, fieldname, row, key)
				restored += 1
			except Exception as e:
				failed.append(f"{where}: {e}")

	if restored:
		print(f"aws_s3_storage: restored {restored} missing File record(s)")
	for note in orphaned:
		print(f"aws_s3_storage: {note}")
	for note in failed:
		print(f"aws_s3_storage: could not restore {note}")


def _restore_file(doctype, fieldname, row, key):
	"""Insert the File record the attachment lost, linked to its document.

	``is_private`` is read off the key's prefix by S3File.set_is_private (Frappe's
	own version derives it from a "/private" URL, which no S3 URL has). The
	uploader cannot be recovered, so the record inherits the referencing
	document's owner — the closest the remaining data gets to the truth.
	"""
	frappe.get_doc(
		{
			"doctype": "File",
			"file_name": key.rsplit("/", 1)[-1],
			"file_url": s3_utils._build_file_url(key),
			"s3_key": key,
			"is_private": 1 if key.startswith("private/") else 0,
			"attached_to_doctype": doctype,
			"attached_to_name": row.name,
			"attached_to_field": fieldname,
			"folder": "Home/Attachments",
			"owner": row.owner,
		}
	).insert(ignore_permissions=True)
	# Commit per record: one document that refuses the insert (an attachment limit,
	# a mandatory validation on its own doctype) must not discard the rest of the run.
	frappe.db.commit()


def _linked_rows(doctype, fieldname):
	"""Rows whose Attach field holds one of this app's download URLs.

	The identifiers come from the schema (and the column was just verified to
	exist), never from a request, so they are safe to interpolate.
	"""
	try:
		return frappe.db.sql(
			f"""
			SELECT `name`, `owner`, `{fieldname}` AS `value`
			FROM `tab{doctype}`
			WHERE `{fieldname}` LIKE %(pattern)s
			""",
			{"pattern": f"%{s3_utils.DOWNLOAD_METHOD}%"},
			as_dict=True,
		)
	except Exception as e:
		print(f"aws_s3_storage: could not scan {doctype}.{fieldname}: {e}")
		return []
