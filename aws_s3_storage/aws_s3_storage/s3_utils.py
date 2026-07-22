import mimetypes
import uuid
from urllib.parse import parse_qs, quote, urlparse

import boto3
import frappe
from frappe.utils import get_url

# Whitelisted method used to serve files back from S3 via short-lived presigned URLs.
DOWNLOAD_METHOD = "aws_s3_storage.aws_s3_storage.s3_utils.download_file"
# Lifetime of a generated presigned URL, in seconds.
PRESIGNED_URL_EXPIRY = 3600


def get_s3_client():
    settings = frappe.get_single('S3 Settings')
    secret = settings.get_password('secret_access_key', raise_exception=False)
    if not settings.bucket_name or not settings.access_key_id or not secret:
        frappe.throw('S3 Settings are not fully configured')

    return boto3.client(
        's3',
        aws_access_key_id=settings.access_key_id,
        aws_secret_access_key=secret,
        region_name=settings.region
    )


def _build_file_url(key):
    # A stable, self-referential URL. It is absolute so Frappe treats the file as
    # remote and never attempts to read/hash it from the local filesystem.
    return f"{get_url()}/api/method/{DOWNLOAD_METHOD}?key={quote(key, safe='')}"


def _extract_key(file_url):
    # Recover the S3 object key that was embedded in the file URL by _build_file_url.
    if not file_url:
        return None
    keys = parse_qs(urlparse(file_url).query).get('key')
    return keys[0] if keys else None


def write_file_to_s3(fname, content, content_type=None, is_private=0):
    settings = frappe.get_single('S3 Settings')
    if not settings.bucket_name:
        frappe.throw('AWS S3 Bucket Name is not configured in S3 Settings')

    # boto3 needs bytes; normalise so the reported file_size is accurate.
    if isinstance(content, str):
        content = content.encode('utf-8')

    if not content_type:
        content_type = mimetypes.guess_type(fname)[0] or 'application/octet-stream'

    # A uuid segment guarantees a unique key without inspecting the call stack.
    prefix = 'private' if is_private else 'public'
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
    )

    return {
        "file_name": fname,
        "file_url": _build_file_url(key),
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

    settings = frappe.get_single('S3 Settings')

    if key.startswith('private/'):
        file_name = frappe.db.get_value(
            'File', {"file_url": ["like", f"%key={quote(key, safe='')}%"]}, "name"
        )
        if not file_name:
            raise frappe.PermissionError
        frappe.get_doc('File', file_name).check_permission('read')

    s3 = get_s3_client()
    presigned_url = s3.generate_presigned_url(
        'get_object',
        Params={
            "Bucket": settings.bucket_name,
            "Key": key,
            "ResponseContentDisposition": f'inline; filename="{key.rsplit("/", 1)[-1]}"',
        },
        ExpiresIn=PRESIGNED_URL_EXPIRY,
    )

    frappe.local.response["type"] = "redirect"
    frappe.local.response["location"] = presigned_url


def delete_file_from_s3(doc, only_thumbnail=False):
    settings = frappe.get_single('S3 Settings')
    if not settings.bucket_name:
        return

    s3 = get_s3_client()
    bucket = settings.bucket_name

    urls = []
    if not only_thumbnail and getattr(doc, 'file_url', None):
        urls.append(doc.file_url)
    if getattr(doc, 'thumbnail_url', None):
        urls.append(doc.thumbnail_url)

    for url in urls:
        key = _extract_key(url)
        if not key:
            continue
        try:
            s3.delete_object(Bucket=bucket, Key=key)
        except Exception as e:
            frappe.logger().error(f"S3 Delete Error for {key}: {e}")


def sync_backups_to_s3():
    import os
    import time

    settings = frappe.get_single('S3 Settings')
    if not settings.bucket_name:
        return

    try:
        s3 = get_s3_client()
        bucket = settings.bucket_name
        site = frappe.local.site
        backup_dir = frappe.get_site_path('private', 'backups')

        if not os.path.exists(backup_dir):
            return

        cutoff = time.time() - 86400  # only sync files created within the last 24 hours
        for fname in os.listdir(backup_dir):
            file_path = os.path.join(backup_dir, fname)
            if not os.path.isfile(file_path) or os.path.getmtime(file_path) <= cutoff:
                continue
            key = f"backups/{site}/{fname}"
            # upload_file streams from disk, handles multipart for large backups,
            # and closes the file handle for us (no manual open()).
            s3.upload_file(file_path, bucket, key)
            frappe.logger().info(f"Successfully uploaded backup {fname} to S3")
    except Exception as e:
        frappe.logger().error(f"S3 Backup Sync Error: {e}")
