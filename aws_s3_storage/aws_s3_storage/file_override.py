# Copyright (c) 2026, Innomate LLC
# For license information, please see license.txt

from io import BytesIO

import frappe
from botocore.exceptions import ClientError
from frappe.core.doctype.file.file import File

from aws_s3_storage.aws_s3_storage import s3_utils


class S3File(File):
	"""File doctype override that keeps S3-backed files consistent.

	Frappe reads content from local disk, fetches remote files over HTTP, skips
	moving remote files when privacy changes, and can't detect S3 objects for
	deduplication. Each of those is routed through boto3 instead.
	"""

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)

		# Frappe's File.check_content() accesses _content directly.
		# Attachment copies created during Amend may reference an existing
		# S3 object without loading its content first.
		self._content = getattr(self, "_content", None)

	def _key(self):
		"""The record's S3 key, preferring the stored column over the URL.

		File.validate() runs ``self.file_url = unquote(self.file_url)`` before the
		rest of validation, so mid-save the URL is no longer the canonical encoded
		one this app writes. A key containing '&' or '#' then parses short (the
		query splits on '&', urlparse cuts at '#') and every read of it 404s, even
		though the stored record is perfectly fine. The s3_key column is not
		URL-encoded and cannot be mangled that way, so it is the better source.
		"""
		return self.get("s3_key") or s3_utils._extract_key(self.file_url)

	def _read_s3(self, key, required=True):
		"""Read an object, telling "it is gone" apart from a real S3 failure.

		A File row can outlive its object (deleted from the bucket, a lifecycle
		rule, a site restored against another bucket). Frappe simply skips the PDF
		scan when a file has no content, so a missing object must not be fatal
		there: Document.copy_attachments_from_amended_from() re-inserts every
		attachment as a new File, and one orphaned pointer would otherwise block
		every Amend of the document it hangs off.
		"""
		try:
			return s3_utils.read_file_from_s3(key)
		except ClientError as e:
			if e.response.get("Error", {}).get("Code") not in s3_utils._NOT_FOUND_CODES:
				raise

			# get(): a File being inserted has no name yet on every code path.
			frappe.logger().warning(
				f"aws_s3_storage: object missing for File {self.get('name') or self.get('file_name')}: {key}"
			)
			if required:
				# Same failure mode Frappe gives for a missing local file, but naming
				# the attachment and the key instead of a raw botocore error.
				raise FileNotFoundError(
					f"Attachment '{self.get('file_name')}' is not in the S3 bucket (key: {key})"
				) from e

			return None

	def validate(self):
		super().validate()
		self._restore_canonical_urls()

	def _restore_canonical_urls(self):
		"""Undo the ``unquote()`` Frappe applies to file_url at the top of validate().

		Left alone, the decoded URL is what gets written to the database, so a file
		whose name contains '&' or '#' ends up with a URL that no longer parses back
		to its own key — the record then 404s on download even though the object is
		untouched. Rebuilding from the (unencoded) keys keeps the stored URLs
		canonical no matter how often the record is saved.
		"""
		if self.get("s3_key"):
			self.file_url = s3_utils._build_file_url(self.s3_key)
		if self.get("s3_thumbnail_key"):
			self.thumbnail_url = s3_utils._build_file_url(self.s3_thumbnail_key)

	def check_content(self):
		"""Load S3-backed PDF content before Frappe performs its security check."""

		if self.file_type == "PDF" and not self._content:
			if self.get("content"):
				self._content = self.get_content()
			else:
				key = self._key()
				if key:
					# No content -> Frappe skips the scan, exactly as for a local file
					# it cannot read. Never fail the whole save over it.
					self._content = self._read_s3(key, required=False)

		return super().check_content()

	def save_file(self, *args, **kwargs):
		# Frappe's content-hash deduplication runs *before* the write_file hook, so a
		# file that must stay on disk could still inherit another record's S3 URL (and
		# then fail when its owner reopens it by path). It would also make two records
		# share one file, and these are rewritten in place. Always give them their own.
		#
		# The arguments are passed straight through (ignore_existing_file_check is the
		# third one) so this keeps working if Frappe's signature changes.
		if s3_utils.is_local_only_file(self):
			if len(args) >= 3:
				args = (*args[:2], True, *args[3:])
			else:
				kwargs["ignore_existing_file_check"] = True

		return super().save_file(*args, **kwargs)

	@property
	def is_remote_file(self):
		# Older Frappe (e.g. v15.69) only treats http(s) URLs as remote, so our
		# relative "/api/method/...download_file" URL would be validated as a local
		# path and rejected ("The File URL you've entered is incorrect"). Recognising
		# it here makes validate_file_path / validate_file_url short-circuit on every
		# Frappe 15 build, old or new.
		if self._key():
			return True
		return super().is_remote_file

	def set_is_private(self):
		# Frappe derives privacy from the URL (`file_url.startswith("/private")`),
		# which is always false for our "/api/method/..." URLs — so an attachment
		# copied onto another document (Amend) silently arrives as public. The key's
		# own prefix is where privacy actually lives for an S3 object, so read it
		# from there instead. Only insert is affected: Frappe does not call this on
		# update, so a deliberate privacy change still goes through untouched.
		key = self._key()
		if not key:
			return super().set_is_private()

		if key.startswith("private/"):
			self.is_private = 1
		elif key.startswith("public/"):
			self.is_private = 0

	def before_insert(self):
		# Capture the key before Frappe applies unquote() to file_url (in validate()
		# on v15.80, in before_insert() on newer builds). This is especially important
		# when copying attachments during Amend, where an encoded %2B can otherwise
		# become a raw '+'.
		existing_key = s3_utils._extract_key(self.file_url) or self.get("s3_key")

		existing_thumbnail_key = s3_utils._extract_key(self.get("thumbnail_url")) or self.get(
			"s3_thumbnail_key"
		)

		super().before_insert()

		# Rebuild canonical encoded URLs after Frappe has processed the copied File.
		if existing_key:
			self.s3_key = existing_key
			self.file_url = s3_utils._build_file_url(existing_key)

		if existing_thumbnail_key:
			self.s3_thumbnail_key = existing_thumbnail_key
			self.thumbnail_url = s3_utils._build_file_url(existing_thumbnail_key)

		self._backfill_s3_keys()
		self._enforce_storage_location()

	def _enforce_storage_location(self):
		"""Undo a content-hash reuse that would store this file in the wrong place.

		Frappe deduplicates twice while inserting a File — once in ``save_file()``
		and again in ``validate_duplicate_entry()`` — and both simply copy the
		matching record's ``file_url``, wherever that record happens to live. With a
		doctype scope configured that silently breaks it in both directions: an
		out-of-scope attachment inherits an S3 object, and an in-scope one is left on
		local disk because an identical file was uploaded before the bucket existed.
		Neither reuse is visible anywhere — the record simply ends up on the wrong
		storage forever.

		So after Frappe is done, compare where the file *is* with where it belongs
		and, when they disagree, write the content again with deduplication turned
		off. Nothing was written in the reuse case, so this is the file's only write.

		Copies that carry no content of their own (an Amend re-inserting an
		attachment, a record built straight from a URL) are left alone: there is
		nothing to write, and the object they point at belongs to the record they
		were copied from.

		The same mechanism is what gives a restored copy of another site's database
		its own files: there ``should_store_in_s3`` answers False for everything, so
		an upload that Frappe deduplicated onto an inherited S3 object is written to
		this site's local disk instead — a real, separate copy — and the object the
		owning site is still serving is never touched.
		"""
		if self.get("is_folder") or self.flags.get("copy_from_existing_file"):
			return

		# _content is the decoded payload; preferring it avoids decoding twice.
		content = getattr(self, "_content", None) or self.get("content")
		if not content:
			return

		if s3_utils.should_store_in_s3(self) == bool(self._key()):
			return

		self.file_url = None
		self.s3_key = None
		self.s3_thumbnail_key = None
		# save_file() checks is_remote_file on its first line, and with no URL that
		# reads self.content — set it, or the rewrite would silently do nothing.
		self.content = content
		self.save_file(content=content, ignore_existing_file_check=True)
		self._backfill_s3_keys()

	def _backfill_s3_keys(self):
		if not self.get("s3_key"):
			key = s3_utils._extract_key(self.file_url)
			if key:
				self.s3_key = key
		if not self.get("s3_thumbnail_key") and self.get("thumbnail_url"):
			thumb_key = s3_utils._extract_key(self.thumbnail_url)
			if thumb_key:
				self.s3_thumbnail_key = thumb_key

	def get_full_path(self):
		# Frappe's get_full_path() runs the "/api/method/..." URL through is_safe_path()
		# and rejects it ("Cannot access file path") while saving the record. For an
		# S3-backed file the URL *is* the location, so return it directly and skip the
		# local-filesystem path handling.
		if self._key():
			return self.file_url
		return super().get_full_path()

	def validate_file_on_disk(self):
		# S3-backed files never live on the local disk.
		if self._key():
			return True
		return super().validate_file_on_disk()

	def get_content(self) -> bytes:
		if not self.get("content") and self.file_url:
			key = self._key()
			if key:
				content = self._read_s3(key)
				# Mirror Frappe's behaviour of returning text as str when decodable.
				try:
					self._content = content.decode()
				except (UnicodeDecodeError, AttributeError):
					self._content = content
				return self._content
		return super().get_content()

	def exists_on_disk(self):
		# An S3-backed record is considered "present" so Frappe's content-hash
		# deduplication reuses the existing object instead of re-uploading it.
		if self._key():
			return True
		return super().exists_on_disk()

	def handle_is_private_changed(self):
		# Frappe's default skips remote files (ours are remote), which would leave the
		# object under the wrong prefix and, worse, a "private" record reachable under
		# public/. Move the object to match the new privacy instead.
		if not self._key():
			return super().handle_is_private_changed()

		if not self.get_doc_before_save():
			# Frappe reaches this during insert as well: is_new() returns None for a
			# document built from a dict (as Amend does) and has_value_changed() is
			# True whenever there is nothing to compare against. A record being
			# inserted has no previous privacy, so there is nothing to move — the key
			# it points at already carries the right prefix (see set_is_private).
			return

		s3_utils.move_object_privacy(self)

	def make_thumbnail(
		self,
		set_as_thumbnail: bool = True,
		width: int = 300,
		height: int = 300,
		suffix: str = "small",
		crop: bool = False,
	) -> str | None:
		key = self._key()
		if not key:
			# Local file — Frappe's default (disk-based) thumbnailing is fine.
			return super().make_thumbnail(set_as_thumbnail, width, height, suffix, crop)

		from PIL import Image, ImageOps

		try:
			image = Image.open(BytesIO(s3_utils.read_file_from_s3(key)))
			image_format = image.format or "PNG"
		except Exception as e:
			frappe.logger().error(f"S3 Thumbnail Error for {key}: {e}")
			return None

		size = (width, height)
		if crop:
			image = ImageOps.fit(image, size, Image.Resampling.LANCZOS)
		else:
			image.thumbnail(size, Image.Resampling.LANCZOS)

		buffer = BytesIO()
		image.save(buffer, format=image_format)

		# Store the thumbnail next to its source object, preserving privacy.
		base, dot, ext = key.rpartition(".")
		thumb_key = f"{base}_{suffix}.{ext}" if dot else f"{key}_{suffix}"
		content_type = Image.MIME.get(image_format, "image/png")

		thumbnail_url = s3_utils.upload_thumbnail(thumb_key, buffer.getvalue(), content_type)
		if not thumbnail_url:
			# Refused: this environment does not own the bucket (or it is read-only).
			# The record keeps whatever thumbnail it already had — writing a URL for
			# an object that was never uploaded would just produce a broken preview.
			return None
		if set_as_thumbnail:
			self.db_set({"thumbnail_url": thumbnail_url, "s3_thumbnail_key": thumb_key})
		return thumbnail_url
