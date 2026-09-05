// Copyright (c) 2026, Innomate LLC
// For license information, please see license.txt

frappe.ui.form.on("S3 Settings", {
	refresh(frm) {
		const group = __("S3 Operations");

		frm.add_custom_button(
			__("Test Connection"),
			() => {
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
			},
			group
		);

		frm.add_custom_button(
			__("1. Migrate Files — Keep Local Copies"),
			() => {
				frappe.confirm(
					__(
						"Upload local files to S3 in the background and repoint their records? Only doctypes inside the configured scope are touched. Local copies are kept — reclaim disk space later with step 3."
					),
					() => {
						frappe.call({
							method: "aws_s3_storage.aws_s3_storage.migrate.start_migration",
							args: { batch_size: 100, delete_local: 0 },
							freeze: true,
							callback: (r) => {
								if (!r.exc) {
									frappe.show_alert({
										message: __("Migration queued ({0} pending).", [
											(r.message && r.message.total_files) || 0,
										]),
										indicator: "blue",
									});
								}
							},
						});
					}
				);
			},
			group
		);

		frm.add_custom_button(
			__("2. Audit Local Links"),
			() => {
				frappe.call({
					method: "aws_s3_storage.aws_s3_storage.migrate.audit_local_links",
					freeze: true,
					freeze_message: __("Scanning for embedded /files/ links…"),
					callback: (r) => {
						const rows = r.message || [];
						if (!rows.length) {
							frappe.msgprint({
								title: __("Audit"),
								message: __("No fields still contain local /files/ links."),
								indicator: "green",
							});
							return;
						}
						const body = rows
							.map(
								(x) =>
									`<tr><td>${frappe.utils.escape_html(x.doctype)}</td>` +
									`<td>${frappe.utils.escape_html(x.fieldname)}</td>` +
									`<td>${frappe.utils.escape_html(x.fieldtype)}</td>` +
									`<td style="text-align:right">${x.rows}</td></tr>`
							)
							.join("");
						frappe.msgprint({
							title: __("Fields with local links (review manually)"),
							message: `<table class="table table-bordered"><thead><tr>
								<th>${__("DocType")}</th><th>${__("Field")}</th>
								<th>${__("Type")}</th><th>${__("Rows")}</th></tr></thead>
								<tbody>${body}</tbody></table>`,
							indicator: "orange",
						});
					},
				});
			},
			group
		);

		frm.add_custom_button(
			__("3. Delete Verified Local Copies"),
			() => {
				frappe.confirm(
					__(
						"<b>Danger:</b> permanently delete local copies of files already migrated and verified in S3. Do this only after testing and running the audit. Continue?"
					),
					() => {
						frappe.call({
							method: "aws_s3_storage.aws_s3_storage.migrate.start_cleanup",
							args: { batch_size: 200 },
							freeze: true,
							callback: (r) => {
								if (!r.exc) {
									frappe.show_alert({
										message: __("Local cleanup queued."),
										indicator: "orange",
									});
								}
							},
						});
					}
				);
			},
			group
		);

		frm.add_custom_button(
			__("Migration Status"),
			() => {
				frappe.call({
					method: "aws_s3_storage.aws_s3_storage.migrate.get_migration_status",
					callback: (r) => {
						const s = r.message || {};
						const errors = s.errors || [];
						let errorHtml = "";
						if (errors.length) {
							const rows = errors
								.map(
									(e) =>
										`<tr><td>${frappe.utils.escape_html(e.file || "")}</td>` +
										`<td>${frappe.utils.escape_html(e.reason || "")}</td>` +
										`<td>${frappe.utils.escape_html(e.error || "")}</td></tr>`
								)
								.join("");
							errorHtml =
								`<p class="text-muted" style="margin-top:10px">${__(
									"Most recent failures (full list: S3 Migration Error):"
								)}</p>` +
								`<table class="table table-bordered"><thead><tr>
									<th>${__("File")}</th><th>${__("Reason")}</th><th>${__("Error")}</th>
								</tr></thead><tbody>${rows}</tbody></table>`;
						}
						frappe.msgprint({
							title: __("Migration Status"),
							message:
								`<table class="table table-bordered">
								<tr><td>${__("Status")}</td><td><b>${frappe.utils.escape_html(s.status || "Idle")}</b></td></tr>
								<tr><td>${__("Scope")}</td><td>${frappe.utils.escape_html(s.scope || "")}</td></tr>
								<tr><td>${__("Total")}</td><td>${s.total_files || 0}</td></tr>
								<tr><td>${__("Migrated")}</td><td>${s.migrated_files || 0}</td></tr>
								<tr><td>${__("Failed")}</td><td>${s.failed_files || 0}</td></tr>
								<tr><td>${__("Missing")}</td><td>${s.missing_files || 0}</td></tr>
								<tr><td>${__("Pending")}</td><td>${s.pending || 0}</td></tr>
								<tr><td>${__("Last file")}</td><td>${frappe.utils.escape_html(s.last_file || "")}</td></tr>
								</table>` +
								errorHtml +
								`<p class="text-muted">${__("Re-run step 1 to retry failed / remaining files.")}</p>`,
							indicator: s.status === "Running" ? "blue" : "green",
						});
					},
				});
			},
			group
		);
	},
});
