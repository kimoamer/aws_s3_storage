# Copyright (c) 2026, Innomate LLC
# For license information, please see license.txt

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

# Object keys can be long ("public/<uuid>/<filename>"), so the columns are wider
# than the default Data length (140) to avoid "Data too long for column 's3_key'".
S3_KEY_LENGTH = 500

# Custom fields added to the core File doctype to store the canonical S3 object
# keys. Indexed so the download endpoint can resolve permissions with an exact
# lookup instead of a LIKE scan on file_url.
FILE_CUSTOM_FIELDS = {
	"File": [
		{
			"fieldname": "s3_key",
			"label": "S3 Key",
			"fieldtype": "Data",
			"length": S3_KEY_LENGTH,
			"read_only": 1,
			"hidden": 1,
			"no_copy": 1,
			"search_index": 1,
			"insert_after": "file_url",
		},
		{
			"fieldname": "s3_thumbnail_key",
			"label": "S3 Thumbnail Key",
			"fieldtype": "Data",
			"length": S3_KEY_LENGTH,
			"read_only": 1,
			"hidden": 1,
			"no_copy": 1,
			"search_index": 1,
			"insert_after": "s3_key",
		},
	]
}


def after_install():
	_setup()


def after_migrate():
	# Idempotent: create_custom_fields updates existing fields in place.
	_setup()


def _setup():
	create_custom_fields(FILE_CUSTOM_FIELDS, ignore_validate=True)
	_ensure_key_column_length()


def _ensure_key_column_length():
	"""Widen the key columns on tabFile if an earlier install created them at the
	default 140. A Custom Field length change is not always applied to the DB
	column automatically, so enforce it here."""
	for column in ("s3_key", "s3_thumbnail_key"):
		try:
			rows = frappe.db.sql(
				"""SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS
				WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tabFile'
				AND COLUMN_NAME = %s""",
				column,
			)
			current = rows[0][0] if rows and rows[0] else None
			if current is not None and current < S3_KEY_LENGTH:
				frappe.db.sql_ddl(f"ALTER TABLE `tabFile` MODIFY COLUMN `{column}` varchar({S3_KEY_LENGTH})")
		except Exception:
			frappe.logger().error(f"aws_s3_storage: could not widen tabFile.{column}")
