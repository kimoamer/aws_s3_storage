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

		self._validate_doctype_scope()

	def _validate_doctype_scope(self):
		"""Tidy the scope list and warn — never block — on a configuration that
		silently keeps files local: a doctype name that does not exist (a typo, or an
		app that was uninstalled) and a restriction that covers nothing at all.

		Warnings rather than a throw on purpose: the form must stay saveable so the
		restriction can always be turned off again.
		"""
		names = []
		for line in (self.scoped_doctypes or "").replace(",", "\n").splitlines():
			name = line.strip()
			if name and name not in names:
				names.append(name)
		self.scoped_doctypes = "\n".join(names)

		if not cint(self.restrict_to_doctypes):
			return

		unknown = [name for name in names if not frappe.db.exists("DocType", name)]
		if unknown:
			frappe.msgprint(
				"These doctypes do not exist, so their files will stay on local disk: " + ", ".join(unknown),
				title="Unknown doctype",
				indicator="orange",
			)

		if not names and not cint(self.include_unattached_files):
			frappe.msgprint(
				"S3 storage is limited to specific doctypes but none are listed — "
				"no new upload will be stored in S3.",
				title="Nothing is in scope",
				indicator="orange",
			)
