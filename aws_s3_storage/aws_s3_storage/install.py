# Copyright (c) 2026, Innomate LLC
# For license information, please see license.txt

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from aws_s3_storage.aws_s3_storage import environment

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
		{
			# Which environment put this object in the bucket. Empty means "unknown"
			# (every file uploaded before this field existed), which the owning
			# environment is allowed to modify; a *different* environment's id is what
			# keeps a site that has adopted a shared bucket away from files it did not
			# create. See aws_s3_storage/environment.py.
			"fieldname": "s3_owner",
			"label": "S3 Storage Owner",
			"fieldtype": "Data",
			"length": 64,
			"read_only": 1,
			"hidden": 1,
			# Unlike the key columns, this is *not* no_copy: it describes the object,
			# so a record that inherits the object must inherit its owner too. A
			# copy that arrived without one is a record the guard would read as
			# "unknown owner" and let through (see S3File._backfill_s3_keys, which
			# recovers it from the record the key came from).
			"no_copy": 0,
			"search_index": 1,
			"insert_after": "s3_thumbnail_key",
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
	# Record this environment as the owner of the bucket — but only if nobody has
	# claimed it yet. A database restored from another site arrives with an owner
	# id already in it, so this never transfers ownership by accident; it reports
	# the mismatch instead. See aws_s3_storage/environment.py.
	environment.claim_if_unclaimed()


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
