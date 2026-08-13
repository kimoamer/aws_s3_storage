import base64
import hashlib
import mimetypes
import os
import uuid
from fnmatch import fnmatch
from urllib.parse import quote, unquote, urlparse

import boto3
import frappe
from botocore.config import Config
from botocore.exceptions import ClientError
from frappe.model.document import Document
from frappe.utils import cint

# Whitelisted method used to serve files back from S3 via short-lived presigned URLs.
DOWNLOAD_METHOD = "aws_s3_storage.aws_s3_storage.s3_utils.download_file"
# Fallback lifetime of a generated presigned URL, in seconds, when not configured.
DEFAULT_PRESIGNED_URL_EXPIRY = 3600
# S3's hard limits for SigV4 presigned URL expiry.
MIN_PRESIGNED_URL_EXPIRY = 60
MAX_PRESIGNED_URL_EXPIRY = 604800  # 7 days
# Only these prefixes are ever served to the web; everything else (e.g. backups/)
# must never be reachable through the public download endpoint.
SERVABLE_PREFIXES = ("public/", "private/")
# S3 error codes that mean "object does not exist".
_NOT_FOUND_CODES = {"404", "NoSuchKey", "NotFound"}


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


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
	if not settings.bucket_name:
		frappe.throw("AWS S3 Bucket Name is not configured in S3 Settings")

	endpoint_url = (settings.get("endpoint_url") or "").strip() or None
	kwargs = {
		"region_name": settings.region,
		"endpoint_url": endpoint_url,
		"config": _boto_config(endpoint_url),
	}

	# Static keys are optional: when both are provided we use them, otherwise boto3
	# falls back to its default credential chain (EC2 instance profile / IAM role,
	# environment variables, ...). This lets a server on AWS avoid storing secrets.
	access_key = settings.access_key_id
	secret = settings.get_password("secret_access_key", raise_exception=False)
	if access_key and secret:
		kwargs["aws_access_key_id"] = access_key
		kwargs["aws_secret_access_key"] = secret

	return boto3.client("s3", **kwargs)


def get_bucket():
	return frappe.get_single("S3 Settings").bucket_name


def _is_enabled(settings):
	# Default to enabled: a value that was never set (None) counts as on, only an
	# explicit unchecked (0) disables S3 storage.
	value = settings.get("enabled")
	return True if value is None else bool(cint(value))


def _save_to_filesystem(file_or_fname, content, content_type, is_private):
	"""Fallback to Frappe's default local storage (integration disabled/unconfigured)."""
	if isinstance(file_or_fname, Document):
		return file_or_fname.save_file_on_filesystem()

	from frappe.utils.file_manager import save_file_on_filesystem

	return save_file_on_filesystem(file_or_fname, content, content_type=content_type, is_private=is_private)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _presigned_expiry(settings):
	expiry = cint(settings.get("presigned_url_expiry")) or DEFAULT_PRESIGNED_URL_EXPIRY
	return max(MIN_PRESIGNED_URL_EXPIRY, min(expiry, MAX_PRESIGNED_URL_EXPIRY))


def _upload_extra_args(settings, content=None, storage_class=None):
	"""Build ExtraArgs/put_object kwargs from the configurable S3 Settings.

	``content`` (bytes) is only passed for single-shot put_object uploads so we can
	attach a Content-MD5 header; multipart uploads (upload_file) and copy_object
	compute their own integrity, so it is omitted there. ``storage_class`` overrides
	the default attachment storage class (used for backups).
	"""
	extra = {}

	storage_class = (storage_class or settings.get("storage_class") or "").strip()
	if storage_class and storage_class != "STANDARD":
		extra["StorageClass"] = storage_class

	if content is not None and cint(settings.get("verify_upload_integrity")):
		# S3 validates the body against this digest and rejects a corrupted upload
		# with BadDigest, so a damaged object is never silently stored.
		digest = hashlib.md5(content, usedforsecurity=False).digest()
		extra["ContentMD5"] = base64.b64encode(digest).decode()

	return extra


def _build_file_url(key):
	# A relative URL, so files keep working across domain changes, restores, clones
	# and HTTP<->HTTPS. Frappe treats a "/api/method/..." URL as a remote file, so it
	# never tries to read it from the local filesystem.
	#
	# Keep the path separators unencoded (safe="/"): encoding them to %2F invites a
	# second encoding pass (%252F) when the URL is rendered, which then fails the
	# public/ prefix check in download_file.
	return f"/api/method/{DOWNLOAD_METHOD}?key={quote(key, safe='/')}"


