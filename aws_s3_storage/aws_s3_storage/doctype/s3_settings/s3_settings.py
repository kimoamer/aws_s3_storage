# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

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
