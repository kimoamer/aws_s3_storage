# Copyright (c) 2026, Frappe and contributors
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