def _normalize_key(key):
	# Strip up to two layers of URL-encoding so both cleanly-built keys and older
	# double-encoded ones (%2F / %252F) resolve to the same canonical S3 key.
	if not key:
		return None
	for _ in range(2):
		decoded = unquote(key)
		if decoded == key:
			break
		key = decoded
	return key


def _extract_key(file_url):
	"""Extract the S3 key without converting literal '+' characters to spaces.

	urllib.parse.parse_qs() uses form-encoding rules where '+' means a space.
	S3 object keys may legitimately contain '+', especially in filenames such
	as '(2+1).pdf', so parse the raw query manually and decode with unquote().
	"""
	if not file_url:
		return None

	query = urlparse(file_url).query

	for parameter in query.split("&"):
		name, separator, value = parameter.partition("=")

		if not separator:
			continue

		if unquote(name) == "key":
			# _normalize_key uses unquote(), not unquote_plus(), so '+' is preserved.
			return _normalize_key(value)

	return None


def _content_disposition(filename):
	# HTTP header values must be latin-1, so a non-ASCII filename (e.g. Arabic) is
	# sent RFC 5987 style (filename*), with a plain ASCII fallback for old clients.
	# Without this S3 rejects the presigned URL with "Header value cannot be
	# represented using ISO-8859-1".
	ascii_name = filename.encode("ascii", "ignore").decode().replace('"', "").replace("\\", "").strip()
	ascii_name = ascii_name or "file"
	return f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


# Keep keys within the s3_key column width (varchar 500); real filenames are far
# shorter (the filesystem caps names at 255), so this only guards pathological cases.
MAX_KEY_LENGTH = 480


def _bounded_filename(fname, max_len):
	if max_len <= 0:
		return ""
	if len(fname) <= max_len:
		return fname
	root, ext = os.path.splitext(fname)
	ext = ext[:max_len]
	keep = max_len - len(ext)
	return (root[:keep] + ext) if keep > 0 else fname[:max_len]


def _new_key(fname, is_private):
	prefix = "private" if cint(is_private) else "public"
	head = f"{prefix}/{uuid.uuid4().hex}/"
	return head + _bounded_filename(fname, MAX_KEY_LENGTH - len(head))


def _swap_prefix(key, is_private):
	"""Return the same key under the public/ or private/ prefix."""
	new_prefix = "private" if cint(is_private) else "public"
	head, sep, rest = key.partition("/")
	if sep and head in ("public", "private"):
		return f"{new_prefix}/{rest}"
	return key


def object_exists(key, expected_size=None, s3=None, bucket=None):
	"""Return True only if the object exists and (optionally) matches expected_size."""
	s3 = s3 or get_s3_client()
	bucket = bucket or get_bucket()
	try:
		head = s3.head_object(Bucket=bucket, Key=key)
	except ClientError as e:
		if e.response.get("Error", {}).get("Code") in _NOT_FOUND_CODES:
			return False
		raise
	if expected_size is not None:
		return head.get("ContentLength") == expected_size
	return True


# ---------------------------------------------------------------------------
# Attachments that must stay on the local disk
# ---------------------------------------------------------------------------
# Most apps treat an attachment as an opaque blob and read it back through the
# File API, which works fine from S3. A few instead reopen the file *by path* and
# rewrite it in place. ERPNext's reposting data file is the known case:
# erpnext/stock/stock_ledger.py:create_json_gz_file() does
#
#     path = file_doc.get_full_path()
#     with open(path, "wb") as f: ...
#
# after every reposting batch, so the attachment has to be a real path on disk —
# an S3-backed File makes every repost fail with FileNotFoundError. Files matching
# the rules below therefore bypass S3 and use Frappe's local storage. They are
# small, short-lived and deleted by ERPNext once the repost finishes.
#
# To keep another app's attachment local, add its doctype/fieldname here.
LOCAL_ONLY_ATTACHED_TO_DOCTYPES = {"Repost Item Valuation"}
LOCAL_ONLY_ATTACHED_TO_FIELDS = {"reposting_data_file"}
# Fallback for callers that pass only a filename (the legacy file_manager
# convention), where the attachment link is not available.
LOCAL_ONLY_FILENAME_PATTERNS = ("repost_item_valuation-*.json.gz",)


