# Copyright (c) 2026, Innomate LLC
# For license information, please see license.txt
"""Repair File records whose S3 pointer was mangled by Frappe's URL handling.

``File.validate()`` starts with ``self.file_url = unquote(self.file_url)``, and
that decoded URL is what gets written back to the database. For a key holding a
'&' or a '#' the URL then no longer parses back to its own key — the query splits
on '&' and urlparse() cuts at '#' — so the record 404s on download and any later
save (an Amend re-inserts every attachment) reads a truncated key::

    NoSuchKey: The specified key does not exist.

New saves keep their URLs canonical (S3File._restore_canonical_urls). This patch
fixes the records already stored:

* fills ``s3_key`` / ``s3_thumbnail_key`` where only the URL carries the key,
  recovering the real key from the bucket when the URL is already truncated,
* rewrites every ``file_url`` / ``thumbnail_url`` that is not the canonical
  encoded form of its key,
* restores ``is_private`` where the key says private but the record says public
  (Frappe reads privacy off the URL prefix, which never matches an S3 URL).

Records whose real key cannot be identified are reported and left untouched.
Idempotent — a second run finds nothing to repair.
"""

import frappe

from aws_s3_storage.aws_s3_storage import s3_utils


def execute():
	if not frappe.db.has_column("File", "s3_key"):
		# App installed but custom fields not created yet — nothing can be in S3.
		return

	settings = frappe.get_single("S3 Settings")
	if not settings.bucket_name:
		return

	bucket = _Bucket(settings)
	repaired, unresolved = 0, []

	for row in _s3_backed_files():
		update = {}

		key = row.s3_key
		if not key and row.file_url:
			key, note = _key_from_url(row.file_url, bucket, file_name=row.file_name)
			if key:
				update["s3_key"] = key
			elif note:
				unresolved.append(f"File {row.name}: {note}")

		if key and row.file_url and s3_utils._build_file_url(key) != row.file_url:
			update["file_url"] = s3_utils._build_file_url(key)

		thumbnail_key = row.s3_thumbnail_key
		if not thumbnail_key and row.thumbnail_url:
			# The thumbnail shares the object folder with its source (make_thumbnail),
			# so exclude the main key to keep the lookup unambiguous.
			thumbnail_key, note = _key_from_url(row.thumbnail_url, bucket, exclude={key})
			if thumbnail_key:
				update["s3_thumbnail_key"] = thumbnail_key
			elif note:
				unresolved.append(f"File {row.name} (thumbnail): {note}")

		if thumbnail_key and s3_utils._build_file_url(thumbnail_key) != row.thumbnail_url:
			update["thumbnail_url"] = s3_utils._build_file_url(thumbnail_key)

		if key and key.startswith("private/") and not row.is_private:
			# Frappe's set_is_private() derives privacy from "/private" at the start of
			# the URL, which no S3 URL has, so private attachments were stored as public.
			update["is_private"] = 1

		if update:
			frappe.db.set_value("File", row.name, update, update_modified=False)
			repaired += 1
			if repaired % 500 == 0:
				frappe.db.commit()

	frappe.db.commit()

	if repaired:
		print(f"aws_s3_storage: repaired {repaired} File record(s)")
	for note in unresolved:
		print(f"aws_s3_storage: {note}")


class _Bucket:
	"""Bucket handle that builds its boto3 client only if a record needs a lookup."""

	def __init__(self, settings):
		self.name = settings.bucket_name
		self._s3 = None

	@property
	def s3(self):
		if self._s3 is None:
			self._s3 = s3_utils.get_s3_client()
		return self._s3


def _key_from_url(url, bucket, file_name=None, exclude=()):
	"""Return ``(key, note)`` for a stored URL: the note is set only on failure."""
	key = s3_utils._extract_key(url)
	if not key:
		return None, None

	if s3_utils._build_file_url(key) == url:
		# Canonical URL — what it parses back to is the key, no lookup needed.
		return key, None

	resolved = _resolve_key(key, bucket, file_name=file_name, exclude=exclude)
	if resolved:
		return resolved, None

	return None, f"could not resolve S3 key from {url}"


def _resolve_key(key, bucket, file_name=None, exclude=()):
	"""Return the real key for one parsed out of a mangled URL, or None.

	A truncated key is still a unique prefix: keys are built as
	"<prefix>/<uuid>/<filename>" (s3_utils._new_key), so listing the uuid folder
	turns "private/<uuid>/Quotation A" back into "private/<uuid>/Quotation A&B.pdf".
	The folder holds the file's thumbnails as well, hence the two tie-breakers —
	anything still ambiguous returns None rather than a guess.
	"""
	if s3_utils.object_exists(key, s3=bucket.s3, bucket=bucket.name):
		return key

	prefix, _, _ = key.rpartition("/")
	parts = key.split("/")
	if len(parts) < 3 or parts[0] not in ("public", "private"):
		return None

	response = bucket.s3.list_objects_v2(Bucket=bucket.name, Prefix=f"{prefix}/", MaxKeys=100)
	matches = [
		item["Key"]
		for item in response.get("Contents") or []
		if item["Key"].startswith(key) and item["Key"] not in exclude
	]

	if len(matches) == 1:
		return matches[0]

	# The record keeps the untruncated file name, which is the key's last segment.
	named = [k for k in matches if k.rsplit("/", 1)[-1] == file_name] if file_name else []
	return named[0] if len(named) == 1 else None


def _s3_backed_files():
	"""File rows served from S3, whether or not their key columns are filled."""
	rows = {}
	for field in ("file_url", "thumbnail_url"):
		for row in frappe.get_all(
			"File",
			filters={"is_folder": 0, field: ["like", f"%{s3_utils.DOWNLOAD_METHOD}%"]},
			fields=[
				"name",
				"file_name",
				"file_url",
				"thumbnail_url",
				"s3_key",
				"s3_thumbnail_key",
				"is_private",
			],
		):
			rows[row.name] = row

	return list(rows.values())
