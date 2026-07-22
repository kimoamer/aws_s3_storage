# Copyright (c) 2026, Frappe and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from aws_s3_storage.aws_s3_storage import s3_utils


class TestS3Settings(FrappeTestCase):
	def setUp(self):
		frappe.db.set_single_value("S3 Settings", "bucket_name", "test-bucket")
		frappe.db.set_single_value("S3 Settings", "region", "us-east-1")

	def test_key_roundtrips_through_file_url(self):
		key = "private/abc123/my report.pdf"
		url = s3_utils._build_file_url(key)
		self.assertEqual(s3_utils._extract_key(url), key)

	def test_extract_key_ignores_non_s3_urls(self):
		self.assertIsNone(s3_utils._extract_key("/files/foo.png"))
		self.assertIsNone(s3_utils._extract_key(None))

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
	def test_delete_uses_key_from_url(self, mock_get_client):
		s3 = MagicMock()
		mock_get_client.return_value = s3

		key = "public/xyz/pic.png"
		doc = frappe._dict(file_url=s3_utils._build_file_url(key), thumbnail_url=None)
		s3_utils.delete_file_from_s3(doc)

		s3.delete_object.assert_called_once_with(Bucket="test-bucket", Key=key)
