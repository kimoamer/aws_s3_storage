# Copyright (c) 2026, Innomate LLC
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import cint

MIN_PRESIGNED_URL_EXPIRY = 60
MAX_PRESIGNED_URL_EXPIRY = 604800  # 7 days
DEFAULT_PRESIGNED_URL_EXPIRY = 3600


class S3Settings(Document):
	def validate(self):
		self.endpoint_url = (self.endpoint_url or "").strip()

		expiry = cint(self.presigned_url_expiry) or DEFAULT_PRESIGNED_URL_EXPIRY
		self.presigned_url_expiry = max(MIN_PRESIGNED_URL_EXPIRY, min(expiry, MAX_PRESIGNED_URL_EXPIRY))

		# Credentials are optional (IAM role), but a key id without a secret — or vice
		# versa — is a misconfiguration that would silently fall back to the IAM role.
		has_secret = bool(self.get_password("secret_access_key", raise_exception=False))
		if bool(self.access_key_id) != has_secret:
			frappe.throw(
				"Provide both Access Key ID and Secret Access Key, or leave both blank to use an IAM role."
			)
