// Copyright (c) 2026, Frappe and contributors
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
	},
});
