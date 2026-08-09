# Copyright (c) 2026, Innomate LLC
# See license.txt

import base64
import hashlib
import os
import tempfile
from contextlib import contextmanager
from io import BytesIO
from unittest.mock import MagicMock, patch

import frappe
from botocore.exceptions import ClientError
from frappe.core.doctype.file.file import File
from frappe.tests.utils import FrappeTestCase

from aws_s3_storage.aws_s3_storage import s3_utils


class TestS3Settings(FrappeTestCase):
	def setUp(self):
		frappe.db.set_single_value("S3 Settings", "bucket_name", "test-bucket")
		frappe.db.set_single_value("S3 Settings", "region", "us-east-1")
		frappe.db.set_single_value("S3 Settings", "storage_class", "STANDARD")
		frappe.db.set_single_value("S3 Settings", "verify_upload_integrity", 0)
		frappe.db.set_single_value("S3 Settings", "presigned_url_expiry", 3600)
		frappe.db.set_single_value("S3 Settings", "enable_backup_sync", 0)
		frappe.db.set_single_value("S3 Settings", "allow_guest_downloads", 0)
		frappe.db.set_single_value("S3 Settings", "guest_upload_doctypes", "")

	# --- helpers -----------------------------------------------------------

	@contextmanager
	def _as_user(self, user):
		original = frappe.session.user
		frappe.session.user = user
		try:
			yield
		finally:
			frappe.session.user = original

	@contextmanager
	def _file_doc(self, file_doc):
		"""Fake only the File lookup: get_single("S3 Settings") goes through get_doc too."""
		real_get_doc = frappe.get_doc

		def fake_get_doc(*args, **kwargs):
			if args and args[0] == "File":
				return file_doc
			return real_get_doc(*args, **kwargs)

		with patch.object(frappe, "get_doc", side_effect=fake_get_doc):
			yield

	# --- URL / key helpers -------------------------------------------------

	def test_key_roundtrips_through_file_url(self):
		key = "private/abc123/my report.pdf"
		url = s3_utils._build_file_url(key)
		self.assertTrue(url.startswith("/api/method/"))  # relative, domain-independent
		self.assertEqual(s3_utils._extract_key(url), key)

	def test_extract_key_ignores_non_s3_urls(self):
		self.assertIsNone(s3_utils._extract_key("/files/foo.png"))
		self.assertIsNone(s3_utils._extract_key(None))

	def test_build_file_url_keeps_slashes_unencoded(self):
		url = s3_utils._build_file_url("public/uid/fb.png")
		self.assertIn("key=public/uid/fb.png", url)
		self.assertNotIn("%2F", url)

	def test_content_disposition_is_latin1_safe_for_arabic(self):
		cd = s3_utils._content_disposition("03- السجل تجاري.pdf")
		# Must be representable as a latin-1 HTTP header (no raw non-ASCII bytes).
		cd.encode("latin-1")
		self.assertIn("filename*=UTF-8''", cd)
		self.assertNotIn("السجل", cd)  # the non-ASCII part is percent-encoded

	def test_normalize_key_strips_double_encoding(self):
		self.assertEqual(s3_utils._normalize_key("public/uid/fb.png"), "public/uid/fb.png")
		self.assertEqual(s3_utils._normalize_key("public%2Fuid%2Ffb.png"), "public/uid/fb.png")
		self.assertEqual(s3_utils._normalize_key("public%252Fuid%252Ffb.png"), "public/uid/fb.png")

	@patch.object(s3_utils, "get_s3_client")
	def test_download_accepts_encoded_key(self, mock_get_client):
		s3 = MagicMock()
		s3.generate_presigned_url.return_value = "https://signed.example/x"
		mock_get_client.return_value = s3

		s3_utils.download_file("public%2Fuid%2Ffb.png")

		_, kwargs = s3.generate_presigned_url.call_args
		self.assertEqual(kwargs["Params"]["Key"], "public/uid/fb.png")

	def test_swap_prefix(self):
		self.assertEqual(s3_utils._swap_prefix("public/u/f.png", 1), "private/u/f.png")
		self.assertEqual(s3_utils._swap_prefix("private/u/f.png", 0), "public/u/f.png")

	def test_new_key_normal_filename(self):
		key = s3_utils._new_key("report.pdf", 1)
		self.assertTrue(key.startswith("private/"))
		self.assertTrue(key.endswith("/report.pdf"))

	def test_new_key_is_bounded_and_keeps_extension(self):
		key = s3_utils._new_key("x" * 1000 + ".png", 0)
		self.assertLessEqual(len(key), s3_utils.MAX_KEY_LENGTH)
		self.assertTrue(key.startswith("public/"))
		self.assertTrue(key.endswith(".png"))

	# --- client / credentials ---------------------------------------------

	def test_client_uses_iam_role_when_no_keys(self):
		fake = frappe._dict(bucket_name="b", region="us-east-1", access_key_id="")
		fake.get_password = lambda *a, **k: None
		with (
			patch.object(frappe, "get_single", return_value=fake),
			patch.object(s3_utils.boto3, "client") as mock_client,
		):
			s3_utils.get_s3_client()
		_, kwargs = mock_client.call_args
		self.assertNotIn("aws_access_key_id", kwargs)
		self.assertNotIn("aws_secret_access_key", kwargs)

	def test_client_uses_static_keys_when_present(self):
		fake = frappe._dict(bucket_name="b", region="us-east-1", access_key_id="AKIA")
		fake.get_password = lambda *a, **k: "secret"
		with (
			patch.object(frappe, "get_single", return_value=fake),
			patch.object(s3_utils.boto3, "client") as mock_client,
		):
			s3_utils.get_s3_client()
		_, kwargs = mock_client.call_args
		self.assertEqual(kwargs["aws_access_key_id"], "AKIA")
		self.assertEqual(kwargs["aws_secret_access_key"], "secret")

	# --- master enable switch ----------------------------------------------

	def test_is_enabled_defaults_on(self):
		self.assertTrue(s3_utils._is_enabled(frappe._dict()))  # unset -> enabled
		self.assertTrue(s3_utils._is_enabled(frappe._dict(enabled=1)))
		self.assertFalse(s3_utils._is_enabled(frappe._dict(enabled=0)))

	def test_write_falls_back_to_local_when_disabled(self):
		frappe.db.set_single_value("S3 Settings", "enabled", 0)
		with (
			patch.object(s3_utils, "get_s3_client") as client,
			patch.object(s3_utils, "_save_to_filesystem", return_value={"file_url": "/files/x"}) as fallback,
		):
			result = s3_utils.write_file_to_s3("x.txt", b"data")
		client.assert_not_called()
		fallback.assert_called_once()
		self.assertEqual(result, {"file_url": "/files/x"})

	def test_write_falls_back_to_local_when_no_bucket(self):
		frappe.db.set_single_value("S3 Settings", "bucket_name", "")
		with (
			patch.object(s3_utils, "get_s3_client") as client,
			patch.object(s3_utils, "_save_to_filesystem", return_value={"file_url": "/files/x"}) as fallback,
		):
			s3_utils.write_file_to_s3("x.txt", b"data")
		client.assert_not_called()
		fallback.assert_called_once()

	# --- attachments that must stay on local disk --------------------------

	def test_is_local_only_file_detects_repost_attachment(self):
		# ERPNext rewrites this one in place by path, so it can never live in S3.
		self.assertTrue(
			s3_utils.is_local_only_file(frappe._dict(attached_to_doctype="Repost Item Valuation"))
		)
		self.assertTrue(s3_utils.is_local_only_file(frappe._dict(attached_to_field="reposting_data_file")))
		# Filename fallback, for callers that pass no attachment link at all.
		self.assertTrue(s3_utils.is_local_only_file(fname="repost_item_valuation-3a7f0c1.json.gz"))

	def test_is_local_only_file_false_for_normal_attachment(self):
		self.assertFalse(s3_utils.is_local_only_file(frappe._dict()))
		self.assertFalse(s3_utils.is_local_only_file(fname="invoice.pdf"))
		self.assertFalse(
			s3_utils.is_local_only_file(
				frappe._dict(
					attached_to_doctype="Sales Invoice",
					attached_to_field="scanned_copy",
					file_name="report.json.gz",
				)
			)
		)

	def test_write_keeps_repost_data_file_local(self):
		frappe.db.set_single_value("S3 Settings", "enabled", 1)
		doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "repost_item_valuation-3a7f0c1.json.gz",
				"attached_to_doctype": "Repost Item Valuation",
				"attached_to_name": "3a7f0c1",
				"attached_to_field": "reposting_data_file",
				"is_private": 1,
			}
		)
		with (
			patch.object(s3_utils, "get_s3_client") as client,
			patch.object(
				s3_utils, "_save_to_filesystem", return_value={"file_url": "/private/files/x.json.gz"}
			) as fallback,
		):
			result = s3_utils.write_file_to_s3(doc)

		client.assert_not_called()
		fallback.assert_called_once()
		self.assertEqual(result["file_url"], "/private/files/x.json.gz")

	def test_local_only_file_skips_dedup(self):
		# Dedup runs before the write_file hook: reusing an existing (S3) object here
		# would hand the reposting file an /api/method URL it can't open by path.
		from aws_s3_storage.aws_s3_storage.file_override import S3File

		f = S3File(
			{
				"doctype": "File",
				"file_name": "repost_item_valuation-3a7f0c1.json.gz",
				"attached_to_doctype": "Repost Item Valuation",
				"attached_to_field": "reposting_data_file",
				"is_private": 1,
			}
		)
		with patch.object(File, "save_file") as parent:
			f.save_file(content=b"x")
		self.assertTrue(parent.call_args.kwargs["ignore_existing_file_check"])

	def test_normal_file_keeps_dedup(self):
		from aws_s3_storage.aws_s3_storage.file_override import S3File

		f = S3File({"doctype": "File", "file_name": "invoice.pdf", "is_private": 1})
		with patch.object(File, "save_file") as parent:
			f.save_file(content=b"x")
		self.assertFalse(parent.call_args.kwargs.get("ignore_existing_file_check"))

	def test_migration_skips_local_only_file(self):
		# The daily migration must not move a local-only file into S3 later on.
		from aws_s3_storage.aws_s3_storage import migrate

		doc = frappe._dict(
			name="F1",
			is_folder=0,
			s3_key=None,
			file_url="/private/files/repost_item_valuation-3a7f0c1.json.gz",
			file_name="repost_item_valuation-3a7f0c1.json.gz",
			attached_to_doctype="Repost Item Valuation",
			attached_to_field="reposting_data_file",
		)
		with patch.object(frappe, "get_doc", return_value=doc):
			self.assertEqual(migrate.migrate_file("F1"), "skipped")

	# --- write_file_to_s3 --------------------------------------------------

	@patch.object(s3_utils, "get_s3_client")
	def test_write_public_file_sets_no_acl(self, mock_get_client):
		s3 = MagicMock()
		mock_get_client.return_value = s3

		result = s3_utils.write_file_to_s3("hello.txt", b"data", content_type="text/plain", is_private=0)

		self.assertEqual(result["file_size"], 4)
		key = result["s3_key"]
		self.assertTrue(key.startswith("public/"))
		self.assertTrue(key.endswith("/hello.txt"))
		_, kwargs = s3.put_object.call_args
		self.assertNotIn("ACL", kwargs)
		self.assertEqual(kwargs["Key"], key)

	@patch.object(s3_utils, "get_s3_client")
	def test_write_registers_rollback_cleanup(self, mock_get_client):
		# An object uploaded in a transaction that later rolls back must be removed.
		s3 = MagicMock()
		mock_get_client.return_value = s3

		captured = []
		with patch.object(frappe.db.after_rollback, "add", side_effect=captured.append):
			result = s3_utils.write_file_to_s3("f.txt", b"data")

		self.assertEqual(len(captured), 1)
		captured[0]()  # simulate rollback firing
		s3.delete_object.assert_called_once_with(Bucket="test-bucket", Key=result["s3_key"])

	@patch.object(s3_utils, "get_s3_client")
	def test_write_sends_content_md5_when_enabled(self, mock_get_client):
		frappe.db.set_single_value("S3 Settings", "verify_upload_integrity", 1)
		s3 = MagicMock()
		mock_get_client.return_value = s3

		content = b"integrity-please"
		s3_utils.write_file_to_s3("f.bin", content)

		_, kwargs = s3.put_object.call_args
		expected = base64.b64encode(hashlib.md5(content, usedforsecurity=False).digest()).decode()
		self.assertEqual(kwargs["ContentMD5"], expected)

	@patch.object(s3_utils, "get_s3_client")
	def test_write_applies_storage_class(self, mock_get_client):
		frappe.db.set_single_value("S3 Settings", "storage_class", "STANDARD_IA")
		s3 = MagicMock()
		mock_get_client.return_value = s3

		s3_utils.write_file_to_s3("f.bin", b"data")

		_, kwargs = s3.put_object.call_args
		self.assertEqual(kwargs["StorageClass"], "STANDARD_IA")

	# --- download_file (access control) ------------------------------------

	@patch.object(s3_utils, "get_s3_client")
	def test_download_public_redirects(self, mock_get_client):
		frappe.db.set_single_value("S3 Settings", "presigned_url_expiry", 120)
		s3 = MagicMock()
		s3.generate_presigned_url.return_value = "https://signed.example/x"
		mock_get_client.return_value = s3

		s3_utils.download_file("public/xyz/pic.png")

		_, kwargs = s3.generate_presigned_url.call_args
		self.assertEqual(kwargs["ExpiresIn"], 120)
		self.assertEqual(frappe.local.response["location"], "https://signed.example/x")

	def test_download_rejects_backup_key(self):
		# The critical fix: a guest must not be able to presign a backup object.
		with self.assertRaises(frappe.PermissionError):
			s3_utils.download_file("backups/site1/database.sql.gz")

	def test_download_rejects_arbitrary_or_empty_key(self):
		for bad in ("secret/whatever", "", "../etc/passwd", "private"):
			with self.assertRaises(frappe.PermissionError):
				s3_utils.download_file(bad)

	@patch.object(s3_utils, "get_s3_client")
	def test_download_private_checks_read_permission(self, mock_get_client):
		s3 = MagicMock()
		s3.generate_presigned_url.return_value = "https://signed.example/thumb"
		mock_get_client.return_value = s3

		file_doc = frappe._dict(owner="Administrator", has_permission=lambda ptype: True)

		with (
			patch.object(s3_utils, "_files_for_key", return_value=["FILE-1"]),
			self._file_doc(file_doc),
		):
			s3_utils.download_file("private/uid/pic_small.png")

		self.assertEqual(frappe.local.response["location"], "https://signed.example/thumb")

	def test_download_private_refuses_without_read_permission(self):
		file_doc = frappe._dict(owner="Administrator", has_permission=lambda ptype: False)

		with (
			patch.object(s3_utils, "_files_for_key", return_value=["FILE-1"]),
			self._file_doc(file_doc),
			self.assertRaises(frappe.PermissionError),
		):
			s3_utils.download_file("private/uid/secret.pdf")

	def test_download_private_refuses_unknown_key(self):
		with (
			patch.object(s3_utils, "_files_for_key", return_value=[]),
			self.assertRaises(frappe.PermissionError),
		):
			s3_utils.download_file("private/uid/not-a-record.pdf")

	# --- legacy records without the s3_key column --------------------------

	def test_files_for_key_falls_back_to_url_match(self):
		# Uploaded before s3_key existed: the key lives only inside file_url, so the
		# exact-column lookup finds nothing and the record must still resolve.
		key = "private/uid123/report.pdf"
		rows = [frappe._dict(name="FILE-1", file_url=s3_utils._build_file_url(key), thumbnail_url=None)]

		with (
			patch.object(frappe.db, "sql_list", return_value=[]),
			patch.object(frappe.db, "sql", return_value=rows),
		):
			self.assertEqual(s3_utils._files_for_key(key), ["FILE-1"])

	def test_files_for_key_drops_rows_the_like_only_narrowed(self):
		# The LIKE matches on the uuid alone; a different object under the same uuid
		# (e.g. its thumbnail's sibling) must not count as this key.
		key = "private/uid123/report.pdf"
		rows = [
			frappe._dict(
				name="FILE-2",
				file_url=s3_utils._build_file_url("private/uid123/other.pdf"),
				thumbnail_url=None,
			)
		]

		with (
			patch.object(frappe.db, "sql_list", return_value=[]),
			patch.object(frappe.db, "sql", return_value=rows),
		):
			self.assertEqual(s3_utils._files_for_key(key), [])

	def test_files_for_key_prefers_indexed_columns(self):
		with (
			patch.object(frappe.db, "sql_list", return_value=["FILE-1"]),
			patch.object(frappe.db, "sql") as fallback,
		):
			self.assertEqual(s3_utils._files_for_key("private/uid/f.pdf"), ["FILE-1"])
		fallback.assert_not_called()

	# --- guest (web form) uploads ------------------------------------------

	def test_guest_may_read_own_upload_when_enabled(self):
		settings = frappe._dict(allow_guest_downloads=1, guest_upload_doctypes="")
		file_doc = frappe._dict(owner="Guest", attached_to_doctype="Job Applicant")

		with self._as_user("Guest"):
			self.assertTrue(s3_utils._guest_may_read(file_doc, settings))

	def test_guest_may_read_is_off_by_default(self):
		settings = frappe._dict(allow_guest_downloads=0, guest_upload_doctypes="")
		file_doc = frappe._dict(owner="Guest", attached_to_doctype="Job Applicant")

		with self._as_user("Guest"):
			self.assertFalse(s3_utils._guest_may_read(file_doc, settings))

	def test_guest_may_not_read_someone_elses_file(self):
		# The rule only ever covers what the visitor uploaded themselves.
		settings = frappe._dict(allow_guest_downloads=1, guest_upload_doctypes="")
		file_doc = frappe._dict(owner="Administrator", attached_to_doctype="Sales Invoice")

		with self._as_user("Guest"):
			self.assertFalse(s3_utils._guest_may_read(file_doc, settings))

	def test_guest_rule_never_applies_to_a_logged_in_user(self):
		settings = frappe._dict(allow_guest_downloads=1, guest_upload_doctypes="")
		file_doc = frappe._dict(owner="Guest", attached_to_doctype="")

		with self._as_user("test@example.com"):
			self.assertFalse(s3_utils._guest_may_read(file_doc, settings))

	def test_guest_doctype_allowlist_is_enforced(self):
		settings = frappe._dict(allow_guest_downloads=1, guest_upload_doctypes="Job Applicant\n Lead ")

		with self._as_user("Guest"):
			self.assertTrue(
				s3_utils._guest_may_read(frappe._dict(owner="Guest", attached_to_doctype="Lead"), settings)
			)
			self.assertFalse(
				s3_utils._guest_may_read(
					frappe._dict(owner="Guest", attached_to_doctype="Sales Invoice"), settings
				)
			)
			# Not attached yet: the state of every upload before the form is submitted,
			# which is exactly the preview this rule exists for.
			self.assertTrue(
				s3_utils._guest_may_read(frappe._dict(owner="Guest", attached_to_doctype=""), settings)
			)

	@patch.object(s3_utils, "get_s3_client")
	def test_download_serves_guest_upload_when_enabled(self, mock_get_client):
		frappe.db.set_single_value("S3 Settings", "allow_guest_downloads", 1)
		s3 = MagicMock()
		s3.generate_presigned_url.return_value = "https://signed.example/webform"
		mock_get_client.return_value = s3

		# Frappe grants a Guest no read permission on a private File.
		file_doc = frappe._dict(owner="Guest", attached_to_doctype="", has_permission=lambda ptype: False)

		with (
			patch.object(s3_utils, "_files_for_key", return_value=["FILE-1"]),
			self._file_doc(file_doc),
			self._as_user("Guest"),
		):
			s3_utils.download_file("private/uid/cv.pdf")

		self.assertEqual(frappe.local.response["location"], "https://signed.example/webform")

	def test_download_refuses_guest_upload_when_disabled(self):
		file_doc = frappe._dict(owner="Guest", attached_to_doctype="", has_permission=lambda ptype: False)

		with (
			patch.object(s3_utils, "_files_for_key", return_value=["FILE-1"]),
			self._file_doc(file_doc),
			self._as_user("Guest"),
			self.assertRaises(frappe.PermissionError),
		):
			s3_utils.download_file("private/uid/cv.pdf")

	# --- is_private change -------------------------------------------------

	@patch.object(s3_utils, "get_s3_client")
	def test_move_object_privacy_copies_and_rewrites(self, mock_get_client):
		s3 = MagicMock()
		mock_get_client.return_value = s3

		old_key = "public/uid/f.pdf"
		doc = frappe._dict(file_url=s3_utils._build_file_url(old_key), is_private=1, thumbnail_url=None)
		s3_utils.move_object_privacy(doc)

		_, kwargs = s3.copy_object.call_args
		self.assertEqual(kwargs["Key"], "private/uid/f.pdf")
		self.assertEqual(kwargs["CopySource"], {"Bucket": "test-bucket", "Key": old_key})
		self.assertEqual(doc.s3_key, "private/uid/f.pdf")
		self.assertEqual(s3_utils._extract_key(doc.file_url), "private/uid/f.pdf")

	# --- deduplication + reference-checked deletion ------------------------

	def test_exists_on_disk_true_for_s3_file(self):
		from aws_s3_storage.aws_s3_storage.file_override import S3File

		f = S3File({"doctype": "File", "file_url": s3_utils._build_file_url("public/uid/f.png")})
		self.assertTrue(f.exists_on_disk())

	def test_get_full_path_returns_url_for_s3_file(self):
		# get_full_path must not push an /api/method URL through is_safe_path, which
		# would abort the File save with "Cannot access file path".
		from aws_s3_storage.aws_s3_storage.file_override import S3File

		url = s3_utils._build_file_url("public/uid/f.png")
		f = S3File({"doctype": "File", "file_url": url})
		self.assertEqual(f.get_full_path(), url)
		self.assertTrue(f.validate_file_on_disk())

	def test_is_remote_file_true_for_s3_url(self):
		# On older Frappe (URL_PREFIXES without /api/method), the app must classify
		# its own download URL as remote so validate_file_path/url short-circuit.
		from aws_s3_storage.aws_s3_storage.file_override import S3File

		f = S3File({"doctype": "File", "file_url": s3_utils._build_file_url("public/uid/f.png")})
		self.assertTrue(f.is_remote_file)

	@patch.object(s3_utils, "get_s3_client")
	def test_delete_skips_key_still_referenced(self, mock_get_client):
		s3 = MagicMock()
		mock_get_client.return_value = s3
		with patch.object(frappe.db, "exists", return_value=True):
			s3_utils._delete_keys("test-bucket", ["public/uid/f.png"], check_references=True)
		s3.delete_object.assert_not_called()

	@patch.object(s3_utils, "get_s3_client")
	def test_delete_runs_when_key_unreferenced(self, mock_get_client):
		s3 = MagicMock()
		mock_get_client.return_value = s3
		with patch.object(frappe.db, "exists", return_value=False):
			s3_utils._delete_keys("test-bucket", ["public/uid/f.png"], check_references=True)
		s3.delete_object.assert_called_once_with(Bucket="test-bucket", Key="public/uid/f.png")

	# --- deletion guard: documents that still link the object ---------------

	@contextmanager
	def _document_links(self, values, unscannable=False):
		"""One Attach field whose rows hold ``values``."""

		def fake_sql(query, params=None):
			if unscannable:
				raise Exception("Unknown column")
			return [(value,) for value in values]

		with (
			patch.object(
				s3_utils, "_attach_fields", return_value=[("Interview", "custom_resume_attachment")]
			),
			patch.object(frappe.db, "sql", side_effect=fake_sql),
		):
			yield

	def test_key_still_linked_from_a_document_is_referenced(self):
		# The Job Applicant's File is gone, but the Interview still shows the CV.
		key = "private/uid/cv.pdf"
		with self._document_links([s3_utils._build_file_url(key)]):
			self.assertTrue(s3_utils._key_is_linked_from_a_document(key))

	def test_document_link_to_a_different_object_is_not_a_reference(self):
		# Same uuid folder (the thumbnail lives there too) — only the LIKE matches.
		with self._document_links([s3_utils._build_file_url("private/uid/other.pdf")]):
			self.assertFalse(s3_utils._key_is_linked_from_a_document("private/uid/cv.pdf"))

	def test_unscannable_field_counts_as_a_reference(self):
		# Never delete on a check that could not be completed.
		with self._document_links([], unscannable=True):
			self.assertTrue(s3_utils._key_is_linked_from_a_document("private/uid/cv.pdf"))

	@patch.object(s3_utils, "get_s3_client")
	def test_delete_skips_key_a_document_still_links(self, mock_get_client):
		s3 = MagicMock()
		mock_get_client.return_value = s3
		key = "private/uid/cv.pdf"

		with (
			patch.object(frappe.db, "exists", return_value=False),
			patch.object(s3_utils, "_key_is_linked_from_a_document", return_value=True) as scan,
		):
			s3_utils._delete_keys("test-bucket", [key], check_references=True, check_documents=True)

		scan.assert_called_once_with(key)
		s3.delete_object.assert_not_called()

	@patch.object(s3_utils, "get_s3_client")
	def test_privacy_move_deletes_the_old_object_despite_a_document_link(self, mock_get_client):
		# The old public object must go even while a document still shows its URL,
		# otherwise a file just marked private stays readable under its public key.
		s3 = MagicMock()
		mock_get_client.return_value = s3

		with (
			patch.object(frappe.db, "exists", return_value=False),
			patch.object(frappe.db, "sql", return_value=[]),
			patch.object(s3_utils, "_key_is_linked_from_a_document") as scan,
		):
			s3_utils._delete_keys("test-bucket", ["public/uid/f.pdf"], check_references=True)

		scan.assert_not_called()
		s3.delete_object.assert_called_once_with(Bucket="test-bucket", Key="public/uid/f.pdf")

	@patch.object(s3_utils, "get_s3_client")
	def test_failed_delete_is_queued_for_retry(self, mock_get_client):
		s3 = MagicMock()
		s3.delete_object.side_effect = RuntimeError("network")
		mock_get_client.return_value = s3

		with patch.object(s3_utils, "_queue_deletion") as queue:
			s3_utils._delete_keys("test-bucket", ["public/uid/f.png"])
		queue.assert_called_once()

	def test_delete_falls_back_to_local_for_non_s3_files(self):
		recorded = {}

		class FakeFile:
			file_url = "/private/files/legacy.pdf"

			def delete_file_from_filesystem(self, only_thumbnail=False):
				recorded["only_thumbnail"] = only_thumbnail

		s3_utils.delete_file_from_s3(FakeFile())
		self.assertEqual(recorded.get("only_thumbnail"), False)

	# --- server-side read / thumbnails -------------------------------------

	@patch.object(s3_utils, "get_s3_client")
	def test_read_file_from_s3(self, mock_get_client):
		s3 = MagicMock()
		s3.get_object.return_value = {"Body": BytesIO(b"payload")}
		mock_get_client.return_value = s3

		self.assertEqual(s3_utils.read_file_from_s3("public/x/f.bin"), b"payload")
		s3.get_object.assert_called_once_with(Bucket="test-bucket", Key="public/x/f.bin")

	@patch.object(s3_utils, "get_s3_client")
	def test_make_thumbnail_stores_private_thumbnail_and_key(self, mock_get_client):
		from PIL import Image

		from aws_s3_storage.aws_s3_storage.file_override import S3File

		buf = BytesIO()
		Image.new("RGB", (10, 10), "red").save(buf, format="PNG")
		png = buf.getvalue()

		s3 = MagicMock()
		mock_get_client.return_value = s3

		key = "private/uid/pic.png"
		f = S3File({"doctype": "File", "file_name": "pic.png", "file_url": s3_utils._build_file_url(key)})
		f.db_set = MagicMock()

		with patch.object(s3_utils, "read_file_from_s3", return_value=png):
			f.make_thumbnail()

		_, kwargs = s3.put_object.call_args
		self.assertTrue(kwargs["Key"].startswith("private/uid/pic_small."))
		# db_set is called with a dict carrying both the url and the thumbnail key.
		f.db_set.assert_called_once()
		saved = f.db_set.call_args[0][0]
		self.assertIn("thumbnail_url", saved)
		self.assertIn("s3_thumbnail_key", saved)

	# --- dedup s3_key backfill / shared-object safety ----------------------

	def test_backfill_s3_keys_from_url(self):
		from aws_s3_storage.aws_s3_storage.file_override import S3File

		f = S3File(
			{
				"doctype": "File",
				"file_url": s3_utils._build_file_url("public/uid/f.png"),
				"thumbnail_url": s3_utils._build_file_url("public/uid/f_small.png"),
			}
		)
		f._backfill_s3_keys()
		self.assertEqual(f.s3_key, "public/uid/f.png")
		self.assertEqual(f.s3_thumbnail_key, "public/uid/f_small.png")

	@patch.object(s3_utils, "get_s3_client")
	def test_delete_skips_key_referenced_as_thumbnail(self, mock_get_client):
		s3 = MagicMock()
		mock_get_client.return_value = s3

		def exists(doctype, filters):
			return "s3_thumbnail_key" in filters  # still referenced as someone's thumbnail

		with patch.object(frappe.db, "exists", side_effect=exists):
			s3_utils._delete_keys("test-bucket", ["public/uid/f.png"], check_references=True)
		s3.delete_object.assert_not_called()

	# --- migration safety --------------------------------------------------

	def test_scheduled_migration_noop_when_disabled(self):
		from aws_s3_storage.aws_s3_storage import migrate

		frappe.db.set_single_value("S3 Settings", "enable_scheduled_migration", 0)
		with patch.object(migrate, "run_migration") as run_migration:
			migrate.scheduled_migration()
		run_migration.assert_not_called()

	def test_run_migration_does_not_loop_on_missing(self):
		from aws_s3_storage.aws_s3_storage import migrate

		names = ["F1", "F2"]
		processed = []

		def fake_pending(limit, exclude=None):
			exclude = set(exclude or [])
			return [n for n in names if n not in exclude]

		def fake_migrate(name, delete_local=1):
			processed.append(name)
			return "missing"

		with (
			patch.object(migrate, "_pending_local_files", side_effect=fake_pending),
			patch.object(migrate, "migrate_file", side_effect=fake_migrate),
			patch.object(frappe.db, "commit"),
		):
			totals = migrate.run_migration()

		# Each file processed exactly once, then the run terminates (no infinite loop).
		self.assertEqual(processed, ["F1", "F2"])
		self.assertEqual(totals["missing"], 2)

	def test_update_attached_field_repoints_matching_value(self):
		from aws_s3_storage.aws_s3_storage import migrate

		doc = frappe._dict(attached_to_doctype="ToDo", attached_to_name="T1", attached_to_field="image")
		saved = {}
		with (
			patch.object(migrate, "_is_stored_field", return_value=True),
			patch.object(frappe.db, "get_value", return_value="/files/old.png"),
			patch.object(frappe.db, "set_value", side_effect=lambda dt, dn, f, v, **k: saved.update(v=v)),
		):
			migrate._update_attached_field(doc, "/files/old.png", "/api/method/x?key=public/u/old.png")
		self.assertEqual(saved.get("v"), "/api/method/x?key=public/u/old.png")

	def test_update_attached_field_skips_on_mismatch(self):
		from aws_s3_storage.aws_s3_storage import migrate

		doc = frappe._dict(attached_to_doctype="ToDo", attached_to_name="T1", attached_to_field="image")
		with (
			patch.object(migrate, "_is_stored_field", return_value=True),
			patch.object(frappe.db, "get_value", return_value="/files/something-else.png"),
			patch.object(frappe.db, "set_value") as set_value,
		):
			migrate._update_attached_field(doc, "/files/old.png", "/api/method/x?key=public/u/old.png")
		set_value.assert_not_called()

	def test_update_attached_field_skips_when_field_not_a_column(self):
		# The real-world "Unknown column 'file'" case: linked field is not a stored
		# column — skip quietly instead of failing the whole file.
		from aws_s3_storage.aws_s3_storage import migrate

		doc = frappe._dict(attached_to_doctype="ToDo", attached_to_name="T1", attached_to_field="file")
		with (
			patch.object(migrate, "_is_stored_field", return_value=False),
			patch.object(frappe.db, "get_value") as get_value,
			patch.object(frappe.db, "set_value") as set_value,
		):
			migrate._update_attached_field(doc, "/files/old.png", "/api/method/x")
		get_value.assert_not_called()
		set_value.assert_not_called()

	def test_record_error_inserts_row(self):
		from aws_s3_storage.aws_s3_storage import migrate

		inserted = {}

		class FakeDoc:
			def insert(self, ignore_permissions=False):
				inserted["done"] = True

		with (
			patch.object(frappe.db, "get_value", return_value="/files/x"),
			patch.object(frappe, "get_doc", return_value=FakeDoc()) as get_doc,
			patch.object(frappe.db, "commit"),
		):
			migrate._record_error("F1", "Failed", "boom")

		payload = get_doc.call_args[0][0]
		self.assertEqual(payload["doctype"], "S3 Migration Error")
		self.assertEqual(payload["file"], "F1")
		self.assertEqual(payload["reason"], "Failed")
		self.assertEqual(payload["error"], "boom")
		self.assertTrue(inserted["done"])

	def test_update_attached_field_raises_on_failure(self):
		# A failed field update must propagate so the file is rolled back, not
		# silently treated as migrated.
		from aws_s3_storage.aws_s3_storage import migrate

		doc = frappe._dict(
			attached_to_doctype="ToDo", attached_to_name="T1", attached_to_field="image", name="F1"
		)
		with (
			patch.object(migrate, "_is_stored_field", return_value=True),
			patch.object(frappe.db, "get_value", return_value="/files/old.png"),
			patch.object(frappe.db, "set_value", side_effect=RuntimeError("boom")),
			self.assertRaises(RuntimeError),
		):
			migrate._update_attached_field(doc, "/files/old.png", "/api/method/x")

	def test_cleanup_keeps_local_when_size_mismatches(self):
		from aws_s3_storage.aws_s3_storage import migrate

		tmp = tempfile.NamedTemporaryFile(delete=False)
		tmp.write(b"data")
		tmp.close()
		try:
			with (
				patch.object(migrate, "_full_path", return_value=tmp.name),
				patch.object(frappe.db, "exists", return_value=False),
				patch.object(s3_utils, "object_exists", return_value=False) as obj_exists,
			):
				removed = migrate._safe_remove_local(
					"/files/f.png", "public/u/f.png", MagicMock(), "b", expected_size=10
				)
			self.assertFalse(removed)
			self.assertTrue(os.path.exists(tmp.name))  # not deleted
			self.assertEqual(obj_exists.call_args.kwargs.get("expected_size"), 10)
		finally:
			os.unlink(tmp.name)

	def test_cleanup_removes_local_when_verified(self):
		from aws_s3_storage.aws_s3_storage import migrate

		tmp = tempfile.NamedTemporaryFile(delete=False)
		tmp.write(b"data")
		tmp.close()
		try:
			with (
				patch.object(migrate, "_full_path", return_value=tmp.name),
				patch.object(frappe.db, "exists", return_value=False),
				patch.object(s3_utils, "object_exists", return_value=True),
			):
				removed = migrate._safe_remove_local("/files/f.png", "public/u/f.png", MagicMock(), "b")
			self.assertTrue(removed)
			self.assertFalse(os.path.exists(tmp.name))  # deleted
		finally:
			if os.path.exists(tmp.name):
				os.unlink(tmp.name)

	# --- restoring File records a document still links to -------------------

	@contextmanager
	def _linked_rows(self, rows, covered_by=(), object_exists=True):
		"""Run the patch over ``rows`` of one Attach field, faking every lookup."""
		from aws_s3_storage.patches.v1_0 import restore_missing_file_records as repair

		with (
			patch.object(
				s3_utils, "_attach_fields", return_value=[("Interview", "custom_resume_attachment")]
			),
			patch.object(repair, "_linked_rows", return_value=rows),
			patch.object(s3_utils, "_files_for_key", return_value=list(covered_by)),
			patch.object(s3_utils, "object_exists", return_value=object_exists),
			patch.object(s3_utils, "get_s3_client", return_value=MagicMock()),
			patch.object(repair, "_restore_file") as restore,
		):
			yield restore

	def _interview_row(self, key):
		return frappe._dict(name="HR-INT-0001", owner="hr@example.com", value=s3_utils._build_file_url(key))

	def test_patch_restores_record_for_orphaned_link(self):
		from aws_s3_storage.patches.v1_0 import restore_missing_file_records as repair

		key = "private/f5288607fbaa4ac4948da25797a7868c/Eslam_S_Cv_.pdf"
		with self._linked_rows([self._interview_row(key)]) as restore:
			repair.execute()

		restore.assert_called_once()
		doctype, fieldname, row, restored_key = restore.call_args[0]
		self.assertEqual((doctype, fieldname), ("Interview", "custom_resume_attachment"))
		self.assertEqual(restored_key, key)
		self.assertEqual(row.name, "HR-INT-0001")

	def test_patch_skips_link_already_covered_by_a_file_record(self):
		from aws_s3_storage.patches.v1_0 import restore_missing_file_records as repair

		rows = [self._interview_row("private/uid/cv.pdf")]
		with self._linked_rows(rows, covered_by=["FILE-1"]) as restore:
			repair.execute()
		restore.assert_not_called()

	def test_patch_reports_instead_of_restoring_a_deleted_object(self):
		# Recreating a record for an object that is gone only turns a 403 into a 404.
		from aws_s3_storage.patches.v1_0 import restore_missing_file_records as repair

		rows = [self._interview_row("private/uid/cv.pdf")]
		with self._linked_rows(rows, object_exists=False) as restore:
			repair.execute()
		restore.assert_not_called()

	def test_patch_ignores_local_and_non_servable_values(self):
		from aws_s3_storage.patches.v1_0 import restore_missing_file_records as repair

		rows = [
			frappe._dict(name="D1", owner="x", value="/files/local.pdf"),
			frappe._dict(name="D2", owner="x", value=s3_utils._build_file_url("backups/site/db.sql.gz")),
		]
		with self._linked_rows(rows) as restore:
			repair.execute()
		restore.assert_not_called()

	# --- backup sync -------------------------------------------------------

	@patch.object(s3_utils, "get_s3_client")
	def test_backup_sync_noop_when_disabled(self, mock_get_client):
		s3_utils.sync_backups_to_s3()
		mock_get_client.assert_not_called()

	def test_object_exists_checks_size(self):
		s3 = MagicMock()
		s3.head_object.return_value = {"ContentLength": 10}
		self.assertTrue(s3_utils.object_exists("k", expected_size=10, s3=s3, bucket="b"))
		self.assertFalse(s3_utils.object_exists("k", expected_size=5, s3=s3, bucket="b"))

	def test_object_exists_false_on_missing(self):
		s3 = MagicMock()
		s3.head_object.side_effect = ClientError({"Error": {"Code": "404"}}, "HeadObject")
		self.assertFalse(s3_utils.object_exists("k", s3=s3, bucket="b"))