def is_local_only_file(file_doc=None, fname=None):
	"""True when this attachment must be stored on local disk instead of S3.

	``file_doc`` may be a File Document or any dict-like row carrying the
	``attached_to_*`` fields; ``fname`` alone is enough for the filename rules.
	"""
	if file_doc is not None:
		if (file_doc.get("attached_to_doctype") or "") in LOCAL_ONLY_ATTACHED_TO_DOCTYPES:
			return True
		if (file_doc.get("attached_to_field") or "") in LOCAL_ONLY_ATTACHED_TO_FIELDS:
			return True
		fname = fname or file_doc.get("file_name")

	fname = (fname or "").lower()
	return any(fnmatch(fname, pattern) for pattern in LOCAL_ONLY_FILENAME_PATTERNS)


# ---------------------------------------------------------------------------
# Write / read
# ---------------------------------------------------------------------------


def write_file_to_s3(file_or_fname, content=None, content_type=None, is_private=0):
	"""Frappe ``write_file`` hook.

	Frappe calls this hook with two different conventions depending on the code
	path, so both are supported:

	* File doctype (frappe/core/doctype/file/file.py) -> ``write_file_to_s3(file_doc)``
	* Legacy file_manager (frappe/utils/file_manager.py) ->
	  ``write_file_to_s3(fname, content, content_type=..., is_private=...)``
	"""
	settings = frappe.get_single("S3 Settings")

	file_doc = file_or_fname if isinstance(file_or_fname, Document) else None
	fname = file_doc.file_name if file_doc is not None else file_or_fname

	# Master switch: when disabled — or before a bucket is configured — fall back to
	# Frappe's default local storage instead of failing the upload. Attachments that
	# an app rewrites in place by path (see is_local_only_file) take the same route.
	if not _is_enabled(settings) or not settings.bucket_name or is_local_only_file(file_doc, fname):
		return _save_to_filesystem(file_or_fname, content, content_type, is_private)

	if file_doc is not None:
		content = file_doc.get_content()
		content_type = getattr(file_doc, "content_type", None)
		is_private = file_doc.is_private

	# boto3 needs bytes; normalise so the reported file_size and MD5 are accurate.
	if isinstance(content, str):
		content = content.encode("utf-8")

	if not content_type:
		content_type = mimetypes.guess_type(fname)[0] or "application/octet-stream"

	key = _new_key(fname, is_private)

	s3 = get_s3_client()
	bucket = settings.bucket_name
	# No ACL is set: the bucket stays fully private and access is granted through
	# presigned URLs (see download_file).
	s3.put_object(
		Bucket=bucket,
		Key=key,
		Body=content,
		ContentType=content_type,
		**_upload_extra_args(settings, content),
	)
	# If the surrounding transaction rolls back, the File record is never created,
	# so remove the just-uploaded object to avoid orphaning it in S3. after_rollback
	# is cleared on commit, so this is a no-op on success.
	_delete_on_rollback(bucket, [key])

	file_url = _build_file_url(key)
	if file_doc is not None:
		file_doc.file_url = file_url
		file_doc.s3_key = key

	return {
		"file_name": fname,
		"file_url": file_url,
		"file_size": len(content),
		"s3_key": key,
	}


def read_file_from_s3(key):
	"""Return the raw bytes of an S3 object, read server-side via boto3.

	Used by the File override to read content/thumbnails back without an HTTP
	round-trip (which would fail permission checks for private files).
	"""
	s3 = get_s3_client()
	obj = s3.get_object(Bucket=get_bucket(), Key=key)
	return obj["Body"].read()


def upload_thumbnail(key, content, content_type):
	"""Upload a generated thumbnail and return the URL used to serve it."""
	settings = frappe.get_single("S3 Settings")
	s3 = get_s3_client()
	s3.put_object(
		Bucket=settings.bucket_name,
		Key=key,
		Body=content,
		ContentType=content_type,
		**_upload_extra_args(settings, content),
	)
	return _build_file_url(key)


def copy_object(src_key, dst_key, settings=None, s3=None, bucket=None):
	"""Server-side copy within the bucket, preserving the storage class."""
	settings = settings or frappe.get_single("S3 Settings")
	s3 = s3 or get_s3_client()
	bucket = bucket or settings.bucket_name
	s3.copy_object(
		Bucket=bucket,
		Key=dst_key,
		CopySource={"Bucket": bucket, "Key": src_key},
		MetadataDirective="COPY",
		**_upload_extra_args(settings),
	)


