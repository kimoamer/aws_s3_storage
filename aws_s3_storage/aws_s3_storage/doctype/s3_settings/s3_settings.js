// Copyright (c) 2026, Innomate LLC
// For license information, please see license.txt

frappe.ui.form.on("S3 Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Test Connection"), () => {
			frappe.call({
				method: "aws_s3_storage.aws_s3_storage.s3_utils.test_connection",
				freeze: true,
				freeze_message: __("Testing connection to S3…"),
				callback: (r) => {
					if (!r.exc && r.message) {
						frappe.msgprint({
							title: __("Connection successful"),
							message: r.message,
							indicator: "green",
						});
					}
				},
			});
		});

		frm.add_custom_button(__("Migrate Local Files"), () => {
			frappe.confirm(
				__(
					"Upload files currently stored on local disk to S3 in the background? Local copies are removed only after each object is verified in S3. Attach fields are repointed automatically; links embedded in rich text / Print Formats are not — run audit_local_links to review those."
				),
				() => {
					frappe.call({
						method: "aws_s3_storage.aws_s3_storage.migrate.start_migration",
						freeze: true,
						callback: (r) => {
							if (!r.exc) {
								frappe.msgprint({
									title: __("Migration started"),
									message: __("Queued in the background. Pending files: {0}", [
										(r.message && r.message.pending) || 0,
									]),
									indicator: "blue",
								});
							}
						},
					});
				}
			);
		});
	},
});
