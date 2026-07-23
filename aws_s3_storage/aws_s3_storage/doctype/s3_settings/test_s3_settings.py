# Copyright (c) 2026, Frappe and Contributors
# See license.txt

import base64
import hashlib
from unittest.mock import MagicMock, patch

import frappe
from botocore.exceptions import ClientError
from frappe.tests.utils import FrappeTestCase

from aws_s3_storage.aws_s3_storage import s3_utils


class TestS3Settings(FrappeTestCase):
	def setUp(self):
		# Deterministic settings regardless of other tests / rollbacks.
		frappe.db.set_single_value("S3 Settings", "bucket_name", "test-bucket")
		frappe.db.set_single_value("S3 Settings", "region", "us-east-1")
		frappe.db.set_single_value("S3 Settings", "storage_class", "STANDARD")
		frappe.db.set_single_value("S3 Settings", "verify_upload_integrity", 0)
		frappe.db.set_single_value("S3 Settings", "presigned_url_expiry", 3600)
		frappe.db.set_single_value("S3 Settings", "enable_backup_sync", 0)

	# --- URL / key helpers -------------------------------------------------

	def test_key_roundtrips_through_file_url(self):
		key = "private/abc123/my report.pdf"
		url = s3_utils._build_file_url(key)
		self.assertEqual(s3_utils._extract_key(url), key)

	def test_extract_key_ignores_non_s3_urls(self):
		self.assertIsNone(s3_utils._extract_key("/files/foo.png"))
		self.assertIsNone(s3_utils._extract_key(None))

	# --- write_file_to_s3 --------------------------------------------------

	@patch.object(s3_utils, "get_s3_client")
	def test_write_public_file_sets_no_acl(self, mock_get_client):
		s3 = MagicMock()
		mock_get_client.return_value = s3

		result = s3_utils.write_file_to_s3("hello.txt", b"data", content_type="text/plain", is_private=0)

		self.assertEqual(result["file_name"], "hello.txt")
		self.assertEqual(result["file_size"], 4)
		key = s3_utils._extract_key(result["file_url"])
		self.assertTrue(key.startswith("public/"))
		self.assertTrue(key.endswith("/hello.txt"))

		_, kwargs = s3.put_object.call_args
		self.assertNotIn("ACL", kwargs)
		self.assertEqual(kwargs["Bucket"], "test-bucket")
		self.assertEqual(kwargs["Key"], key)

	@patch.object(s3_utils, "get_s3_client")
	def test_write_encodes_str_content_as_utf8(self, mock_get_client):
		s3 = MagicMock()
		mock_get_client.return_value = s3

		result = s3_utils.write_file_to_s3("note.txt", "café", is_private=1)

		# "café" is 5 bytes in UTF-8
		self.assertEqual(result["file_size"], 5)
		self.assertTrue(s3_utils._extract_key(result["file_url"]).startswith("private/"))

	@patch.object(s3_utils, "get_s3_client")
	def test_write_accepts_file_document(self, mock_get_client):
		# The v15 File doctype path calls the hook with the File document itself.
		s3 = MagicMock()
		mock_get_client.return_value = s3

		file_doc = frappe.new_doc("File")
		file_doc.file_name = "doc.txt"
		file_doc.is_private = 1
		file_doc.content_type = "text/plain"
		file_doc.get_content = MagicMock(return_value=b"hello")

		result = s3_utils.write_file_to_s3(file_doc)

		key = s3_utils._extract_key(result["file_url"])
		self.assertTrue(key.startswith("private/"))
		self.assertTrue(key.endswith("/doc.txt"))
		# The hook must set file_url on the document, mirroring save_file_on_filesystem.
		self.assertEqual(file_doc.file_url, result["file_url"])

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
	def test_write_omits_content_md5_when_disabled(self, mock_get_client):
		s3 = MagicMock()
		mock_get_client.return_value = s3

		s3_utils.write_file_to_s3("f.bin", b"data")

		_, kwargs = s3.put_object.call_args
		self.assertNotIn("ContentMD5", kwargs)

	@patch.object(s3_utils, "get_s3_client")
	def test_write_applies_storage_class(self, mock_get_client):
		frappe.db.set_single_value("S3 Settings", "storage_class", "STANDARD_IA")
		s3 = MagicMock()
		mock_get_client.return_value = s3

		s3_utils.write_file_to_s3("f.bin", b"data")

		_, kwargs = s3.put_object.call_args
		self.assertEqual(kwargs["StorageClass"], "STANDARD_IA")

	# --- download_file -----------------------------------------------------

	@patch.object(s3_utils, "get_s3_client")
	def test_download_uses_configured_expiry(self, mock_get_client):
		frappe.db.set_single_value("S3 Settings", "presigned_url_expiry", 120)
		s3 = MagicMock()
		s3.generate_presigned_url.return_value = "https://signed.example/x"
		mock_get_client.return_value = s3

		s3_utils.download_file("public/xyz/pic.png")

		_, kwargs = s3.generate_presigned_url.call_args
		self.assertEqual(kwargs["ExpiresIn"], 120)
		self.assertEqual(frappe.local.response["location"], "https://signed.example/x")

	# --- delete_file_from_s3 (deferred to after-commit) --------------------

	@patch.object(s3_utils, "get_s3_client")
	def test_delete_is_deferred_until_after_commit(self, mock_get_client):
		s3 = MagicMock()
		mock_get_client.return_value = s3

		key = "public/xyz/pic.png"
		doc = frappe._dict(file_url=s3_utils._build_file_url(key), thumbnail_url=None)

		captured = []
		with patch.object(frappe.db.after_commit, "add", side_effect=captured.append):
			s3_utils.delete_file_from_s3(doc)

		# Nothing is deleted inline — only after the transaction commits.
		s3.delete_object.assert_not_called()
		self.assertEqual(len(captured), 1)

		captured[0]()  # simulate the post-commit callback firing
		s3.delete_object.assert_called_once_with(Bucket="test-bucket", Key=key)

	@patch.object(s3_utils, "get_s3_client")
	def test_delete_falls_back_to_local_for_non_s3_files(self, mock_get_client):
		# A legacy file stored on local disk (no S3 key) must still be cleaned up
		# via Frappe's own on-disk deletion, not silently leaked.
		recorded = {}

		class FakeFile:
			file_url = "/private/files/legacy.pdf"
			thumbnail_url = None

			def delete_file_from_filesystem(self, only_thumbnail=False):
				recorded["only_thumbnail"] = only_thumbnail

		s3_utils.delete_file_from_s3(FakeFile())

		self.assertEqual(recorded.get("only_thumbnail"), False)
		mock_get_client.assert_not_called()

	# --- backup sync -------------------------------------------------------

	@patch.object(s3_utils, "get_s3_client")
	def test_backup_sync_noop_when_disabled(self, mock_get_client):
		s3_utils.sync_backups_to_s3()
		mock_get_client.assert_not_called()

	def test_object_exists_checks_size(self):
		s3 = MagicMock()
		s3.head_object.return_value = {"ContentLength": 10}
		self.assertTrue(s3_utils._object_exists(s3, "b", "k", expected_size=10))
		self.assertFalse(s3_utils._object_exists(s3, "b", "k", expected_size=5))

	def test_object_exists_false_on_missing(self):
		s3 = MagicMock()
		s3.head_object.side_effect = ClientError({"Error": {"Code": "404"}}, "HeadObject")
		self.assertFalse(s3_utils._object_exists(s3, "b", "k"))

	# --- server-side read / thumbnails -------------------------------------

	@patch.object(s3_utils, "get_s3_client")
	def test_read_file_from_s3(self, mock_get_client):
		from io import BytesIO

		s3 = MagicMock()
		s3.get_object.return_value = {"Body": BytesIO(b"payload")}
		mock_get_client.return_value = s3

		self.assertEqual(s3_utils.read_file_from_s3("public/x/f.bin"), b"payload")
		s3.get_object.assert_called_once_with(Bucket="test-bucket", Key="public/x/f.bin")

	@patch.object(s3_utils, "get_s3_client")
	def test_s3file_make_thumbnail_stores_private_thumbnail_in_s3(self, mock_get_client):
		from io import BytesIO

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
			result = f.make_thumbnail()

		# The thumbnail is uploaded as its own (still private) S3 object.
		self.assertTrue(s3.put_object.called)
		_, kwargs = s3.put_object.call_args
		self.assertTrue(kwargs["Key"].startswith("private/uid/pic_small."))
		# thumbnail_url is persisted and points back at our download endpoint.
		f.db_set.assert_called_once()
		self.assertEqual(f.db_set.call_args[0][0], "thumbnail_url")
		self.assertIn("key=", result)

	@patch.object(s3_utils, "get_s3_client")
	def test_download_allows_thumbnail_key_via_permission(self, mock_get_client):
		# A private thumbnail key lives in File.thumbnail_url, not file_url; the
		# permission lookup must still find the owning File.
		s3 = MagicMock()
		s3.generate_presigned_url.return_value = "https://signed.example/thumb"
		mock_get_client.return_value = s3

		thumb_key = "private/uid/pic_small.png"
		looked_up = {}

		def fake_get_value(doctype, filters, fieldname):
			looked_up.setdefault("filters", []).append(filters)
			# Simulate: no File matches on file_url, one matches on thumbnail_url.
			if "thumbnail_url" in filters:
				return "FILE-0001"
			return None

		with (
			patch.object(frappe.db, "get_value", side_effect=fake_get_value),
			patch.object(frappe, "get_doc") as mock_get_doc,
		):
			s3_utils.download_file(thumb_key)

		mock_get_doc.assert_called_once_with("File", "FILE-0001")
		mock_get_doc.return_value.check_permission.assert_called_once_with("read")
		self.assertEqual(frappe.local.response["location"], "https://signed.example/thumb")