def move_object_privacy(file_doc):
	"""Move a File's S3 object(s) between the public/ and private/ prefixes when
	``is_private`` changes, keeping the stored keys/URLs in sync.

	The old objects are deleted only after commit; the new copies are removed if the
	transaction rolls back.
	"""
	# Prefer the stored keys: by the time Frappe calls this (inside File.validate)
	# it has already run unquote() over file_url, so a key holding '&' or '#' would
	# parse back short and the move would target an object that does not exist.
	old_key = file_doc.get("s3_key") or _extract_key(file_doc.file_url)
	if not old_key:
		return

	new_key = _swap_prefix(old_key, file_doc.is_private)
	if new_key == old_key:
		return

	settings = frappe.get_single("S3 Settings")
	bucket = settings.bucket_name
	s3 = get_s3_client()

	new_keys, old_keys = [new_key], [old_key]
	copy_object(old_key, new_key, settings=settings, s3=s3, bucket=bucket)
	file_doc.file_url = _build_file_url(new_key)
	file_doc.s3_key = new_key

	old_thumb = file_doc.get("s3_thumbnail_key") or _extract_key(file_doc.get("thumbnail_url"))
	if old_thumb:
		new_thumb = _swap_prefix(old_thumb, file_doc.is_private)
		copy_object(old_thumb, new_thumb, settings=settings, s3=s3, bucket=bucket)
		file_doc.thumbnail_url = _build_file_url(new_thumb)
		file_doc.s3_thumbnail_key = new_thumb
		new_keys.append(new_thumb)
		old_keys.append(old_thumb)

	_delete_on_rollback(bucket, new_keys)
	# Only drop the old object(s) if no other File still references them (a shared /
	# deduplicated object must survive).
	_delete_after_commit(bucket, old_keys, check_references=True)


# ---------------------------------------------------------------------------
# Serving
# ---------------------------------------------------------------------------


def _files_for_key(key):
	"""Names of every File record pointing at this object.

	The indexed key columns are tried first. Records written before those columns
	existed (or written directly with db_set / SQL) carry the key only inside their
	URL, so fall back to the same URL matching used by the deletion guard —
	otherwise a perfectly valid old attachment resolves to no record at all and is
	refused even for a user who may read it.
	"""
	names = frappe.db.sql_list(
		"""
		SELECT `name`
		FROM `tabFile`
		WHERE `s3_key` = %(key)s
		   OR `s3_thumbnail_key` = %(key)s
		""",
		{"key": key},
	)
	if names:
		return names

	rows = frappe.db.sql(
		"""
		SELECT `name`, `file_url`, `thumbnail_url`
		FROM `tabFile`
		WHERE `file_url` LIKE %(pattern)s OR `thumbnail_url` LIKE %(pattern)s
		""",
		{"pattern": f"%{_url_match_token(key)}%"},
		as_dict=True,
	)
	return [
		row.name
		for row in rows
		if _url_references_key(row.file_url, key) or _url_references_key(row.thumbnail_url, key)
	]


def _guest_may_read(file_doc, settings):
	"""Whether the anonymous visitor may read this private object.

	Frappe never grants a Guest read access to a private File (see
	frappe/core/doctype/file/file.py:has_permission and download_private_file), so a
	web form attachment uploaded by a visitor cannot be shown back to them — not in
	the preview right after upload, and not on the submitted document.

	This opt-in rule closes that gap without widening access for anyone else:
	only objects whose File record is *owned by Guest* qualify, i.e. only what a
	visitor uploaded themselves. The object stays private in the bucket and is still
	served through a short-lived presigned URL; what protects it from other visitors
	is the uuid4 in its key, exactly as it protects a public/ object today.
	"""
	if frappe.session.user != "Guest":
		return False

	if not cint(settings.get("allow_guest_downloads")):
		return False

	if (file_doc.get("owner") or "") != "Guest":
		return False

	allowed = {
		doctype.strip()
		for doctype in (settings.get("guest_upload_doctypes") or "").splitlines()
		if doctype.strip()
	}
	attached_to = file_doc.get("attached_to_doctype") or ""

	# An empty allowlist means "any guest upload". A file that is not attached to
	# anything yet is always allowed through: that is the state of every upload
	# between the file being sent and the web form being submitted, which is exactly
	# the preview this rule exists for.
	if allowed and attached_to and attached_to not in allowed:
		return False

	return True


