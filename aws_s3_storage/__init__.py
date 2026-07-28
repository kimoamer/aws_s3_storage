__version__ = "0.0.1"

# Frappe imports the app package before loading hooks in every web, worker and
# scheduler process. Install ERPNext compatibility patches at that point.
from aws_s3_storage.aws_s3_storage.erpnext_compat import apply_patches

apply_patches()
