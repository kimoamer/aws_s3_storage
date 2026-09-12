# Copyright (c) 2026, Innomate LLC
# See license.txt

import base64
import hashlib
import os
import tempfile
from contextlib import ExitStack, contextmanager
from io import BytesIO
from unittest.mock import MagicMock, patch

import frappe
from botocore.exceptions import ClientError
from frappe.core.doctype.file.file import File
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from aws_s3_storage.aws_s3_storage import environment, s3_utils


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
		frappe.db.set_single_value("S3 Settings", "restrict_to_doctypes", 0)
		frappe.db.set_single_value("S3 Settings", "scoped_doctypes", "")
		frappe.db.set_single_value("S3 Settings", "include_unattached_files", 0)
		frappe.db.set_single_value("S3 Settings", "enabled", 1)
		frappe.db.set_single_value("S3 Settings", "read_only_mode", 0)
		frappe.db.set_single_value("S3 Settings", "inherited_storage_policy", environment.POLICY_READ_ONLY)
		# Match whatever this site actually recorded on disk, so the ownership check
		# lands on "owner" (or "unclaimed" if the install never claimed) rather than
		# depending on how the test site was created.
		frappe.db.set_single_value("S3 Settings", "storage_owner_id", environment.local_owner_id())
		frappe.db.set_single_value("S3 Settings", "storage_owner_site", environment.current_site())
		frappe.db.set_single_value("S3 Settings", "storage_owner_instance", environment.instance_id())
		# There is no real bucket here to hold an ownership lease, so seed the
		# answer the lease check caches. Tests that are *about* the lease clear it
		# and drive _read_lease themselves (see _with_lease).
		self._seed_lease(environment.LEASE_OK)

	def _seed_lease(self, status, reason=None):
		frappe.cache().set_value(
			environment._lease_cache_key(frappe.get_single("S3 Settings")),
			{"status": status, "reason": reason, "holder": {}, "stale": False},
			expires_in_sec=environment.LEASE_CHECK_INTERVAL,
		)

	def _clear_lease_cache(self):
		environment._clear_lease_cache()

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
	def _environment(self, db_owner, disk_owner, owner_site=None):
		"""Force the two halves of the ownership check together or apart.

		``db_owner`` is what a restored database carries; ``disk_owner`` is what
		site_config.json on this server says. They only agree on the site that
		claimed the bucket — which is exactly what the guard relies on.
		"""
		key = environment.OWNER_CONFIG_KEY
		had, previous = key in frappe.conf, frappe.conf.get(key)
		before = {
			field: frappe.db.get_single_value("S3 Settings", field) or ""
			for field in ("storage_owner_id", "storage_owner_site")
		}

		if disk_owner is None:
			frappe.conf.pop(key, None)
		else:
			frappe.conf[key] = disk_owner
		frappe.db.set_single_value("S3 Settings", "storage_owner_id", db_owner or "")
		frappe.db.set_single_value(
			"S3 Settings",
			"storage_owner_site",
			environment.current_site() if owner_site is None else owner_site,
		)
		try:
			yield
		finally:
			if had:
				frappe.conf[key] = previous
			else:
				frappe.conf.pop(key, None)
			for field, value in before.items():
				frappe.db.set_single_value("S3 Settings", field, value)

	def _restored_copy(self):
		"""A production database restored onto a site that never claimed the bucket."""
		return self._environment(db_owner="prod-environment", disk_owner=None)

	def _owner_site(self):
		return self._environment(db_owner="this-environment", disk_owner="this-environment")

	@contextmanager
	def _with_lease(self, lease, instance=None):
		"""Run against a given lease object in the bucket, uncached.

		``lease`` is what ``_read_lease`` returns — a dict, or None for "no lease
		object yet". ``instance`` overrides what this server answers as its own
		identity, which is how a copy running on another machine is simulated.
		"""
		with ExitStack() as stack:
			if instance is not None:
				stack.enter_context(patch.object(environment, "instance_id", return_value=instance))
			# instance_id feeds the cache key, so clear only once it is patched.
			self._clear_lease_cache()
			stack.enter_context(patch.object(environment, "_read_lease", return_value=(lease, '"etag"')))
			stack.enter_context(
				patch.object(environment, "_write_lease", side_effect=lambda settings, **kw: lease or {})
			)
			try:
				yield
			finally:
				self._clear_lease_cache()
		self._seed_lease(environment.LEASE_OK)

	@staticmethod
	def _live_lease(owner_id="this-environment", instance="server-a", site="prod.example.com"):
		return {
			"owner_id": owner_id,
			"instance": instance,
			"site": site,
			"host": "prod-host",
			"heartbeat_at": now_datetime().isoformat(),
		}

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

	# --- doctype scope -----------------------------------------------------

	def _restrict_to(self, doctypes, include_unattached=0):
		frappe.db.set_single_value("S3 Settings", "restrict_to_doctypes", 1)
		frappe.db.set_single_value("S3 Settings", "scoped_doctypes", doctypes)
		frappe.db.set_single_value("S3 Settings", "include_unattached_files", include_unattached)

	def test_scope_is_site_wide_by_default(self):
		# Nothing configured -> every doctype (and every unattached file) uses S3.
		self.assertTrue(s3_utils.in_scope(frappe._dict(attached_to_doctype="Sales Invoice")))
		self.assertTrue(s3_utils.in_scope(frappe._dict()))
		self.assertFalse(s3_utils.is_scope_restricted())

	def test_in_scope_only_for_listed_doctypes(self):
		self._restrict_to("Sales Invoice\nPurchase Invoice")
		self.assertTrue(s3_utils.in_scope(frappe._dict(attached_to_doctype="Sales Invoice")))
		self.assertTrue(s3_utils.in_scope(attached_to_doctype="Purchase Invoice"))
		self.assertFalse(s3_utils.in_scope(frappe._dict(attached_to_doctype="Lead")))

	def test_unattached_files_follow_their_own_switch(self):
		# A file that hangs off no document has no doctype to match.
		self._restrict_to("Sales Invoice")
		self.assertFalse(s3_utils.in_scope(frappe._dict()))
		self.assertFalse(s3_utils.in_scope(frappe._dict(attached_to_doctype="")))

		self._restrict_to("Sales Invoice", include_unattached=1)
		self.assertTrue(s3_utils.in_scope(frappe._dict()))
		self.assertFalse(s3_utils.in_scope(frappe._dict(attached_to_doctype="Lead")))

	def test_empty_scope_stores_nothing_in_s3(self):
		self._restrict_to("")
		self.assertFalse(s3_utils.in_scope(frappe._dict(attached_to_doctype="Sales Invoice")))
		self.assertFalse(s3_utils.in_scope(frappe._dict()))

	def test_scope_description(self):
		self.assertEqual(s3_utils.scope_description(), "All doctypes")
		self._restrict_to("Sales Invoice")
		self.assertEqual(s3_utils.scope_description(), "Sales Invoice")
		self._restrict_to("Sales Invoice", include_unattached=1)
		self.assertEqual(s3_utils.scope_description(), "Sales Invoice, Files not attached to a document")

	def test_write_falls_back_to_local_for_out_of_scope_doctype(self):
		self._restrict_to("Sales Invoice")
		doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "cv.pdf",
				"attached_to_doctype": "Lead",
				"attached_to_name": "LEAD-0001",
				"is_private": 1,
			}
		)
		with (
			patch.object(s3_utils, "get_s3_client") as client,
			patch.object(
				s3_utils, "_save_to_filesystem", return_value={"file_url": "/private/files/cv.pdf"}
			) as fallback,
		):
			result = s3_utils.write_file_to_s3(doc)

		client.assert_not_called()
		fallback.assert_called_once()
		self.assertEqual(result["file_url"], "/private/files/cv.pdf")

	@patch.object(s3_utils, "get_s3_client")
	def test_write_uses_s3_for_a_scoped_doctype(self, mock_get_client):
		self._restrict_to("Sales Invoice")
		s3 = MagicMock()
		mock_get_client.return_value = s3
		doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "invoice.pdf",
				"attached_to_doctype": "Sales Invoice",
				"attached_to_name": "SINV-0001",
				"is_private": 1,
				"content": b"data",
				"decode": 0,
			}
		)

		with patch.object(s3_utils, "_save_to_filesystem") as fallback:
			result = s3_utils.write_file_to_s3(doc)

		fallback.assert_not_called()
		s3.put_object.assert_called_once()
		self.assertTrue(result["s3_key"].startswith("private/"))

	def test_write_keeps_unattached_file_local_when_excluded(self):
		# The legacy file_manager convention passes a name only: no doctype to match,
		# so it follows the unattached switch (off here).
		self._restrict_to("Sales Invoice")
		with (
			patch.object(s3_utils, "get_s3_client") as client,
			patch.object(s3_utils, "_save_to_filesystem", return_value={"file_url": "/files/x"}) as fallback,
		):
			s3_utils.write_file_to_s3("logo.png", b"data")

		client.assert_not_called()
		fallback.assert_called_once()

	def test_migration_skips_out_of_scope_file(self):
		from aws_s3_storage.aws_s3_storage import migrate

		self._restrict_to("Sales Invoice")
		doc = frappe._dict(
			name="F1",
			is_folder=0,
			s3_key=None,
			file_url="/private/files/cv.pdf",
			file_name="cv.pdf",
			attached_to_doctype="Lead",
		)
		with self._file_doc(doc):
			self.assertEqual(migrate.migrate_file("F1"), "skipped")

	def test_pending_filter_sets_follow_the_scope(self):
		from aws_s3_storage.aws_s3_storage import migrate

		# Unrestricted: one query, no doctype condition at all.
		self.assertEqual(len(migrate._pending_filter_sets()), 1)
		self.assertNotIn("attached_to_doctype", migrate._pending_filter_sets()[0])

		self._restrict_to("Sales Invoice\nPurchase Invoice")
		filter_sets = migrate._pending_filter_sets()
		self.assertEqual(len(filter_sets), 1)
		self.assertEqual(filter_sets[0]["attached_to_doctype"], ["in", ["Purchase Invoice", "Sales Invoice"]])

		# Unattached files are a second, mutually exclusive query.
		self._restrict_to("Sales Invoice", include_unattached=1)
		filter_sets = migrate._pending_filter_sets()
		self.assertEqual(len(filter_sets), 2)
		self.assertEqual(filter_sets[1]["attached_to_doctype"], ["is", "not set"])

		# Restricted to nothing at all: no query, so the migration has nothing to do.
		self._restrict_to("")
		self.assertEqual(migrate._pending_filter_sets(), [])
		self.assertEqual(migrate.count_pending(), 0)

	def test_settings_normalise_the_doctype_list(self):
		doc = frappe.get_single("S3 Settings")
		doc.restrict_to_doctypes = 1
		doc.scoped_doctypes = "  ToDo \n\nContact, ToDo\n"
		doc._validate_doctype_scope()
		self.assertEqual(doc.scoped_doctypes, "ToDo\nContact")

	# --- scope: deduplication must not cross storages ----------------------

	def _file_with_content(self, **fields):
		"""A File doc holding content, as it is mid-insert (never saved)."""
		from aws_s3_storage.aws_s3_storage.file_override import S3File

		doc = S3File({"doctype": "File", "file_name": "report.pdf", "is_private": 1, **fields})
		doc._content = b"data"
		return doc

	def test_out_of_scope_upload_does_not_inherit_an_s3_object(self):
		# Frappe reuses a matching content hash before the storage hook runs: an
		# out-of-scope attachment would silently end up on the S3 object.
		self._restrict_to("Sales Invoice")
		key = "private/uid/report.pdf"
		doc = self._file_with_content(
			attached_to_doctype="Lead", file_url=s3_utils._build_file_url(key), s3_key=key
		)

		with patch.object(File, "save_file") as save_file:
			doc._enforce_storage_location()

		save_file.assert_called_once()
		self.assertTrue(save_file.call_args.kwargs["ignore_existing_file_check"])
		self.assertIsNone(doc.file_url)  # cleared, so the rewrite picks local storage
		self.assertIsNone(doc.s3_key)

	def test_in_scope_upload_does_not_stay_on_a_local_duplicate(self):
		# The mirror image: an identical file uploaded before the bucket existed
		# would keep an in-scope attachment on local disk forever.
		self._restrict_to("Sales Invoice")
		doc = self._file_with_content(
			attached_to_doctype="Sales Invoice", file_url="/private/files/report.pdf"
		)

		with patch.object(File, "save_file") as save_file:
			doc._enforce_storage_location()

		save_file.assert_called_once()
		self.assertTrue(save_file.call_args.kwargs["ignore_existing_file_check"])
		self.assertIsNone(doc.file_url)

	def test_storage_location_is_left_alone_when_it_is_already_right(self):
		self._restrict_to("Sales Invoice")
		key = "private/uid/report.pdf"
		in_s3 = self._file_with_content(
			attached_to_doctype="Sales Invoice", file_url=s3_utils._build_file_url(key), s3_key=key
		)
		local = self._file_with_content(attached_to_doctype="Lead", file_url="/private/files/report.pdf")

		with patch.object(File, "save_file") as save_file:
			in_s3._enforce_storage_location()
			local._enforce_storage_location()

		save_file.assert_not_called()

	def test_an_attachment_copy_without_content_is_never_rewritten(self):
		# Amend re-inserts attachments pointing at the original's object; there is
		# nothing to write, and the object belongs to the record it came from.
		from aws_s3_storage.aws_s3_storage.file_override import S3File

		self._restrict_to("Sales Invoice")
		key = "private/uid/report.pdf"
		doc = S3File(
			{
				"doctype": "File",
				"file_name": "report.pdf",
				"is_private": 1,
				"attached_to_doctype": "Lead",
				"file_url": s3_utils._build_file_url(key),
				"s3_key": key,
			}
		)

		with patch.object(File, "save_file") as save_file:
			doc._enforce_storage_location()

		save_file.assert_not_called()
		self.assertEqual(doc.s3_key, key)

	# --- scope: files that are attached (or re-attached) later --------------

	def _reevaluate(self, **fields):
		"""Run the File on_update hook and return the jobs it queued."""
		from aws_s3_storage.aws_s3_storage import migrate

		doc = frappe._dict(name="F1", **fields)
		with patch.object(frappe, "enqueue") as enqueue:
			migrate.reevaluate_scope(doc)
		return enqueue.call_args_list

	def test_web_form_upload_moves_to_s3_once_it_is_attached(self):
		# A Web Form uploads before the document exists, so the file starts
		# unattached and local; submitting attaches it to an in-scope doctype.
		self._restrict_to("Sales Invoice")
		self.assertEqual(self._reevaluate(file_url="/private/files/wf.pdf", attached_to_doctype=None), [])

		jobs = self._reevaluate(file_url="/private/files/wf.pdf", attached_to_doctype="Sales Invoice")
		self.assertEqual(len(jobs), 1)
		self.assertEqual(jobs[0].kwargs["file_name"], "F1")
		self.assertTrue(jobs[0].kwargs["enqueue_after_commit"])

	def test_attachment_copied_to_an_out_of_scope_doctype_leaves_s3(self):
		self._restrict_to("Sales Invoice")
		key = "private/uid/report.pdf"
		jobs = self._reevaluate(
			file_url=s3_utils._build_file_url(key), s3_key=key, attached_to_doctype="Lead"
		)
		self.assertEqual(len(jobs), 1)

	def test_reevaluate_is_a_noop_when_nothing_is_out_of_place(self):
		key = "private/uid/report.pdf"
		s3_url = s3_utils._build_file_url(key)

		# Scope off: the hook never fires at all.
		self.assertEqual(self._reevaluate(file_url="/files/a.pdf", attached_to_doctype="Lead"), [])

		self._restrict_to("Sales Invoice")
		# Already where it belongs, in both directions.
		self.assertEqual(
			self._reevaluate(file_url=s3_url, s3_key=key, attached_to_doctype="Sales Invoice"), []
		)
		self.assertEqual(self._reevaluate(file_url="/files/a.pdf", attached_to_doctype="Lead"), [])
		# An external link has nothing to move, and folders are not files.
		self.assertEqual(
			self._reevaluate(file_url="https://example.com/a.pdf", attached_to_doctype="Sales Invoice"),
			[],
		)
		self.assertEqual(
			self._reevaluate(is_folder=1, file_url="/files/a.pdf", attached_to_doctype="Sales Invoice"),
			[],
		)

	def test_move_file_for_scope_picks_the_right_direction(self):
		from aws_s3_storage.aws_s3_storage import migrate

		self._restrict_to("Sales Invoice")
		key = "private/uid/report.pdf"
		to_s3 = frappe._dict(
			name="F1", is_folder=0, file_url="/private/files/report.pdf", attached_to_doctype="Sales Invoice"
		)
		to_disk = frappe._dict(
			name="F2",
			is_folder=0,
			file_url=s3_utils._build_file_url(key),
			s3_key=key,
			attached_to_doctype="Lead",
		)

		for doc, expected in ((to_s3, "up"), (to_disk, "down")):
			with (
				self._file_doc(doc),
				patch.object(frappe.db, "exists", return_value=True),
				patch.object(migrate, "migrate_file", return_value="migrated") as up,
				patch.object(migrate, "move_file_to_disk") as down,
				patch.object(frappe.db, "commit"),
			):
				migrate.move_file_for_scope(doc.name)
			self.assertEqual(up.called, expected == "up")
			self.assertEqual(down.called, expected == "down")

	def test_cleanup_never_touches_a_file_kept_local_by_the_scope(self):
		# cleanup only walks records that already carry an s3_key, and it keeps any
		# local file another record still points at — so an out-of-scope twin that
		# shares the name is safe.
		from aws_s3_storage.aws_s3_storage import migrate

		self._restrict_to("Sales Invoice")
		tmp = tempfile.NamedTemporaryFile(delete=False)
		tmp.write(b"data")
		tmp.close()
		try:
			with (
				patch.object(migrate, "_full_path", return_value=tmp.name),
				# an out-of-scope File record still points at this local URL
				patch.object(frappe.db, "exists", return_value=True),
				patch.object(s3_utils, "object_exists", return_value=True) as obj_exists,
			):
				removed = migrate._safe_remove_local(
					"/files/report.pdf", "public/u/report.pdf", MagicMock(), "b"
				)
			self.assertFalse(removed)
			self.assertTrue(os.path.exists(tmp.name))
			obj_exists.assert_not_called()  # bailed out before even asking S3
		finally:
			os.unlink(tmp.name)

	def test_cleanup_query_only_covers_migrated_files(self):
		from aws_s3_storage.aws_s3_storage import migrate

		self._restrict_to("Sales Invoice")
		captured = []

		def fake_get_all(doctype, filters=None, **kwargs):
			captured.append(filters)
			return []

		with patch.object(frappe, "get_all", side_effect=fake_get_all):
			migrate.cleanup_migrated_local_files()

		self.assertEqual(captured[0]["s3_key"], ["not in", ["", None]])

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

		def fake_pending(limit, exclude=None, **kwargs):
			exclude = set(exclude or [])
			return [n for n in names if n not in exclude]

		def fake_migrate(name, **kwargs):
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
			patch.object(repair, "_linked_single_values", return_value=[]),
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

	def test_patch_restores_a_logo_attached_to_a_single(self):
		# The site logo and favicon live in Website Settings, whose values are rows in
		# tabSingles, not columns on a table — and they are requested on every page.
		from aws_s3_storage.patches.v1_0 import restore_missing_file_records as repair

		key = "private/0fed92f26b0a4df991df01bc9cfe8421/PL-logo-favicon.png"
		single = frappe._dict(
			doctype="Website Settings", field="favicon", value=s3_utils._build_file_url(key)
		)

		with (
			patch.object(s3_utils, "_attach_fields", return_value=[]),
			patch.object(repair, "_linked_single_values", return_value=[single]),
			patch.object(s3_utils, "_files_for_key", return_value=[]),
			patch.object(s3_utils, "object_exists", return_value=True),
			patch.object(s3_utils, "get_s3_client", return_value=MagicMock()),
			patch.object(repair, "_restore_file") as restore,
		):
			repair.execute()

		doctype, fieldname, row, restored_key = restore.call_args[0]
		self.assertEqual((doctype, fieldname), ("Website Settings", "favicon"))
		# A Single's record is the doctype itself.
		self.assertEqual(row.name, "Website Settings")
		self.assertEqual(restored_key, key)

	def test_deletion_guard_sees_a_link_held_by_a_single(self):
		key = "private/0fed92f26b0a4df991df01bc9cfe8421/PL-logo-favicon.png"

		def fake_sql(query, params=None):
			# Only tabSingles holds it; no table-backed field does.
			return [(s3_utils._build_file_url(key),)] if "tabSingles" in query else []

		with (
			patch.object(s3_utils, "_attach_fields", return_value=[]),
			patch.object(frappe.db, "sql", side_effect=fake_sql),
		):
			self.assertTrue(s3_utils._key_is_linked_from_a_document(key))

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

	# --- storage ownership -------------------------------------------------
	# A copy of this database must not be able to modify the original's files.
	# The scenario throughout: a production database is restored onto a test site.
	# Every File record, every S3 key and the bucket configuration come with it,
	# so nothing inside the database distinguishes the two sites. Ownership is
	# decided by a value that is *not* in the database (site_config.json), which
	# is why the test site can be told apart at all.

	def test_a_fresh_install_is_unclaimed(self):
		with self._environment(db_owner="", disk_owner=None):
			self.assertEqual(environment.state(), environment.UNCLAIMED)
		# Unclaimed is permissive: nothing here says the database was moved.
		with self._environment(db_owner="", disk_owner=None):
			self.assertTrue(environment.may_modify_storage())

	def test_the_site_that_claimed_the_bucket_owns_it(self):
		with self._owner_site():
			self.assertEqual(environment.state(), environment.OWNER)
			self.assertTrue(environment.may_modify_storage())

	def test_a_restored_database_is_foreign(self):
		with self._restored_copy():
			info = environment.ownership()
			self.assertEqual(info.state, environment.FOREIGN)
			self.assertIn("restored here", info.reason)
			self.assertFalse(environment.may_modify_storage())

	def test_a_restore_over_a_site_that_claimed_its_own_bucket_is_foreign(self):
		with self._environment(db_owner="prod-environment", disk_owner="test-environment"):
			info = environment.ownership()
			self.assertEqual(info.state, environment.FOREIGN)
			self.assertIn("different environments", info.reason)

	def test_a_whole_site_copy_is_caught_by_the_site_name(self):
		# site_config.json travelled too, so the ids agree — the site it now answers
		# to is the only thing left that tells the copy from the original.
		with self._environment(db_owner="same-id", disk_owner="same-id", owner_site="prod.example.com"):
			info = environment.ownership()
			self.assertEqual(info.state, environment.FOREIGN)
			self.assertIn("looks like a copy", info.reason)

	def test_migrate_never_transfers_ownership_to_a_restored_copy(self):
		# bench migrate runs after_migrate -> claim_if_unclaimed on every site,
		# including the test copy. It must leave the recorded owner alone.
		with self._restored_copy():
			environment.claim_if_unclaimed()
			self.assertEqual(environment.recorded_owner_id(), "prod-environment")
			self.assertEqual(environment.state(), environment.FOREIGN)

	# --- read-only mode ----------------------------------------------------

	def test_read_only_mode_blocks_the_owner_too(self):
		with self._owner_site():
			frappe.db.set_single_value("S3 Settings", "read_only_mode", 1)
			self.assertEqual(environment.state(), environment.OWNER)
			self.assertFalse(environment.may_modify_storage())
			self.assertIn("Read-Only Mode", environment.blocked_reason())

	# --- per-file ownership ------------------------------------------------

	def test_a_file_with_no_recorded_owner_may_be_modified(self):
		with self._owner_site():
			self.assertTrue(environment.may_modify_object(frappe._dict()))
			self.assertTrue(environment.may_modify_object(frappe._dict(s3_owner="")))

	def test_a_file_owned_by_another_environment_is_left_alone(self):
		with self._owner_site():
			self.assertTrue(environment.may_modify_object(frappe._dict(s3_owner="this-environment")))
			self.assertFalse(environment.may_modify_object(frappe._dict(s3_owner="somebody-else")))

	def test_full_access_policy_overrides_per_file_ownership(self):
		with self._owner_site():
			frappe.db.set_single_value(
				"S3 Settings", "inherited_storage_policy", environment.POLICY_FULL_ACCESS
			)
			self.assertTrue(environment.may_modify_object(frappe._dict(s3_owner="somebody-else")))

	def test_full_access_policy_does_not_override_the_environment(self):
		# The escape hatch is for files, not for being the wrong environment.
		with self._restored_copy():
			frappe.db.set_single_value(
				"S3 Settings", "inherited_storage_policy", environment.POLICY_FULL_ACCESS
			)
			self.assertFalse(environment.may_modify_object(frappe._dict(s3_owner="prod-environment")))

	# --- uploads -----------------------------------------------------------

	def test_a_restored_copy_uploads_to_its_own_disk(self):
		with self._restored_copy():
			self.assertFalse(s3_utils.should_store_in_s3(frappe._dict(attached_to_doctype="Sales Invoice")))
			with (
				patch.object(s3_utils, "get_s3_client") as client,
				patch.object(
					s3_utils, "_save_to_filesystem", return_value={"file_url": "/files/x"}
				) as fallback,
			):
				result = s3_utils.write_file_to_s3("x.txt", b"data")

		client.assert_not_called()
		fallback.assert_called_once()
		self.assertEqual(result, {"file_url": "/files/x"})

	@patch.object(s3_utils, "get_s3_client")
	def test_an_upload_records_the_environment_that_made_it(self, mock_get_client):
		mock_get_client.return_value = MagicMock()
		with self._owner_site():
			result = s3_utils.write_file_to_s3("x.txt", b"data")
		self.assertEqual(result["s3_owner"], "this-environment")

	# --- deletion ----------------------------------------------------------

	def test_a_restored_copy_unlinks_but_never_deletes(self):
		doc = frappe._dict(s3_key="private/uid/report.pdf", file_url="/x", thumbnail_url=None)
		with self._restored_copy(), patch.object(s3_utils, "_delete_after_commit") as scheduled:
			s3_utils.delete_file_from_s3(doc)
		scheduled.assert_not_called()

	@patch.object(s3_utils, "get_s3_client")
	def test_the_delete_primitive_refuses_on_a_restored_copy(self, mock_get_client):
		# Callers check first, but _delete_keys runs from after_commit callbacks and
		# background jobs, so the one place that issues delete_object checks again.
		s3 = MagicMock()
		mock_get_client.return_value = s3
		with self._restored_copy():
			s3_utils._delete_keys("bucket", ["private/uid/report.pdf"])
		s3.delete_object.assert_not_called()

	def test_a_file_owned_by_another_environment_is_never_deleted(self):
		doc = frappe._dict(s3_key="private/uid/report.pdf", s3_owner="prod-environment", file_url="/x")
		with self._owner_site(), patch.object(s3_utils, "_delete_after_commit") as scheduled:
			s3_utils.delete_file_from_s3(doc)
		scheduled.assert_not_called()

	# --- privacy / thumbnails ----------------------------------------------

	@patch.object(s3_utils, "get_s3_client")
	def test_a_privacy_change_does_not_move_another_environments_object(self, mock_get_client):
		s3 = MagicMock()
		mock_get_client.return_value = s3
		key = "public/uid/logo.png"
		doc = frappe._dict(s3_key=key, file_url=s3_utils._build_file_url(key), is_private=1)

		with self._restored_copy():
			s3_utils.move_object_privacy(doc)

		s3.copy_object.assert_not_called()
		# The record still points at the object exactly where it is.
		self.assertEqual(doc.s3_key, key)

	@patch.object(s3_utils, "get_s3_client")
	def test_no_thumbnail_is_written_into_another_environments_bucket(self, mock_get_client):
		s3 = MagicMock()
		mock_get_client.return_value = s3
		with self._restored_copy():
			self.assertIsNone(s3_utils.upload_thumbnail("public/uid/t_small.png", b"x", "image/png"))
		s3.put_object.assert_not_called()

	# --- the deletion queue ------------------------------------------------

	def _queue_row(self, key, bucket="test-bucket"):
		doc = frappe.get_doc(
			{"doctype": "S3 Deletion Queue", "s3_key": key, "bucket": bucket, "status": "Pending"}
		).insert(ignore_permissions=True)
		self.addCleanup(
			lambda: (
				frappe.db.exists("S3 Deletion Queue", doc.name)
				and frappe.delete_doc("S3 Deletion Queue", doc.name, force=True, ignore_permissions=True)
			)
		)
		return doc

	@patch.object(s3_utils, "get_s3_client")
	def test_inherited_deletion_requests_are_parked_not_executed(self, mock_get_client):
		row = self._queue_row("private/inherited/report.pdf")
		s3 = MagicMock()
		mock_get_client.return_value = s3

		with self._restored_copy(), patch.object(frappe.db, "commit"):
			s3_utils.process_deletion_queue()

		s3.delete_object.assert_not_called()
		self.assertEqual(frappe.db.get_value("S3 Deletion Queue", row.name, "status"), "Blocked")

	@patch.object(s3_utils, "get_s3_client")
	def test_a_request_queued_against_another_bucket_is_parked(self, mock_get_client):
		row = self._queue_row("private/uid/old.pdf", bucket="some-other-bucket")
		s3 = MagicMock()
		mock_get_client.return_value = s3

		with self._owner_site(), patch.object(frappe.db, "commit"):
			s3_utils.process_deletion_queue()

		s3.delete_object.assert_not_called()
		self.assertEqual(frappe.db.get_value("S3 Deletion Queue", row.name, "status"), "Blocked")

	@patch.object(s3_utils, "get_s3_client")
	def test_the_queue_rechecks_references_before_retrying(self, mock_get_client):
		# The first attempt failed some time ago; by now the object is in use again.
		row = self._queue_row("private/uid/report.pdf")
		s3 = MagicMock()
		mock_get_client.return_value = s3

		with (
			self._owner_site(),
			patch.object(s3_utils, "_key_is_referenced", return_value=True),
			patch.object(frappe.db, "commit"),
		):
			s3_utils.process_deletion_queue()

		s3.delete_object.assert_not_called()
		self.assertFalse(frappe.db.exists("S3 Deletion Queue", row.name))

	# --- backups, migration, scope moves -----------------------------------

	def test_backup_sync_does_not_run_from_a_restored_copy(self):
		frappe.db.set_single_value("S3 Settings", "enable_backup_sync", 1)
		with self._restored_copy(), patch.object(s3_utils, "get_s3_client") as client:
			s3_utils.sync_backups_to_s3()
		client.assert_not_called()

	def test_migration_refuses_to_start_from_a_restored_copy(self):
		from aws_s3_storage.aws_s3_storage import migrate

		frappe.db.set_single_value("S3 Migration Status", "status", "Idle")
		with self._restored_copy():
			with self.assertRaises(frappe.ValidationError) as caught:
				migrate.start_migration()
		self.assertIn("Cannot migrate files into S3", str(caught.exception))

	def test_migrate_file_skips_on_a_restored_copy(self):
		from aws_s3_storage.aws_s3_storage import migrate

		doc = frappe._dict(name="F1", is_folder=0, file_url="/private/files/a.pdf", s3_key=None)
		with self._restored_copy(), self._file_doc(doc):
			self.assertEqual(migrate.migrate_file("F1"), "skipped")

	def test_a_restored_copy_never_moves_a_file_out_of_s3(self):
		from aws_s3_storage.aws_s3_storage import migrate

		key = "private/uid/report.pdf"
		row = frappe._dict(name="F1", s3_key=key, file_url=s3_utils._build_file_url(key))
		with self._restored_copy(), patch.object(s3_utils, "read_file_from_s3") as read:
			self.assertIsNone(migrate.move_file_to_disk(row))
		read.assert_not_called()

	def test_scope_moves_stand_down_on_a_restored_copy(self):
		self._restrict_to("Sales Invoice")
		key = "private/uid/report.pdf"
		with self._restored_copy():
			jobs = self._reevaluate(
				file_url=s3_utils._build_file_url(key), s3_key=key, attached_to_doctype="Lead"
			)
		self.assertEqual(jobs, [])

	def test_disabling_the_integration_never_pulls_files_out_of_s3(self):
		"""Turning "Enable S3 Storage" off must stop the integration, not start a move.

		With a doctype scope configured, should_store_in_s3() answers False for
		every file once the switch is off — which used to read as "this file belongs
		on local disk", download the object and delete it. The switch an
		administrator reaches for to *stop* the integration was the one that started
		moving files.
		"""
		self._restrict_to("Sales Invoice")
		key = "private/uid/report.pdf"
		s3_url = s3_utils._build_file_url(key)

		# Still on: an out-of-scope file is genuinely out of place, so it moves.
		self.assertEqual(len(self._reevaluate(file_url=s3_url, s3_key=key, attached_to_doctype="Lead")), 1)

		frappe.db.set_single_value("S3 Settings", "enabled", 0)
		# Off: nothing moves, in either direction.
		self.assertEqual(self._reevaluate(file_url=s3_url, s3_key=key, attached_to_doctype="Lead"), [])
		self.assertEqual(
			self._reevaluate(file_url=s3_url, s3_key=key, attached_to_doctype="Sales Invoice"), []
		)
		self.assertEqual(self._reevaluate(file_url="/files/a.pdf", attached_to_doctype="Sales Invoice"), [])

	# --- a copy of the whole server, keeping the site name -----------------
	# The case the local checks cannot see: the database id, the site_config.json
	# id and the site name were all copied, so all three agree on the copy. Only
	# the bucket — the thing the two servers share — can tell them apart.

	def test_a_full_server_copy_still_looks_like_the_owner_locally(self):
		# Stated plainly because it is the premise of everything below: the local
		# checks pass on such a copy. They are not the protection here.
		with self._environment(db_owner="prod-env", disk_owner="prod-env"):
			self.assertEqual(environment.state(), environment.OWNER)
			self.assertTrue(environment.may_modify_storage())

	def test_a_full_server_copy_may_not_destroy_anything(self):
		# ... and the lease catches it: the bucket still names the original server,
		# which was heard from moments ago.
		lease = self._live_lease(owner_id="prod-env", instance="the-original-server")
		with self._environment(db_owner="prod-env", disk_owner="prod-env"):
			with self._with_lease(lease, instance="the-copy"):
				result = environment.check_lease(refresh=True)
				self.assertEqual(result.status, environment.LEASE_CONFLICT)
				self.assertFalse(environment.may_destroy())

	@patch.object(s3_utils, "get_s3_client")
	def test_a_full_server_copy_does_not_reach_delete_object(self, mock_get_client):
		s3 = MagicMock()
		mock_get_client.return_value = s3
		lease = self._live_lease(owner_id="prod-env", instance="the-original-server")

		with self._environment(db_owner="prod-env", disk_owner="prod-env"):
			with self._with_lease(lease, instance="the-copy"):
				s3_utils._delete_keys("test-bucket", ["private/uid/report.pdf"])
				doc = frappe._dict(s3_key="private/uid/report.pdf", file_url="/x")
				with patch.object(s3_utils, "_delete_after_commit") as scheduled:
					s3_utils.delete_file_from_s3(doc)

		s3.delete_object.assert_not_called()
		scheduled.assert_not_called()

	def test_the_original_server_keeps_working(self):
		# The other half of the same scenario: the server the lease names is not
		# blocked by its own lease.
		lease = self._live_lease(owner_id="this-environment", instance="the-original-server")
		with self._owner_site(), self._with_lease(lease, instance="the-original-server"):
			self.assertEqual(environment.check_lease(refresh=True).status, environment.LEASE_OK)
			self.assertTrue(environment.may_destroy())

	def test_an_old_heartbeat_never_hands_the_lease_over(self):
		"""The hole a timeout would open, and the reason there is no timeout.

		The heartbeat is written when the lease is checked, and the lease is only
		checked before a destructive operation — so a healthy production site that
		has simply not deleted anything for a while has an old heartbeat. A rule
		that read that as "the holder is gone" would hand the lease to whoever
		asked next, and in the scenario this whole module exists for, that is the
		test copy asking.
		"""
		old = self._live_lease(instance="the-original-server")
		old["heartbeat_at"] = add_to_date(
			now_datetime(), seconds=-(environment.LEASE_STALE_AFTER * 24)
		).isoformat()

		with self._owner_site(), self._with_lease(old, instance="a-copy-on-another-machine"):
			result = environment.check_lease(refresh=True)
			self.assertEqual(result.status, environment.LEASE_CONFLICT)
			self.assertTrue(result.stale)  # reported, so a person can judge it
			self.assertFalse(environment.may_destroy())

	def test_a_rebuilt_server_waits_for_a_person(self):
		# The cost of the rule above: an identity that changed legitimately needs
		# the button. Stated as a test so the trade-off is not accidental.
		old = self._live_lease(instance="the-old-container")
		old["heartbeat_at"] = add_to_date(
			now_datetime(), seconds=-(environment.LEASE_STALE_AFTER + 60)
		).isoformat()

		with self._owner_site(), self._with_lease(old, instance="the-new-container"):
			self.assertEqual(environment.check_lease(refresh=True).status, environment.LEASE_CONFLICT)
			self.assertIn("Take Ownership", environment.blocked_reason())

	def test_a_bucket_leased_to_another_environment_is_a_conflict(self):
		# Pointing a second site at a bucket that is already someone's.
		with self._owner_site():
			with self._with_lease(self._live_lease(owner_id="someone-elses-env"), instance="mine"):
				result = environment.check_lease(refresh=True)
				self.assertEqual(result.status, environment.LEASE_CONFLICT)
				self.assertIn("someone-elses-env", result.reason)

	def test_an_unreadable_lease_blocks_destruction_but_not_uploads(self):
		# Losing sight of the bucket's lease means we cannot rule out a second live
		# writer. Deletes wait (a failed delete is queued, nothing is lost); uploads
		# are additive and carry on, so an S3 hiccup does not silently start
		# scattering files onto local disk.
		with self._owner_site():
			self._clear_lease_cache()
			with patch.object(environment, "_read_lease", side_effect=RuntimeError("AccessDenied")):
				self.assertEqual(environment.check_lease(refresh=True).status, environment.LEASE_UNVERIFIED)
				self.assertFalse(environment.may_destroy())
				self.assertTrue(environment.may_modify_storage())
				self.assertTrue(s3_utils.should_store_in_s3(frappe._dict(attached_to_doctype="Lead")))
			self._clear_lease_cache()
		self._seed_lease(environment.LEASE_OK)

	def test_the_lease_is_only_read_for_destructive_work(self):
		# The upload path must not pay for a round-trip to S3 on every file.
		with self._owner_site():
			self._clear_lease_cache()
			with patch.object(environment, "_read_lease") as read:
				s3_utils.should_store_in_s3(frappe._dict(attached_to_doctype="Lead"))
			read.assert_not_called()
		self._seed_lease(environment.LEASE_OK)

	# --- an unproven environment is not a permitted one --------------------

	def test_unclaimed_may_not_modify_anything(self):
		# An old backup carries no owner id at all, so it is unclaimed wherever it
		# is restored. Absence of proof is not permission.
		with self._environment(db_owner="", disk_owner=None):
			self.assertEqual(environment.state(), environment.UNCLAIMED)
			self.assertFalse(environment.may_modify_storage())
			self.assertFalse(environment.may_destroy())
			self.assertIn("No environment has claimed", environment.blocked_reason())

	@patch.object(s3_utils, "get_s3_client")
	def test_unclaimed_does_not_reach_delete_object(self, mock_get_client):
		s3 = MagicMock()
		mock_get_client.return_value = s3
		with self._environment(db_owner="", disk_owner=None):
			s3_utils._delete_keys("test-bucket", ["private/uid/report.pdf"])
		s3.delete_object.assert_not_called()

	def test_unclaimed_uploads_to_local_disk(self):
		with self._environment(db_owner="", disk_owner=None):
			self.assertFalse(s3_utils.should_store_in_s3(frappe._dict(attached_to_doctype="Lead")))

	# --- thumbnails are a write over the source object's own key -----------

	@patch.object(s3_utils, "get_s3_client")
	def test_no_thumbnail_is_written_over_another_environments_object(self, mock_get_client):
		# The thumbnail key is derived from the file's key, so this replaces an
		# object the other environment owns — the per-file check has to apply here
		# exactly as it does to a delete.
		s3 = MagicMock()
		mock_get_client.return_value = s3
		doc = frappe._dict(s3_owner="prod-environment")

		with self._owner_site():
			self.assertIsNone(
				s3_utils.upload_thumbnail("private/uid/pic_small.png", b"x", "image/png", file_doc=doc)
			)
		s3.put_object.assert_not_called()

	@patch.object(s3_utils, "get_s3_client")
	def test_no_thumbnail_is_written_while_the_lease_is_contested(self, mock_get_client):
		s3 = MagicMock()
		mock_get_client.return_value = s3
		lease = self._live_lease(owner_id="this-environment", instance="the-original-server")

		with self._owner_site(), self._with_lease(lease, instance="the-copy"):
			self.assertIsNone(s3_utils.upload_thumbnail("private/uid/pic_small.png", b"x", "image/png"))
		s3.put_object.assert_not_called()

	def test_a_copied_attachment_keeps_the_objects_owner(self):
		# s3_owner is recovered from the record the key came from. Without this a
		# copy (an Amend, a record built from a URL) arrives with no owner, which
		# the guard reads as "unknown" and lets through — for precisely the
		# inherited files that need protecting.
		from aws_s3_storage.aws_s3_storage.file_override import S3File

		key = "private/uid/inherited.pdf"
		frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "inherited.pdf",
				"file_url": s3_utils._build_file_url(key),
				"s3_key": key,
				"s3_owner": "prod-environment",
				"is_private": 1,
			}
		).insert(ignore_permissions=True)
		# Left to the test transaction's rollback on purpose: deleting it here would
		# run after that rollback, and would go through the S3 delete path.

		copy = S3File(
			{"doctype": "File", "file_name": "inherited.pdf", "file_url": s3_utils._build_file_url(key)}
		)
		copy._backfill_s3_keys()

		self.assertEqual(copy.s3_key, key)
		self.assertEqual(copy.s3_owner, "prod-environment")
		with self._owner_site():
			self.assertFalse(environment.may_modify_object(copy))

	# --- a queued deletion is a request, not a fact ------------------------

	@patch.object(s3_utils, "get_s3_client")
	def test_a_deletion_queued_by_another_environment_is_never_executed(self, mock_get_client):
		# The scenario the environment check alone misses: a restored copy takes
		# ownership of the bucket (deliberately) *before* the inherited backlog is
		# parked. Ownership does not make production's queued decisions this
		# site's to carry out.
		row = self._queue_row("private/uid/report.pdf")
		frappe.db.set_value("S3 Deletion Queue", row.name, "requested_by_environment", "prod-environment")
		s3 = MagicMock()
		mock_get_client.return_value = s3

		with self._owner_site(), patch.object(frappe.db, "commit"):
			s3_utils.process_deletion_queue()

		s3.delete_object.assert_not_called()
		self.assertEqual(frappe.db.get_value("S3 Deletion Queue", row.name, "status"), "Blocked")

	@patch.object(s3_utils, "get_s3_client")
	def test_a_deletion_naming_another_environments_object_is_never_executed(self, mock_get_client):
		row = self._queue_row("private/uid/report.pdf")
		frappe.db.set_value(
			"S3 Deletion Queue",
			row.name,
			{"requested_by_environment": "this-environment", "object_owner": "prod-environment"},
		)
		s3 = MagicMock()
		mock_get_client.return_value = s3

		with self._owner_site(), patch.object(frappe.db, "commit"):
			s3_utils.process_deletion_queue()

		s3.delete_object.assert_not_called()
		self.assertEqual(frappe.db.get_value("S3 Deletion Queue", row.name, "status"), "Blocked")

	@patch.object(s3_utils, "get_s3_client")
	def test_this_environments_own_request_still_runs(self, mock_get_client):
		row = self._queue_row("private/uid/report.pdf")
		frappe.db.set_value("S3 Deletion Queue", row.name, "requested_by_environment", "this-environment")
		s3 = MagicMock()
		mock_get_client.return_value = s3

		with (
			self._owner_site(),
			patch.object(s3_utils, "_key_is_referenced", return_value=False),
			patch.object(frappe.db, "commit"),
		):
			s3_utils.process_deletion_queue()

		s3.delete_object.assert_called_once()

	def test_a_queued_deletion_records_who_asked_and_for_whose_object(self):
		with self._owner_site(), patch.object(frappe.db, "commit"):
			s3_utils._insert_deletion_row(
				"test-bucket", "private/uid/report.pdf", error="network", object_owner="prod-environment"
			)
		name = frappe.db.get_value("S3 Deletion Queue", {"s3_key": "private/uid/report.pdf"})
		row = frappe.db.get_value(
			"S3 Deletion Queue", name, ["requested_by_environment", "object_owner"], as_dict=True
		)
		self.assertEqual(row.object_owner, "prod-environment")
		self.assertTrue(row.requested_by_environment)

	# --- instance identity -------------------------------------------------

	def test_an_explicit_instance_id_overrides_the_derived_one(self):
		# The only thing that separates a bit-identical clone from its original is
		# something the deployment sets from outside the copied filesystem.
		derived = environment.instance_id()
		with patch.dict(os.environ, {environment.INSTANCE_ENV_VAR: "blue-deployment"}):
			self.assertEqual(environment.instance_id(), "blue-deployment")
		self.assertEqual(environment.instance_id(), derived)

	# --- the lease cannot be won by two servers at once --------------------

	def test_a_missing_lease_grants_nothing_to_anybody(self):
		"""Whoever asks first must not win an empty bucket.

		Every copy of a server passes the local checks — that is why the lease
		exists at all — so "the first site to find no lease creates one and may
		then delete" hands ownership to the test copy exactly as readily as to
		production. A bucket with no lease is initialised once, deliberately.
		"""
		with self._owner_site():
			self._clear_lease_cache()
			with (
				patch.object(environment, "_read_lease", return_value=(None, None)),
				patch.object(environment, "_put_lease") as put,
			):
				result = environment.check_lease(refresh=True)
				self.assertEqual(result.status, environment.LEASE_UNVERIFIED)
				self.assertFalse(environment.may_destroy())
			put.assert_not_called()
			self._clear_lease_cache()
		self._seed_lease(environment.LEASE_OK)

	def test_bench_migrate_never_replaces_an_existing_lease(self):
		# The automatic half of claiming. Restoring an old database with no owner
		# id and running bench migrate must not take production's lease — the
		# documentation promises only a person can do that.
		held = self._live_lease(owner_id="prod-environment", instance="prod-server")
		with self._environment(db_owner="", disk_owner=None):
			with (
				patch.object(environment, "_read_lease", return_value=(held, '"etag"')),
				patch.object(environment, "_put_lease", return_value=(False, "precondition")) as put,
				patch.object(environment, "_write_local_owner_id"),
			):
				environment.claim_if_unclaimed()
			# It may *try* to create one, but only conditionally — never a replace.
			for call in put.call_args_list:
				self.assertEqual(call.kwargs.get("if_none_match"), "*")
				self.assertIsNone(call.kwargs.get("if_match"))

	def test_read_only_mode_writes_no_lease_either(self):
		# Writing the lease is a write to the bucket like any other.
		frappe.db.set_single_value("S3 Settings", "read_only_mode", 1)
		with self._environment(db_owner="", disk_owner=None):
			with (
				patch.object(environment, "_read_lease", return_value=(None, None)),
				patch.object(environment, "_put_lease") as put,
				patch.object(environment, "_write_local_owner_id"),
			):
				environment.claim_if_unclaimed()
			put.assert_not_called()

	def test_losing_the_heartbeat_race_withdraws_permission(self):
		"""Direct evidence that ownership moved must not be answered with "yes".

		The conditional refresh being rejected means the lease changed between the
		read and the write. That is not the documented 60-second cache window —
		it is proof, in hand, that this server no longer holds the lease.
		"""
		ours = self._live_lease(instance="old-server")
		ours["heartbeat_at"] = add_to_date(
			now_datetime(), seconds=-(environment.LEASE_HEARTBEAT_INTERVAL + 60)
		).isoformat()
		theirs = self._live_lease(instance="new-server")

		with self._owner_site():
			self._clear_lease_cache()
			with (
				patch.object(environment, "instance_id", return_value="old-server"),
				patch.object(environment, "_read_lease", side_effect=[(ours, '"old"'), (theirs, '"new"')]),
				patch.object(environment, "_put_lease", return_value=(False, "precondition")),
			):
				result = environment.check_lease(refresh=True)
				self.assertEqual(result.status, environment.LEASE_CONFLICT)
				self.assertFalse(environment.may_destroy())
			self._clear_lease_cache()
		self._seed_lease(environment.LEASE_OK)

	def test_losing_a_takeover_race_is_not_a_licence_to_skip_the_condition(self):
		# The two failures must never be conflated: "this endpoint cannot do
		# conditional writes" is a missing capability, "somebody else got there
		# first" is the condition doing its job.
		with self._owner_site():
			with (
				patch.object(
					environment, "_read_lease", return_value=(self._live_lease(instance="them"), '"e"')
				),
				patch.object(environment, "_put_lease", return_value=(False, "precondition")) as put,
			):
				with self.assertRaises(RuntimeError):
					environment._take_lease_explicitly(frappe.get_single("S3 Settings"))
			# Every attempt carried a condition; none was retried without one.
			for call in put.call_args_list:
				self.assertTrue(call.kwargs.get("if_match") or call.kwargs.get("if_none_match"))

	def test_an_endpoint_without_conditional_writes_does_not_take_the_lease(self):
		# Refusing beats racing: an S3-compatible service that cannot do
		# conditional writes cannot give a safe answer automatically...
		with self._owner_site():
			self._clear_lease_cache()
			with (
				patch.object(
					environment, "_read_lease", return_value=(self._live_lease(instance="me"), '"e"')
				),
				patch.object(environment, "instance_id", return_value="me"),
				patch.object(environment, "_put_lease", return_value=(False, "unconditional")),
			):
				# The heartbeat cannot be written, but we do still hold the lease.
				self.assertEqual(environment.check_lease(refresh=True).status, environment.LEASE_OK)
			self._clear_lease_cache()
		self._seed_lease(environment.LEASE_OK)

	def test_an_unsupported_endpoint_still_allows_a_person_to_take_over(self):
		# ... but a person asking explicitly is a different matter, and it is logged.
		def conditional_writes_unsupported(settings, payload, if_match=None, if_none_match=None):
			return (False, "unconditional") if (if_match or if_none_match) else (True, None)

		with self._owner_site():
			with (
				patch.object(
					environment, "_read_lease", return_value=(self._live_lease(instance="them"), '"e"')
				),
				patch.object(environment, "_put_lease", side_effect=conditional_writes_unsupported),
			):
				self.assertTrue(environment._take_lease_explicitly(frappe.get_single("S3 Settings")))

	def test_reclaiming_as_the_owner_keeps_the_owner_id(self):
		# The id is stamped on every File.s3_owner, so minting a new one on a
		# routine "take ownership" would leave the owner unable to touch its own
		# files.
		with self._owner_site():
			with (
				patch.object(environment, "_write_local_owner_id"),
				patch.object(environment, "_take_lease_explicitly"),
			):
				environment.claim(force=True)
			self.assertEqual(environment.recorded_owner_id(), "this-environment")

	def test_a_restored_copy_cannot_adopt_deletion_requests(self):
		# Holding an owner id proves nothing — the copy holds the same one,
		# because it came out of the same database.
		with self._restored_copy():
			with self.assertRaises(frappe.ValidationError):
				environment.adopt_unattributed_deletions()

	def test_the_heartbeat_never_overwrites_a_takeover(self):
		lease = self._live_lease(instance="me")
		lease["heartbeat_at"] = add_to_date(
			now_datetime(), seconds=-(environment.LEASE_HEARTBEAT_INTERVAL + 60)
		).isoformat()

		with self._owner_site():
			self._clear_lease_cache()
			with (
				patch.object(environment, "instance_id", return_value="me"),
				patch.object(environment, "_read_lease", return_value=(lease, '"etag-1"')),
				patch.object(environment, "_put_lease", return_value=(True, None)) as put,
			):
				environment.check_lease(refresh=True)
			# Conditional on the ETag we actually read, so a takeover in between wins.
			self.assertEqual(put.call_args.kwargs.get("if_match"), '"etag-1"')
			self._clear_lease_cache()
		self._seed_lease(environment.LEASE_OK)

	# --- the cached answer is per server, and short-lived ------------------

	def test_the_lease_cache_is_not_shared_between_servers(self):
		# frappe.cache() is the site's Redis, which several application servers of
		# the same site share. A key naming only the bucket would let a second
		# server read the first one's "yes".
		with self._owner_site():
			with patch.object(environment, "instance_id", return_value="server-a"):
				key_a = environment._lease_cache_key(frappe.get_single("S3 Settings"))
			with patch.object(environment, "instance_id", return_value="server-b"):
				key_b = environment._lease_cache_key(frappe.get_single("S3 Settings"))
		self.assertNotEqual(key_a, key_b)

	def test_a_second_server_does_not_inherit_the_first_ones_answer(self):
		lease = self._live_lease(instance="server-a")
		with self._owner_site():
			with self._with_lease(lease, instance="server-a"):
				self.assertEqual(environment.check_lease().status, environment.LEASE_OK)
			# Same Redis, same bucket, same recorded owner — different machine.
			with self._with_lease(lease, instance="server-b"):
				self.assertEqual(environment.check_lease().status, environment.LEASE_CONFLICT)

	def test_the_cached_answer_is_short_lived(self):
		# The bound on how long a server that just lost ownership keeps acting on
		# its old answer. Asserted so it cannot drift back up unnoticed.
		self.assertLessEqual(environment.LEASE_CHECK_INTERVAL, 60)

	# --- an unattributed deletion request is not an authorised one ---------

	@patch.object(s3_utils, "get_s3_client")
	def test_a_deletion_request_with_no_recorded_environment_is_parked(self, mock_get_client):
		# Rows written before the column existed cannot be told apart from rows a
		# restored copy brought with it. "We do not know who asked for this" is not
		# permission to carry it out.
		row = self._queue_row("private/uid/legacy.pdf")
		frappe.db.set_value("S3 Deletion Queue", row.name, "requested_by_environment", None)
		s3 = MagicMock()
		mock_get_client.return_value = s3

		with self._owner_site(), patch.object(frappe.db, "commit"):
			s3_utils.process_deletion_queue()

		s3.delete_object.assert_not_called()
		self.assertEqual(frappe.db.get_value("S3 Deletion Queue", row.name, "status"), "Blocked")

	@patch.object(s3_utils, "get_s3_client")
	def test_adopting_unattributed_requests_makes_them_runnable(self, mock_get_client):
		row = self._queue_row("private/uid/legacy.pdf")
		frappe.db.set_value("S3 Deletion Queue", row.name, "requested_by_environment", None)
		s3 = MagicMock()
		mock_get_client.return_value = s3

		# Adoption re-reads the lease rather than trusting the cache: it turns a
		# parked request into a live deletion, so it takes a deletion's proof.
		with (
			self._owner_site(),
			self._with_lease(self._live_lease(instance="me"), instance="me"),
			patch.object(frappe.db, "commit"),
		):
			adopted = environment.adopt_unattributed_deletions()
			self.assertGreaterEqual(adopted["adopted"], 1)
			self.assertEqual(
				frappe.db.get_value("S3 Deletion Queue", row.name, "requested_by_environment"),
				"this-environment",
			)
			with patch.object(s3_utils, "_key_is_referenced", return_value=False):
				s3_utils.process_deletion_queue()

		s3.delete_object.assert_called_once()
