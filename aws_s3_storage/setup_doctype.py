import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

def create_doctype():
    import os
    os.chdir('/home/frappe/frappe-bench')
    site_names = ["site.local", "local.site"]
    for site in site_names:
        try:
            frappe.init(site=site)
            frappe.connect()
            print(f"Connected to {site}")
            
            # Install app if not already installed
            if "aws_s3_storage" not in frappe.get_installed_apps():
                print(f"Installing aws_s3_storage on {site}")
                from frappe.installer import install_app
                install_app("aws_s3_storage")
                print(f"Successfully installed app on {site}")
                
            if frappe.db.exists("DocType", "S3 Settings"):
                print("DocType S3 Settings already exists")
            else:
                doc = frappe.get_doc({
                    "doctype": "DocType",
                    "name": "S3 Settings",
                    "module": "Aws S3 Storage",
                    "custom": 0,
                    "issingle": 1,
                    "fields": [
                        {"fieldname": "bucket_name", "fieldtype": "Data", "label": "Bucket Name", "reqd": 1},
                        {"fieldname": "region", "fieldtype": "Data", "label": "Region", "reqd": 1},
                        {"fieldname": "access_key_id", "fieldtype": "Data", "label": "Access Key ID", "reqd": 1},
                        {"fieldname": "secret_access_key", "fieldtype": "Password", "label": "Secret Access Key", "reqd": 1},
                        {"fieldname": "endpoint_url", "fieldtype": "Data", "label": "Endpoint URL"},
                        {"fieldname": "storage_class", "fieldtype": "Select", "label": "Storage Class",
                         "options": "STANDARD\nSTANDARD_IA\nINTELLIGENT_TIERING\nONEZONE_IA\nGLACIER_IR\nDEEP_ARCHIVE", "default": "STANDARD"},
                        {"fieldname": "presigned_url_expiry", "fieldtype": "Int", "label": "Presigned URL Expiry (seconds)", "default": "3600"},
                        {"fieldname": "verify_upload_integrity", "fieldtype": "Check", "label": "Verify Upload Integrity", "default": "1"},
                        {"fieldname": "enable_backup_sync", "fieldtype": "Check", "label": "Enable Daily Backup Sync", "default": "0"},
                    ],
                    "permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1}]
                })
                doc.insert(ignore_permissions=True)
                frappe.db.commit()
                print("Successfully created S3 Settings DocType")
            
        except Exception as e:
            print(f"Error on {site}: {e}")
        finally:
            frappe.destroy()

if __name__ == "__main__":
    create_doctype()
