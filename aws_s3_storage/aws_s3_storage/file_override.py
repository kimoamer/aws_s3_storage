# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from io import BytesIO

import frappe
from frappe.core.doctype.file.file import File

from aws_s3_storage.aws_s3_storage import s3_utils


class S3File(File):
	"""File doctype override that reads content back from S3.

	Frappe reads file content from the local disk (``get_content``) and fetches
	remote files over HTTP (``make_thumbnail``). Neither works for our private S3
	objects — the HTTP path is a session-less self-request that fails the
	permission check — so both are routed through boto3 instead.
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

		# Store the thumbnail next to its source object, preserving privacy
		# (a private image keeps its "private/" prefix, so it is not exposed).
		base, dot, ext = key.rpartition(".")
		thumb_key = f"{base}_{suffix}.{ext}" if dot else f"{key}_{suffix}"
		content_type = Image.MIME.get(image_format, "image/png")

		thumbnail_url = s3_utils.upload_thumbnail(thumb_key, buffer.getvalue(), content_type)
		if set_as_thumbnail:
			self.db_set("thumbnail_url", thumbnail_url)
		return thumbnail_url
