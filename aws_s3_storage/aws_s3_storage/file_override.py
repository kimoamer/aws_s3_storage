# Copyright (c) 2026, Innomate LLC
# For license information, please see license.txt

from io import BytesIO

import frappe
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

	def check_content(self):
		"""Load S3-backed PDF content before Frappe performs its security check."""

		if self.file_type == "PDF" and not self._content:
			if self.get("content"):
				self._content = self.get_content()
			else:
				key = s3_utils._extract_key(self.file_url)
				if key:
					self._content = s3_utils.read_file_from_s3(key)

		return super().check_content()
	@property
	def is_remote_file(self):
		# Older Frappe (e.g. v15.69) only treats http(s) URLs as remote, so our
		# relative "/api/method/...download_file" URL would be validated as a local
		# path and rejected ("The File URL you've entered is incorrect"). Recognising
		# it here makes validate_file_path / validate_file_url short-circuit on every
		# Frappe 15 build, old or new.
		if self.file_url and s3_utils._extract_key(self.file_url):
			return True
		return super().is_remote_file

	def before_insert(self):
		# Capture the key before Frappe's File.before_insert() applies unquote()
		# to file_url. This is especially important when copying attachments
		# during Amend, where an encoded %2B can otherwise become a raw '+'.
		existing_key = (
			s3_utils._extract_key(self.file_url)
			or self.get("s3_key")
		)
	
		existing_thumbnail_key = (
			s3_utils._extract_key(self.get("thumbnail_url"))
			or self.get("s3_thumbnail_key")
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
		if s3_utils._extract_key(self.file_url):
			return self.file_url
		return super().get_full_path()

	def validate_file_on_disk(self):
		# S3-backed files never live on the local disk.
		if s3_utils._extract_key(self.file_url):
			return True
		return super().validate_file_on_disk()

	def get_content(self) -> bytes:
		if not self.get("content") and self.file_url:
			key = s3_utils._extract_key(self.file_url)
			if key:
				content = s3_utils.read_file_from_s3(key)
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
		if s3_utils._extract_key(self.file_url):
			return True
		return super().exists_on_disk()

	def handle_is_private_changed(self):
		# Frappe's default skips remote files (ours are remote), which would leave the
		# object under the wrong prefix and, worse, a "private" record reachable under
		# public/. Move the object to match the new privacy instead.
		if s3_utils._extract_key(self.file_url):
			s3_utils.move_object_privacy(self)
			return
		return super().handle_is_private_changed()

	def make_thumbnail(
		self,
		set_as_thumbnail: bool = True,
		width: int = 300,
		height: int = 300,
		suffix: str = "small",
		crop: bool = False,
	) -> str | None:
		key = s3_utils._extract_key(self.file_url) if self.file_url else None
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
		if set_as_thumbnail:
			self.db_set({"thumbnail_url": thumbnail_url, "s3_thumbnail_key": thumb_key})
		return thumbnail_url
