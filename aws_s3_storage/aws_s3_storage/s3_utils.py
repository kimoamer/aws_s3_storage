import base64
import hashlib
import mimetypes
import uuid
from urllib.parse import parse_qs, quote, urlparse

import boto3
import frappe
from botocore.config import Config
from botocore.exceptions import ClientError
from frappe.model.document import Document
from frappe.utils import cint, get_url

# Whitelisted method used to serve files back from S3 via short-lived presigned URLs.
DOWNLOAD_METHOD = "aws_s3_storage.aws_s3_storage.s3_utils.download_file"
# Fallback lifetime of a generated presigned URL, in seconds, when not configured.
DEFAULT_PRESIGNED_URL_EXPIRY = 3600
# S3's hard limits for SigV4 presigned URL expiry.
MIN_PRESIGNED_URL_EXPIRY = 60
MAX_PRESIGNED_URL_EXPIRY = 604800  # 7 days
# S3 error codes that mean "object does not exist".
_NOT_FOUND_CODES = {"404", "NoSuchKey", "NotFound"}


def _boto_config(endpoint_url=None):
	kwargs = {
		"retries": {"max_attempts": 3, "mode": "standard"},
		"connect_timeout": 10,
		"read_timeout": 60,
	}
	if endpoint_url:
		# S3-compatible endpoints (MinIO, Spaces, Wasabi, ...) usually need path-style URLs.
		kwargs["s3"] = {"addressing_style": "path"}
	return Config(**kwargs)


def get_s3_client():
	settings = frappe.get_single("S3 Settings")
	secret = settings.get_password("secret_access_key", raise_exception=False)
	if not settings.bucket_name or not settings.access_key_id or not secret:
		frappe.throw("S3 Settings are not fully configured")

	endpoint_url = (settings.get("endpoint_url") or "").strip() or None

	return boto3.client(
		"s3",
		aws_access_key_id=settings.access_key_id,
		aws_secret_access_key=secret,
		region_name=settings.region,
		endpoint_url=endpoint_url,
		config=_boto_config(endpoint_url),
	)


def _presigned_expiry(settings):
	expiry = cint(settings.get("presigned_url_expiry")) or DEFAULT_PRESIGNED_URL_EXPIRY
	return max(MIN_PRESIGNED_URL_EXPIRY, min(expiry, MAX_PRESIGNED_URL_EXPIRY))


def _upload_extra_args(settings, content=None):
	"""Build ExtraArgs/put_object kwargs from the configurable S3 Settings.

	``content`` (bytes) is only passed for single-shot put_object uploads so we
	can attach a Content-MD5 header; multipart uploads (upload_file) compute their
	own integrity checks, so it is omitted there.
	"""
	extra = {}

	storage_class = (settings.get("storage_class") or "").strip()
	if storage_class and storage_class != "STANDARD":
		extra["StorageClass"] = storage_class

	if content is not None and cint(settings.get("verify_upload_integrity")):
		# S3 validates the body against this digest and rejects a corrupted upload
		# with BadDigest, so a damaged object is never silently stored.
		digest = hashlib.md5(content, usedforsecurity=False).digest()
		extra["ContentMD5"] = base64.b64encode(digest).decode()

	return extra


def _build_file_url(key):
	# A stable, self-referential URL. It is absolute so Frappe treats the file as
	# remote and never attempts to read/hash it from the local filesystem.
	return f"{get_url()}/api/method/{DOWNLOAD_METHOD}?key={quote(key, safe='')}"


def _extract_key(file_url):
	# Recover the S3 object key that was embedded in the file URL by _build_file_url.
	if not file_url:
		return None
	keys = parse_qs(urlparse(file_url).query).get("key")
	return keys[0] if keys else None


def _object_exists(s3, bucket, key, expected_size=None):
	"""Return True only if the object exists and (optionally) matches expected_size."""
	try:
		head = s3.head_object(Bucket=bucket, Key=key)
	except ClientError as e:
		if e.response.get("Error", {}).get("Code") in _NOT_FOUND_CODES:
			return False
		raise
	if expected_size is not None:
		return head.get("ContentLength") == expected_size
	return True