@frappe.whitelist(allow_guest=True)
def download_file(key=None):
	"""Serve an S3 file while preserving literal '+' characters in its key."""

	raw_query = ""

	if frappe.request:
		raw_query = frappe.request.query_string or b""

		if isinstance(raw_query, bytes):
			raw_query = raw_query.decode("utf-8", errors="replace")

	raw_key = _extract_key(f"/?{raw_query}") if raw_query else None

	key = raw_key or _normalize_key(key)

	if not key or not key.startswith(SERVABLE_PREFIXES):
		raise frappe.PermissionError

	settings = frappe.get_single("S3 Settings")

	if key.startswith("private/"):
		file_names = _files_for_key(key)

		if not file_names:
			raise frappe.PermissionError

		has_access = False

		for file_name in file_names:
			file_doc = frappe.get_doc("File", file_name)

			if file_doc.has_permission("read") or _guest_may_read(file_doc, settings):
				has_access = True
				break

		if not has_access:
			raise frappe.PermissionError

	s3 = get_s3_client()

	presigned_url = s3.generate_presigned_url(
		"get_object",
		Params={
			"Bucket": settings.bucket_name,
			"Key": key,
			"ResponseContentDisposition": _content_disposition(key.rsplit("/", 1)[-1]),
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


# ---------------------------------------------------------------------------
# Deletion (deferred, reference-checked, retried)
# ---------------------------------------------------------------------------


def delete_file_from_s3(doc, only_thumbnail=False):
	"""Frappe ``delete_file_data_content`` hook.

	Objects are removed only *after* the surrounding transaction commits, and only
	when no other File still references the same key (deduplicated uploads share an
	object). A rolled-back delete discards the removal entirely.
	"""
	keys = []
	if not only_thumbnail:
		keys.append(getattr(doc, "s3_key", None) or _extract_key(getattr(doc, "file_url", None)))
	keys.append(getattr(doc, "s3_thumbnail_key", None) or _extract_key(getattr(doc, "thumbnail_url", None)))
	keys = [k for k in keys if k]

	if not keys:
		# Local file (e.g. uploaded before this app was installed) — let Frappe
		# remove it from disk so it is not leaked.
		if hasattr(doc, "delete_file_from_filesystem"):
			doc.delete_file_from_filesystem(only_thumbnail=only_thumbnail)
		return

	# check_documents: a real deletion is the one case where an Attach field still
	# holding the URL has to win, because nothing would bring the object back.
	_delete_after_commit(get_bucket(), keys, check_references=True, check_documents=True)


def _delete_after_commit(bucket, keys, check_references=False, check_documents=False):
	def _run():
		_delete_keys(bucket, keys, check_references=check_references, check_documents=check_documents)

	frappe.db.after_commit.add(_run)


def _delete_on_rollback(bucket, keys):
	def _run():
		_delete_keys(bucket, keys)

	frappe.db.after_rollback.add(_run)


def _delete_keys(bucket, keys, check_references=False, check_documents=False):
	s3 = get_s3_client()
	for key in keys:
		# Never delete an object another File still points at (dedup / shared use).
		if check_references and _key_is_referenced(key, check_documents=check_documents):
			continue
		try:
			s3.delete_object(Bucket=bucket, Key=key)
		except Exception as e:
			frappe.logger().error(f"S3 Delete Error for {key}: {e}")
			_queue_deletion(bucket, key, str(e))


def _key_is_referenced(key, check_documents=False):
	"""True when anything still points at this object.

	Deduplicated uploads share one object, and so does every Amend of a document:
	the copied attachment is a new File record on the original's key. Removing the
	object for one of them breaks all the others.

	The key columns only cover records written through this app's File lifecycle;
	one written directly (db_set, SQL) carries the key in its URL alone, so the URLs
	are checked as well before anything is deleted.

	``check_documents`` additionally scans the Attach fields of every doctype (see
	_key_is_linked_from_a_document). It is on for a real deletion and deliberately
	off when an object is being superseded by a copy of itself under the other
	privacy prefix: there the old object *must* go, or a file just marked private
	stays readable under its public key.
	"""
	if frappe.db.exists("File", {"s3_key": key}) or frappe.db.exists("File", {"s3_thumbnail_key": key}):
		return True

	rows = frappe.db.sql(
		"""
		SELECT `file_url`, `thumbnail_url`
		FROM `tabFile`
		WHERE `file_url` LIKE %(pattern)s OR `thumbnail_url` LIKE %(pattern)s
		""",
		{"pattern": f"%{_url_match_token(key)}%"},
	)
	# The LIKE only narrows the scan (a file name may itself hold SQL wildcards);
	# each candidate is decoded and compared properly below.
	if any(
		_url_references_key(file_url, key) or _url_references_key(thumbnail_url, key)
		for file_url, thumbnail_url in rows
	):
		return True

	return check_documents and _key_is_linked_from_a_document(key)


def _key_is_linked_from_a_document(key):
	"""True when a document's Attach field still holds this object's URL.

	A File record is not the only thing pointing at an object: an Attach field
	stores the URL itself, and that value travels between documents (``fetch_from``,
	an Amend, a script carrying a Job Applicant's CV onto the Interview). The File
	it came from can then be deleted — together with its own document — while other
	documents still show the attachment. Without this check the object goes with it
	and every one of those links dies, with nothing in the bucket to restore.

	Only reached for a genuine deletion whose key no File record covers any more, so
	the scan is rare; it stops at the first live reference, and the field list is
	cached. A field that cannot be scanned counts as a reference: keeping an object
	nothing uses any more is always cheaper than deleting a live one.
	"""
	pattern = f"%{_url_match_token(key)}%"
	unscannable = False

	# Single doctypes keep every field in one narrow table, so all of them — the site
	# logo and favicon in Website Settings, a letter head's image, a print logo — are
	# covered by this one cheap query. They are also the values most likely to outlive
	# the File record they came from.
	try:
		rows = frappe.db.sql(
			"SELECT `value` FROM `tabSingles` WHERE `value` LIKE %(pattern)s",
			{"pattern": pattern},
		)
	except Exception:
		unscannable = True
	else:
		if any(_url_references_key(value, key) for (value,) in rows):
			return True

	try:
		fields = _attach_fields()
	except Exception:
		# This runs inside an after_commit callback, where an escaping exception would
		# surface on a request whose work is already committed. Fail closed instead:
		# the object stays, and the deletion queue is not involved.
		return True

	for doctype, fieldname in fields:
		try:
			rows = frappe.db.sql(
				f"""
				SELECT `{fieldname}`
				FROM `tab{doctype}`
				WHERE `{fieldname}` LIKE %(pattern)s
				LIMIT 20
				""",
				{"pattern": pattern},
			)
		except Exception:
			# A field can outlive its column (renamed doctype, half-applied migration).
			unscannable = True
			continue

		if any(_url_references_key(value, key) for (value,) in rows):
			return True

	return unscannable


# Cached because _key_is_linked_from_a_document runs on the delete path, where the
# doctype metadata behind this list changes far more slowly than files are removed.
_ATTACH_FIELDS_CACHE_KEY = "aws_s3_storage:attach_fields"
_ATTACH_FIELDS_CACHE_TTL = 3600


def _attach_fields():
	"""Every stored Attach / Attach Image field, as ``(doctype, fieldname)`` pairs.

	Single and virtual doctypes have no row to attach to, and a child table's rows
	are not what an attachment links to, so all three are left out.
	"""
	try:
		return frappe.cache().get_value(
			_ATTACH_FIELDS_CACHE_KEY,
			generator=_query_attach_fields,
			expires_in_sec=_ATTACH_FIELDS_CACHE_TTL,
		)
	except Exception:
		return _query_attach_fields()


def _query_attach_fields():
	rows = frappe.db.sql(
		"""
		SELECT df.parent AS doctype, df.fieldname AS fieldname
		FROM `tabDocField` df
		JOIN `tabDocType` dt ON dt.name = df.parent
		WHERE df.fieldtype IN ('Attach', 'Attach Image')
		  AND dt.issingle = 0 AND dt.istable = 0 AND dt.is_virtual = 0
		UNION
		SELECT cf.dt AS doctype, cf.fieldname AS fieldname
		FROM `tabCustom Field` cf
		JOIN `tabDocType` dt ON dt.name = cf.dt
		WHERE cf.fieldtype IN ('Attach', 'Attach Image')
		  AND dt.issingle = 0 AND dt.istable = 0 AND dt.is_virtual = 0
		""",
		as_dict=True,
	)

	fields = []
	for row in rows:
		try:
			if frappe.db.has_column(row.doctype, row.fieldname):
				fields.append((row.doctype, row.fieldname))
		except Exception:
			continue

	return fields


def _url_match_token(key):
	"""The part of a key that survives URL-encoding, used to narrow the scan.

	Keys are "<prefix>/<uuid>/<filename>" (_new_key) and quote() never rewrites the
	uuid hex, so matching on it stays selective whatever the file is called.
	"""
	parts = key.split("/")
	if len(parts) >= 3 and parts[0] in ("public", "private") and parts[1]:
		return parts[1]
	return key


def _url_references_key(file_url, key):
	url_key = _extract_key(file_url)
	if not url_key:
		return False

	# A URL that Frappe has unquoted parses back short when the name holds '&' or
	# '#' (see S3File._key), so a truncated key counts as a reference too: keeping
	# an object nothing uses any more is always cheaper than deleting a live one.
	return key == url_key or key.startswith(url_key)


def _queue_deletion(bucket, key, error=None):
	"""Record a failed deletion so a scheduler can retry it (avoids orphaned objects).

	Runs the insert in a background job: this is usually called from inside an
	after_commit/after_rollback callback, where an inline insert+commit would be a
	re-entrant transaction.
	"""
	try:
		frappe.enqueue(
			"aws_s3_storage.aws_s3_storage.s3_utils._insert_deletion_row",
			queue="short",
			bucket=bucket,
			key=key,
			error=error,
		)
	except Exception as e:
		frappe.logger().error(f"Could not queue S3 deletion for {key}: {e}")


def _insert_deletion_row(bucket, key, error=None):
	if frappe.db.exists("S3 Deletion Queue", {"s3_key": key}):
		return
	frappe.get_doc(
		{
			"doctype": "S3 Deletion Queue",
			"bucket": bucket,
			"s3_key": key,
			"last_error": error,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()


def process_deletion_queue():
	"""Scheduler job: retry queued S3 deletions."""
	rows = frappe.get_all(
		"S3 Deletion Queue",
		filters={"status": "Pending"},
		fields=["name", "bucket", "s3_key", "attempts"],
		limit=200,
	)
	if not rows:
		return

	s3 = get_s3_client()
	for row in rows:
		try:
			s3.delete_object(Bucket=row.bucket, Key=row.s3_key)
			frappe.delete_doc("S3 Deletion Queue", row.name, ignore_permissions=True, force=True)
		except Exception as e:
			attempts = (row.attempts or 0) + 1
			frappe.db.set_value(
				"S3 Deletion Queue",
				row.name,
				{
					"attempts": attempts,
					"last_error": str(e),
					"status": "Failed" if attempts >= 10 else "Pending",
				},
			)
	frappe.db.commit()


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------


def sync_backups_to_s3():
	settings = frappe.get_single("S3 Settings")
	if not settings.bucket_name or not cint(settings.get("enable_backup_sync")):
		return

	try:
		s3 = get_s3_client()
		bucket = settings.bucket_name
		site = frappe.local.site
		backup_dir = frappe.get_site_path("private", "backups")
		storage_class = settings.get("backup_storage_class") or settings.get("storage_class")
		extra_args = _upload_extra_args(settings, storage_class=storage_class)
		delete_local = cint(settings.get("delete_local_backup_after_sync"))

		if not os.path.exists(backup_dir):
			return

		for fname in os.listdir(backup_dir):
			file_path = os.path.join(backup_dir, fname)
			if not os.path.isfile(file_path):
				continue

			key = f"backups/{site}/{fname}"
			local_size = os.path.getsize(file_path)

			# Idempotent: skip a backup already present in S3 with a matching size.
			if not object_exists(key, expected_size=local_size, s3=s3, bucket=bucket):
				s3.upload_file(file_path, bucket, key, ExtraArgs=extra_args or None)
				frappe.logger().info(f"Uploaded backup {fname} to S3")

			# Only reclaim local space once the object is verified present in S3.
			if delete_local and object_exists(key, expected_size=local_size, s3=s3, bucket=bucket):
				os.remove(file_path)
				frappe.logger().info(f"Removed local backup {fname} after successful S3 sync")
	except Exception as e:
		frappe.logger().error(f"S3 Backup Sync Error: {e}")
