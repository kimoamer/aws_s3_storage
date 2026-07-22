import frappe
import boto3
from botocore.exceptions import ClientError
import mimetypes

def get_s3_client():
    settings = frappe.get_single('S3 Settings')
    if not settings.bucket_name or not settings.access_key_id or not settings.secret_access_key:
        frappe.throw('S3 Settings are not fully configured')
    
    return boto3.client(
        's3',
        aws_access_key_id=settings.access_key_id,
        aws_secret_access_key=settings.get_password('secret_access_key'),
        region_name=settings.region
    )

def write_file_to_s3(fname, content, content_type=None, is_private=0):
    settings = frappe.get_single('S3 Settings')
    if not settings.bucket_name:
        frappe.throw('AWS S3 Bucket Name is not configured in S3 Settings')
    
    dt = 'Unattached'
    import inspect
    for frame_record in inspect.stack():
        if frame_record.function == 'save_file':
            dt = frame_record.frame.f_locals.get('dt') or 'Unattached'
            break
            
    s3 = get_s3_client()
    bucket = settings.bucket_name
    
    # Construct folder path
    folder = frappe.scrub(dt)
    key = f"{folder}/{fname}"
    if is_private:
        key = f"private/{key}"
    else:
        key = f"public/{key}"
    
    # Upload
    s3.put_object(
        Bucket=bucket, 
        Key=key, 
        Body=content, 
        ContentType=content_type,
        ACL='private' if is_private else 'public-read'
    )
    url = f"https://{bucket}.s3.{settings.region}.amazonaws.com/{key}"
    
    return {"file_name": fname, "file_url": url, "file_size": len(content)}

def delete_file_from_s3(doc, only_thumbnail=False):
    settings = frappe.get_single('S3 Settings')
    if not settings.bucket_name:
        return
        
    s3 = get_s3_client()
    bucket = settings.bucket_name
    
    def extract_key(url):
        # Extract the S3 key from the full URL
        if url and f"s3.{settings.region}.amazonaws.com" in url:
            return url.split(f"s3.{settings.region}.amazonaws.com/")[-1]
        return None

    if not only_thumbnail and doc.file_url:
        key = extract_key(doc.file_url)
        if key:
            try:
                s3.delete_object(Bucket=bucket, Key=key)
            except Exception as e:
                frappe.logger().error(f"S3 Delete Error: {e}")

    if doc.thumbnail_url:
        thumb_key = extract_key(doc.thumbnail_url)
        if thumb_key:
            try:
                s3.delete_object(Bucket=bucket, Key=thumb_key)
            except Exception as e:
                frappe.logger().error(f"S3 Delete Error (Thumbnail): {e}")

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
        
        if os.path.exists(backup_dir):
            for f in os.listdir(backup_dir):
                file_path = os.path.join(backup_dir, f)
                if os.path.isfile(file_path):
                    # Only upload files created within the last 24 hours (86400 seconds)
                    if os.path.getmtime(file_path) > time.time() - 86400:
                        key = f"backups/{site}/{f}"
                        s3.put_object(
                            Bucket=bucket,
                            Key=key,
                            Body=open(file_path, 'rb')
                        )
                        frappe.logger().info(f"Successfully uploaded backup {f} to S3")
    except Exception as e:
        frappe.logger().error(f"S3 Backup Sync Error: {e}")
