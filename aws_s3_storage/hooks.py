app_name = "aws_s3_storage"
app_title = "Aws S3 Storage"
app_publisher = "Innomate LLC"
app_description = "AWS S3 integration"
app_email = "a.amer@innomate-tech.com"
app_license = "mit"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "aws_s3_storage",
# 		"logo": "/assets/aws_s3_storage/logo.png",
# 		"title": "Aws S3 Storage",
# 		"route": "/aws_s3_storage",
# 		"has_permission": "aws_s3_storage.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/aws_s3_storage/css/aws_s3_storage.css"
# app_include_js = "/assets/aws_s3_storage/js/aws_s3_storage.js"

# include js, css files in header of web template
# web_include_css = "/assets/aws_s3_storage/css/aws_s3_storage.css"
# web_include_js = "/assets/aws_s3_storage/js/aws_s3_storage.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "aws_s3_storage/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "aws_s3_storage/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "aws_s3_storage.utils.jinja_methods",
# 	"filters": "aws_s3_storage.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "aws_s3_storage.install.before_install"
after_install = "aws_s3_storage.aws_s3_storage.install.after_install"
after_migrate = "aws_s3_storage.aws_s3_storage.install.after_migrate"

# Uninstallation
# ------------

# before_uninstall = "aws_s3_storage.uninstall.before_uninstall"
# after_uninstall = "aws_s3_storage.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "aws_s3_storage.utils.before_app_install"
# after_app_install = "aws_s3_storage.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "aws_s3_storage.utils.before_app_uninstall"
# after_app_uninstall = "aws_s3_storage.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "aws_s3_storage.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

override_doctype_class = {"File": "aws_s3_storage.aws_s3_storage.file_override.S3File"}

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

scheduler_events = {
	"daily": ["aws_s3_storage.aws_s3_storage.s3_utils.sync_backups_to_s3"],
	"hourly": ["aws_s3_storage.aws_s3_storage.s3_utils.process_deletion_queue"],
}

# AWS S3 Integration Hooks
write_file = "aws_s3_storage.aws_s3_storage.s3_utils.write_file_to_s3"
delete_file_data_content = "aws_s3_storage.aws_s3_storage.s3_utils.delete_file_from_s3"
# s3_key/s3_thumbnail_key are included so that when Frappe reuses an existing file
# for a duplicate content hash, the new File record inherits the canonical keys.
write_file_keys = ["file_name", "file_url", "file_size", "s3_key", "s3_thumbnail_key"]

# Testing
# -------

# before_tests = "aws_s3_storage.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "aws_s3_storage.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "aws_s3_storage.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["aws_s3_storage.utils.before_request"]
# after_request = ["aws_s3_storage.utils.after_request"]

# Job Events
# ----------
# before_job = ["aws_s3_storage.utils.before_job"]
# after_job = ["aws_s3_storage.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"aws_s3_storage.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []
