// Copyright (c) 2026, Innomate LLC
// For license information, please see license.txt

frappe.ui.form.on("S3 Settings", {
	refresh(frm) {
		const group = __("S3 Operations");

		show_ownership_state(frm);

		frm.add_custom_button(
			__("Take Ownership of This Storage"),
			() => confirm_take_ownership(frm),
			__("Storage Ownership")
		);

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

// ---------------------------------------------------------------------------
// Storage ownership
// ---------------------------------------------------------------------------
// Whether this site owns the bucket is the first thing an administrator needs to
// know — before the bucket name, before the migration buttons — because it is
// what decides whether anything they do here reaches another site's files. So it
// is a banner on the form, not a field they have to go looking for.

function show_ownership_state(frm) {
	frappe.call({
		method: "aws_s3_storage.aws_s3_storage.environment.get_environment_status",
		callback: (r) => {
			const s = r.message;
			if (!s) return;

			if (s.state === "foreign") {
				frm.dashboard.clear_headline();
				frm.set_intro(
					`<b>${__("This site does not own the configured storage.")}</b><br>` +
						`${frappe.utils.escape_html(s.reason || "")}<br><br>` +
						__(
							"Nothing in the bucket is created, moved or deleted from here. New uploads go to this site's own local disk. To give this site its own S3 storage, point it at a different bucket and then use <b>Take Ownership of This Storage</b>."
						),
					"orange",
					true
				);
				if (s.pending_deletions) {
					frm.add_custom_button(
						__("Park {0} Inherited Deletion(s)", [s.pending_deletions]),
						() => park_inherited_deletions(frm),
						__("Storage Ownership")
					);
				}
				return;
			}

			if (s.read_only_mode) {
				frm.set_intro(
					__(
						"<b>Read-Only Mode is on.</b> Files are served from the bucket, but nothing in it is created, moved or deleted from this site."
					),
					"blue",
					true
				);
				return;
			}

			if (s.state === "unclaimed") {
				frm.set_intro(
					__(
						"<b>No environment has claimed this storage yet.</b> Run <code>bench migrate</code>, or use <b>Take Ownership of This Storage</b>, so that a copy of this database restored onto another site can be told apart from this one."
					),
					"orange",
					true
				);
				return;
			}

			frm.set_intro(null);
			frm.dashboard.set_headline(
				__("This site owns the storage ({0}).", [frappe.utils.escape_html(s.owner_id || "")]) +
					(s.files_untagged
						? " " +
						  __(
								"{0} file(s) in S3 are not tagged with an owner yet — use <b>Take Ownership of This Storage</b> to tag them.",
								[s.files_untagged]
						  )
						: "")
			);
		},
	});
}

function confirm_take_ownership(frm) {
	const d = new frappe.ui.Dialog({
		title: __("Take ownership of this storage"),
		fields: [
			{
				fieldtype: "HTML",
				options: `<p>${__(
					"This site will become the owner of bucket <b>{0}</b>, and will be allowed to move and delete objects in it.",
					[frappe.utils.escape_html(frm.doc.bucket_name || "")]
				)}</p>
				<p class="text-danger">${__(
					"Only do this if no other site is still using this bucket. If you are setting up a test copy, give it its <b>own</b> bucket first — otherwise you are taking control of the files your live site is serving."
				)}</p>`,
			},
			{
				fieldtype: "Check",
				fieldname: "adopt_existing_files",
				label: __("Also tag existing S3 files as owned by this site"),
				description: __(
					"Recommended on the site that really owns the bucket. Leave off if the bucket holds files belonging to another environment."
				),
			},
		],
		primary_action_label: __("Take Ownership"),
		primary_action: (values) => {
			d.hide();
			frappe.call({
				method: "aws_s3_storage.aws_s3_storage.environment.claim_storage",
				args: { adopt_existing_files: values.adopt_existing_files ? 1 : 0 },
				freeze: true,
				callback: (r) => {
					if (r.exc) return;
					frappe.show_alert({ message: __("This site now owns the storage."), indicator: "green" });
					frm.reload_doc();
				},
			});
		},
	});
	d.show();
}

function park_inherited_deletions(frm) {
	frappe.confirm(
		__(
			"Park every pending deletion request so none of them is ever executed from this site? They stay in the list as <b>Blocked</b> and can be reviewed."
		),
		() => {
			frappe.call({
				method: "aws_s3_storage.aws_s3_storage.environment.block_inherited_deletions",
				freeze: true,
				callback: (r) => {
					if (r.exc) return;
					frappe.msgprint({
						title: __("Deletion queue"),
						message: __("{0} request(s) parked.", [(r.message && r.message.blocked) || 0]),
						indicator: "green",
					});
					frm.refresh();
				},
			});
		}
	);
}
