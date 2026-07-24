# Copyright (c) 2026, Innomate LLC
# See license.txt

import base64
import hashlib
import os
import tempfile
from io import BytesIO
from unittest.mock import MagicMock, patch

import frappe
from botocore.exceptions import ClientError
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
	def test_download_private_checks_permission_via_thumbnail_key(self, mock_get_client):
		s3 = MagicMock()
		s3.generate_presigned_url.return_value = "https://signed.example/thumb"
		mock_get_client.return_value = s3

		def fake_get_value(doctype, filters, fieldname):
			return "FILE-1" if "s3_thumbnail_key" in filters else None

		with (
			patch.object(frappe.db, "get_value", side_effect=fake_get_value),
			patch.object(frappe, "get_doc") as mock_get_doc,
		):
			s3_utils.download_file("private/uid/pic_small.png")

		mock_get_doc.assert_called_once_with("File", "FILE-1")
		mock_get_doc.return_value.check_permission.assert_called_once_with("read")

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
