# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

# Custom fields added to the core File doctype to store the canonical S3 object
# keys. Indexed so the download endpoint can resolve permissions with an exact
# lookup instead of a LIKE scan on file_url.
FILE_CUSTOM_FIELDS = {
	"File": [
		{
			"fieldname": "s3_key",
			"label": "S3 Key",
			"fieldtype": "Data",
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
			"read_only": 1,
			"hidden": 1,
			"no_copy": 1,
			"search_index": 1,
			"insert_after": "s3_key",
		},
	]
}


def after_install():
	create_custom_fields(FILE_CUSTOM_FIELDS, ignore_validate=True)


def after_migrate():
	# Idempotent: create_custom_fields updates existing fields in place.
	create_custom_fields(FILE_CUSTOM_FIELDS, ignore_validate=True)