def write_file_to_s3(file_or_fname, content=None, content_type=None, is_private=0):
	"""Frappe ``write_file`` hook.

	Frappe calls this hook with two different conventions depending on the code
	path, so both are supported:

	* File doctype (frappe/core/doctype/file/file.py) -> ``write_file_to_s3(file_doc)``
	* Legacy file_manager (frappe/utils/file_manager.py) ->
	  ``write_file_to_s3(fname, content, content_type=..., is_private=...)``

	In both cases we upload to S3 and return the dict Frappe expects
	(``file_name``/``file_url``/``file_size``). For the File-document path we also
	set ``file_url`` on the document itself, mirroring ``save_file_on_filesystem``.
	"""
	settings = frappe.get_single("S3 Settings")
	if not settings.bucket_name:
		frappe.throw("AWS S3 Bucket Name is not configured in S3 Settings")

	file_doc = None
	if isinstance(file_or_fname, Document):
		file_doc = file_or_fname
		fname = file_doc.file_name
		content = file_doc.get_content()
		content_type = getattr(file_doc, "content_type", None)
		is_private = file_doc.is_private
	else:
		fname = file_or_fname

	# boto3 needs bytes; normalise so the reported file_size and MD5 are accurate.
	if isinstance(content, str):
		content = content.encode("utf-8")

	if not content_type:
		content_type = mimetypes.guess_type(fname)[0] or "application/octet-stream"

	# A uuid segment guarantees a unique key without inspecting the call stack, so a
	# new upload can never overwrite an existing object.
	prefix = "private" if cint(is_private) else "public"
	key = f"{prefix}/{uuid.uuid4().hex}/{fname}"

	s3 = get_s3_client()
	# No ACL is set: the bucket stays fully private and access is granted through
	# presigned URLs (see download_file). This avoids AccessControlListNotSupported
	# errors on buckets that have ACLs disabled (the modern S3 default).
	s3.put_object(
		Bucket=settings.bucket_name,
		Key=key,
		Body=content,
		ContentType=content_type,
		**_upload_extra_args(settings, content),
	)

	file_url = _build_file_url(key)
	if file_doc is not None:
		file_doc.file_url = file_url

	return {
		"file_name": fname,
		"file_url": file_url,
		"file_size": len(content),
	}


@frappe.whitelist(allow_guest=True)
def download_file(key):
	"""Redirect to a short-lived presigned URL for an S3-stored file.

	Public files are served to anyone; private files require the caller to have
	read permission on the corresponding File document.
	"""
	if not key:
		raise frappe.PermissionError

	settings = frappe.get_single("S3 Settings")

	if key.startswith("private/"):
		file_name = frappe.db.get_value(
			"File", {"file_url": ["like", f"%key={quote(key, safe='')}%"]}, "name"
		)
		if not file_name:
			raise frappe.PermissionError
		frappe.get_doc("File", file_name).check_permission("read")

	s3 = get_s3_client()
	presigned_url = s3.generate_presigned_url(
		"get_object",
		Params={
			"Bucket": settings.bucket_name,
			"Key": key,
			"ResponseContentDisposition": f'inline; filename="{key.rsplit("/", 1)[-1]}"',
		},
		ExpiresIn=_presigned_expiry(settings),
	)

	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = presigned_url


@frappe.whitelist()
def test_connection():
	"""Verify the configured credentials can reach the bucket.

	Wired to the 'Test Connection' button on the S3 Settings form.
	"""
	frappe.only_for("System Manager")
	settings = frappe.get_single("S3 Settings")
	try:
		get_s3_client().head_bucket(Bucket=settings.bucket_name)
	except Exception as e:
		frappe.throw(f"Could not connect to bucket '{settings.bucket_name}': {e}")
	return f"Successfully connected to bucket '{settings.bucket_name}'."


def delete_file_from_s3(doc, only_thumbnail=False):
	"""Frappe ``delete_file_data_content`` hook.

	The S3 objects are removed only *after* the surrounding database transaction
	commits. If the delete is rolled back, the callback is discarded and the files
	are preserved — the File record and its object never drift apart.
	"""
	settings = frappe.get_single("S3 Settings")
	if not settings.bucket_name:
		return

	keys = []
	if not only_thumbnail and getattr(doc, "file_url", None):
		keys.append(_extract_key(doc.file_url))
	if getattr(doc, "thumbnail_url", None):
		keys.append(_extract_key(doc.thumbnail_url))
	keys = [k for k in keys if k]
	if not keys:
		return

	bucket = settings.bucket_name

	def _delete_committed_objects():
		s3 = get_s3_client()
		for key in keys:
			try:
				s3.delete_object(Bucket=bucket, Key=key)
			except Exception as e:
				frappe.logger().error(f"S3 Delete Error for {key}: {e}")

	frappe.db.after_commit.add(_delete_committed_objects)


def sync_backups_to_s3():
	import os

	settings = frappe.get_single("S3 Settings")
	if not settings.bucket_name or not cint(settings.get("enable_backup_sync")):
		return

	try:
		s3 = get_s3_client()
		bucket = settings.bucket_name
		site = frappe.local.site
		backup_dir = frappe.get_site_path("private", "backups")
		extra_args = _upload_extra_args(settings)

		if not os.path.exists(backup_dir):
			return

		for fname in os.listdir(backup_dir):
			file_path = os.path.join(backup_dir, fname)
			if not os.path.isfile(file_path):
				continue

			key = f"backups/{site}/{fname}"
			# Idempotent: skip a backup already present in S3 with a matching size.
			# This makes the sync self-healing — a missed run simply uploads whatever
			# is still missing, rather than losing backups outside a 24h window.
			if _object_exists(s3, bucket, key, expected_size=os.path.getsize(file_path)):
				continue

			# upload_file streams from disk, handles multipart for large backups,
			# and closes the file handle for us (no manual open()).
			s3.upload_file(file_path, bucket, key, ExtraArgs=extra_args or None)
			frappe.logger().info(f"Successfully uploaded backup {fname} to S3")
	except Exception as e:
		frappe.logger().error(f"S3 Backup Sync Error: {e}")
