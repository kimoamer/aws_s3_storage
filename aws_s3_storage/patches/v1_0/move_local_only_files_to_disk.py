# Copyright (c) 2026, Innomate LLC
# For license information, please see license.txt
"""Bring attachments that must live on local disk back from S3.

ERPNext rewrites its reposting data file in place with
``open(file_doc.get_full_path(), "wb")`` after every batch, so a repost whose
attachment was uploaded to S3 fails with::

    FileNotFoundError: [Errno 2] No such file or directory:
    '/api/method/aws_s3_storage...download_file?key=private/.../repost_item_valuation-....json.gz'

New uploads of those files now stay local (``s3_utils.is_local_only_file``). This
patch fixes the ones already in the bucket: download each object, write it to the
site's files folder, repoint the File record and the document field that links to
it, then drop the now-unused object.

Idempotent — a second run finds nothing left to move.
"""

import os
import re

import frappe
from frappe.utils import cint

from aws_s3_storage.aws_s3_storage import migrate, s3_utils


def execute():
	if not frappe.db.has_column("File", "s3_key"):
		# App installed but custom fields not created yet — nothing can be in S3.
		return

	settings = frappe.get_single("S3 Settings")
	if not settings.bucket_name:
		return

	moved = failed = 0
	for row in _s3_backed_local_only_files():
		try:
			_move_to_disk(row)
			frappe.db.commit()
			moved += 1
		except Exception as e:
			# Never abort the whole migrate for one attachment: report it and move on.
			frappe.db.rollback()
			failed += 1
			frappe.logger().error(f"aws_s3_storage: could not move File {row.name} to disk: {e}")
			print(f"aws_s3_storage: could not move File {row.name} ({row.file_url}) to disk: {e}")

	if moved or failed:
		print(f"aws_s3_storage: moved {moved} attachment(s) back to local disk, {failed} failed")


def _s3_backed_local_only_files():
	"""File rows that must be local (see s3_utils) but currently point at S3."""
	rows = {}
	for filters in (
		{"attached_to_doctype": ["in", sorted(s3_utils.LOCAL_ONLY_ATTACHED_TO_DOCTYPES)]},
		{"attached_to_field": ["in", sorted(s3_utils.LOCAL_ONLY_ATTACHED_TO_FIELDS)]},
	):
		for row in frappe.get_all(
			"File",
			filters={"is_folder": 0, **filters},
			fields=[
				"name",
				"file_name",
				"file_url",
				"s3_key",
				"is_private",
				"attached_to_doctype",
				"attached_to_name",
				"attached_to_field",
			],
		):
			if row.s3_key or s3_utils._extract_key(row.file_url):
				rows[row.name] = row

	return list(rows.values())


def _move_to_disk(row):
	key = row.s3_key or s3_utils._extract_key(row.file_url)
	content = s3_utils.read_file_from_s3(key)

	file_name, file_url = _write_local_copy(row, content)
	frappe.db.set_value(
		"File",
		row.name,
		{"file_name": file_name, "file_url": file_url, "s3_key": None},
		update_modified=False,
	)
	# The owning document (e.g. Repost Item Valuation.reposting_data_file) still
	# holds the S3 URL and is looked up by it — repoint it to the local one.
	migrate._update_attached_field(row, row.file_url, file_url)

	# Only after the record is safely local: the object is no longer referenced.
	s3_utils._delete_after_commit(s3_utils.get_bucket(), [key], check_references=True)


def _write_local_copy(row, content):
	folder = "private" if cint(row.is_private) else "public"
	prefix = "/private/files/" if folder == "private" else "/files/"
	# Same sanitising Frappe applies in File.save_file_on_filesystem().
	file_name = re.sub(r"[/\\%?#]", "_", os.path.basename(row.file_name or "")) or row.name

	if frappe.db.exists("File", {"file_url": prefix + file_name, "name": ["!=", row.name]}):
		# Another record already owns that name on disk — don't overwrite its content.
		stem, ext = os.path.splitext(file_name)
		file_name = f"{stem}-{row.name}{ext}"

	path = frappe.get_site_path(folder, "files", file_name)
	os.makedirs(os.path.dirname(path), exist_ok=True)
	with open(path, "wb") as f:
		f.write(content)

	return file_name, prefix + file_name
