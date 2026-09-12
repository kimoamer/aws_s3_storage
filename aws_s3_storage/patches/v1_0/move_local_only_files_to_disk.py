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

import frappe

from aws_s3_storage.aws_s3_storage import environment, migrate, s3_utils


def execute():
	if not frappe.db.has_column("File", "s3_key"):
		# App installed but custom fields not created yet — nothing can be in S3.
		return

	settings = frappe.get_single("S3 Settings")
	if not settings.bucket_name:
		return

	# Downloading every object and rewriting its record is a migration in its own
	# right, and on a restored copy of another site's database those records (and
	# objects) belong to that site. bench migrate runs patches automatically, so
	# without this a routine migrate on a test site would start reshuffling
	# production's attachments.
	if not environment.may_modify_storage(settings):
		print(f"aws_s3_storage: skipping local-only file move — {environment.blocked_reason(settings)}")
		return

	moved = failed = skipped = 0
	for row in _s3_backed_local_only_files():
		try:
			if migrate.move_file_to_disk(row) is None:
				skipped += 1
				continue
			frappe.db.commit()
			moved += 1
		except Exception as e:
			# Never abort the whole migrate for one attachment: report it and move on.
			frappe.db.rollback()
			failed += 1
			frappe.logger().error(f"aws_s3_storage: could not move File {row.name} to disk: {e}")
			print(f"aws_s3_storage: could not move File {row.name} ({row.file_url}) to disk: {e}")

	if moved or failed or skipped:
		print(
			f"aws_s3_storage: moved {moved} attachment(s) back to local disk, "
			f"{failed} failed, {skipped} skipped"
		)


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
