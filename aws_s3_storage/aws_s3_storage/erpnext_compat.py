"""Compatibility patches for ERPNext file workflows used with S3 storage."""

import gzip
from functools import wraps

import frappe

from aws_s3_storage.aws_s3_storage import s3_utils


_PATCH_MARKER = "_aws_s3_storage_repost_writer"


def apply_patches(*args, **kwargs):
	"""Install idempotent ERPNext patches in web and worker processes."""
	try:
		import erpnext.stock.stock_ledger as stock_ledger
	except ImportError:
		return

	current_writer = stock_ledger.create_json_gz_file
	if getattr(current_writer, _PATCH_MARKER, False):
		return

	original_writer = current_writer

	@wraps(original_writer)
	def create_json_gz_file(data, doc, file_name=None):
		"""Overwrite an existing repost working file directly on S3.

		ERPNext creates the file through the File doctype, then later rewrites it
		using ``open(file_doc.get_full_path(), "wb")``. An S3-backed File returns
		an API URL rather than a filesystem path, so the standard rewrite fails.
		The first creation still uses ERPNext's normal code path; only subsequent
		updates of an existing S3 object are intercepted here.
		"""
		if not file_name:
			return original_writer(data, doc, file_name)

		file_doc = frappe.get_doc("File", file_name)
		key = file_doc.get("s3_key") or s3_utils._extract_key(file_doc.file_url)
		if not key:
			return original_writer(data, doc, file_name)

		encoded_content = frappe.safe_encode(frappe.as_json(data))
		compressed_content = gzip.compress(encoded_content)
		settings = frappe.get_single("S3 Settings")

		s3_utils.get_s3_client().put_object(
			Bucket=settings.bucket_name,
			Key=key,
			Body=compressed_content,
			ContentType="application/gzip",
			**s3_utils._upload_extra_args(settings, compressed_content),
		)

		# Keep File metadata accurate without changing the attachment URL/key.
		file_doc.db_set("file_size", len(compressed_content), update_modified=False)
		return doc.reposting_data_file

	setattr(create_json_gz_file, _PATCH_MARKER, True)
	setattr(create_json_gz_file, "_aws_s3_storage_original", original_writer)
	stock_ledger.create_json_gz_file = create_json_gz_file
