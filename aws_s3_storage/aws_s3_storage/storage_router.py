"""Route filesystem-dependent internal files away from S3 storage."""

from frappe.model.document import Document

from aws_s3_storage.aws_s3_storage.s3_utils import write_file_to_s3


# ERPNext rewrites these attachments in place using ``open(path, "wb")``.
# They therefore need a real local filesystem path instead of an S3 download URL.
LOCAL_ONLY_ATTACHMENTS = {
	("Repost Item Valuation", "reposting_data_file"),
}


def should_store_locally(file_or_fname) -> bool:
	"""Return whether a File document must remain on the site filesystem."""
	if not isinstance(file_or_fname, Document):
		return False

	attachment_target = (
		file_or_fname.get("attached_to_doctype"),
		file_or_fname.get("attached_to_field"),
	)
	return attachment_target in LOCAL_ONLY_ATTACHMENTS


def write_file(file_or_fname, content=None, content_type=None, is_private=0):
	"""Frappe write hook that keeps filesystem-dependent files local."""
	if should_store_locally(file_or_fname):
		return file_or_fname.save_file_on_filesystem()

	return write_file_to_s3(
		file_or_fname,
		content=content,
		content_type=content_type,
		is_private=is_private,
	)
