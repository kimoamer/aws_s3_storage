### Aws S3 Storage

AWS S3 integration for Frappe — stores uploaded files (and, optionally, site
backups) in an S3 bucket instead of on the local disk. The bucket stays fully
private and every file is served through short-lived presigned URLs.

It can be limited to specific doctypes instead of the whole site (§7), and §8
covers protecting the bucket against deletion from inside Frappe — versioning,
IAM, Object Lock and backups.

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch version-15
bench install-app aws_s3_storage
```

---

## Configuration

There are two sides to configure: your **AWS account** (bucket + credentials)
and the **app** (the *S3 Settings* form in Frappe).

### 1. AWS setup

#### 1.1 Create the bucket

1. In the S3 console, **Create bucket**.
2. Pick a **name** and a **Region** — note both, you'll enter them in the app.
3. **Block Public Access: keep all four options ON.** This app never relies on
   public objects; files are shared through presigned URLs, so the bucket can and
   should stay completely private.
4. **Object Ownership:** leave the default *Bucket owner enforced* (ACLs
   disabled). The app deliberately sets **no ACLs** on uploads, which is exactly
   what this mode expects — so you won't hit `AccessControlListNotSupported`.
5. (Optional) enable **Default encryption** (SSE-S3, or SSE-KMS if you have a
   key). See the note under §1.5.

#### 1.2 Create an IAM policy

Create a policy granting only what the app needs. Replace `YOUR_BUCKET` with your
bucket name:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AppBucketAccess",
      "Effect": "Allow",
      "Action": [
        "s3:ListBucket",
        "s3:GetBucketLocation"
      ],
      "Resource": "arn:aws:s3:::YOUR_BUCKET"
    },
    {
      "Sid": "AppObjectAccess",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject"
      ],
      "Resource": "arn:aws:s3:::YOUR_BUCKET/*"
    }
  ]
}
```

> Note the two **different** resources: bucket-level actions target the bucket ARN
> (`arn:aws:s3:::YOUR_BUCKET`) while object actions target everything inside it
> (`.../*`). Splitting them like this is what makes the policy work — a single
> statement covering only `/*` causes `AccessDenied` on the bucket-level calls.

Why each action is needed:

| Action | Resource | Used for |
| --- | --- | --- |
| `s3:GetObject` | `/*` | Serving files (presigned GET), reading content/thumbnails server-side, and `HeadObject` for idempotent backup sync. |
| `s3:PutObject` | `/*` | Uploading files, thumbnails, and backups. |
| `s3:DeleteObject` | `/*` | Removing objects when a File is deleted. |
| `s3:ListBucket` | bucket | `HeadBucket`, used by the **Test Connection** button. |
| `s3:GetBucketLocation` | bucket | boto3 resolves the bucket's region with this call; without it some setups fail with `AccessDenied`. |
| `s3:GetBucketVersioning` | bucket | Optional. Lets **Test Connection** report whether deletions are recoverable (§8). Without it everything still works; the dialog just says it could not check. |

#### 1.3 Create an IAM user and access keys

1. Create an **IAM user** for programmatic access and attach the policy above.
2. Create an **access key** for that user.
3. Copy the **Access Key ID** and **Secret Access Key** — you'll paste them into
   the app. Store the secret safely; AWS shows it only once.

> Prefer a dedicated IAM user scoped to this one bucket over reusing broad
> credentials.

#### 1.4 CORS (usually not required)

Files are shown via `<img>`/download links that follow a redirect to S3, which
does **not** need CORS. Only add a CORS rule if you have custom front-end code
that fetches objects with `fetch()`/`XMLHttpRequest` or draws them onto a
`<canvas>`. Example minimal rule:

```json
[
  {
    "AllowedHeaders": ["*"],
    "AllowedMethods": ["GET"],
    "AllowedOrigins": ["https://your-frappe-site.example"],
    "ExposeHeaders": []
  }
]
```

#### 1.5 Encryption (optional)

- **SSE-S3** works transparently — no extra IAM permissions or app settings.
- **SSE-KMS** also works, but the IAM user additionally needs `kms:Decrypt` and
  `kms:GenerateDataKey` on the key. Upload integrity (Content-MD5) is unaffected.

### 2. App settings (S3 Settings)

Open **S3 Settings** (a single doctype, System Manager only) and fill it in.

#### Connection

| Field | Required | Description |
| --- | --- | --- |
| **Enable S3 Storage** | — | Switch for *new uploads only* (on by default). Off — or before a bucket is configured — uploads fall back to Frappe's local storage instead of failing, and nothing is moved or pulled back out of the bucket. **It does not stop deletions.** Deleting a File still deletes its object, and privacy moves still move objects. This is not a safety switch; **Read-Only Mode** below is. |
| **Bucket Name** | Yes | The exact S3 bucket name. |
| **Region** | Yes | The bucket's region code, e.g. `eu-central-1`. Must match the bucket's actual region — presigned URLs (SigV4) fail if it doesn't. |
| **Access Key ID** | No | The IAM user's access key ID. **Leave blank on AWS to use the server's EC2 IAM role / instance profile** (recommended — no secret stored in the database). |
| **Secret Access Key** | No | The IAM user's secret. Stored encrypted (Password field). Required only if an Access Key ID is set. |
| **Endpoint URL** | No | Leave blank for AWS S3. Set it for **S3-compatible** services (MinIO, DigitalOcean Spaces, Wasabi, …); path-style addressing is then used automatically. |

> **Credentials:** provide **both** the key and secret, or **neither**. When both
> are blank, boto3 uses its default credential chain (EC2 instance profile,
> environment, …) — the preferred setup on AWS, since no long-lived secret lives
> in the site database.

#### Advanced

| Field | Default | Description |
| --- | --- | --- |
| **Storage Class** | `STANDARD` | S3 tier for uploaded objects. See the table below. |
| **Presigned URL Expiry (seconds)** | `3600` | How long a generated download/view link stays valid. Automatically clamped to the S3-allowed range **60–604800** (1 minute to 7 days). Shorter is more secure; longer is friendlier for links that get cached or shared. |
| **Verify Upload Integrity** | On | Sends a `Content-MD5` header with each upload so S3 rejects a corrupted transfer (`BadDigest`) instead of silently storing damaged bytes. Recommended on; turn off only if a non-AWS endpoint rejects the header. |
| **Enable Daily Backup Sync** | Off | When on, the daily scheduler uploads site backups to `backups/<site>/`. See §3. |
| **Backup Storage Class** | `STANDARD` | Storage tier for backups, **independent** of attachments — e.g. `STANDARD_IA` or `GLACIER_IR` for cheap cold storage while attachments stay `STANDARD`. |
| **Delete Local Backup After Sync** | Off | After a backup is verified present in S3 (same size), delete the local copy to reclaim server disk space. |

**Storage Class options**

| Value | When to use |
| --- | --- |
| `STANDARD` | Default. Frequently accessed files. |
| `STANDARD_IA` | Infrequently accessed; cheaper storage, small retrieval fee. |
| `INTELLIGENT_TIERING` | Access pattern unknown/variable; S3 moves objects between tiers automatically. |
| `ONEZONE_IA` | Infrequent access, stored in a single Availability Zone (cheaper, lower durability). |
| `GLACIER_IR` | Archive with **instant** retrieval. |
| `DEEP_ARCHIVE` | Lowest cost, but retrieval takes hours and requires a restore. |

> ⚠️ **Do not** use `DEEP_ARCHIVE` (or `GLACIER`) for user-facing attachments —
> a presigned download would fail until the object is restored. These tiers only
> make sense for cold data such as backups.

#### Storage Ownership

This section decides whether *this site* is allowed to change anything in the
bucket. It is the answer to "we restored production onto a test server, and now
the test server is deleting production's files" — see §9.

| Field | Default | Description |
| --- | --- | --- |
| **Read-Only Mode (Never Modify the Bucket)** | Off | The one switch that means what it says: files are still served from S3, but nothing in the bucket is created, moved or deleted from this site. Covers **every** path — uploads (they fall back to local disk), deletions, privacy moves, thumbnails, the deletion queue, backup sync, the migration jobs and the patches. This, not *Enable S3 Storage*, is how you stop the app touching the bucket. |
| **Storage Owner ID** | set automatically | Which environment owns this bucket. Written on install; the matching value is stored in `site_config.json`, which is *not* part of a database backup. That mismatch is how a restored copy is told apart from the original. Read-only. |
| **Owner Site** | set automatically | The site name that claimed the bucket. Read-only. |
| **Owner Instance** | set automatically | The server it was claimed from. Diagnostic only — a rename or a rebuilt container changes it legitimately, so the bucket's ownership lease (§9.3), not this, decides what is allowed. Read-only. |
| **Ownership Claimed At** | set automatically | When it was claimed. Read-only. |
| **Files Owned by Another Environment** | `Read Only` | What this site may do to objects a *different* environment uploaded (File records carrying another owner id). Only relevant after you deliberately take ownership of a bucket that already holds someone else's files. Leave it on `Read Only`. |

The form shows the current state as a banner, and **Test Connection** reports it
too. The **Take Ownership of This Storage** button is under the *Storage
Ownership* menu — read §9 before using it.

#### Doctype Scope

| Field | Default | Description |
| --- | --- | --- |
| **Limit S3 Storage to Specific Doctypes** | Off | Off = site-wide: every upload goes to S3. On = only attachments of the doctypes listed below are stored in S3; everything else keeps using Frappe's local storage and the migration jobs leave it on disk. See §7. |
| **Doctypes Stored in S3** | empty | One doctype per line (e.g. `Sales Invoice`), matched against the attachment's **Attached To DocType**. |
| **Include Files Not Attached to a Document** | Off | Whether files that hang off no document — uploads from the File list, letter head / print logos, Web Form uploads before submission — also go to S3. They have no doctype to match, so they get their own switch. |

#### Guest Access

| Field | Default | Description |
| --- | --- | --- |
| **Allow Guests to Download Their Own Uploads** | Off | Lets an anonymous visitor open a **private** object when the File record is owned by `Guest` — the Web Form attachment case (see §4). Off by default. |
| **Guest Readable Doctypes** | empty | Optional allowlist, one doctype per line. Restricts the rule above to guest uploads attached to those doctypes. Empty means any guest upload. |

#### Test Connection

After saving, click **Test Connection**. It runs a `HeadBucket` call and reports
whether the credentials, region, and bucket name are correct.

It also reports whether the bucket keeps **versions** — the one setting that
decides whether a file deleted in Frappe can be brought back (§8). If versioning
is off, the dialog says so; if the IAM user cannot read it, add
`s3:GetBucketVersioning` to the policy (§1.2) or check it in the S3 console.

### 3. Backup sync

- Requires **Enable Daily Backup Sync** to be on **and** the bench scheduler to
  be running (`bench --site <site> enable-scheduler`).
- Runs from the `daily` scheduler event and uploads every backup file under the
  site's `private/backups` folder to `backups/<site>/` in the bucket.
- It is **idempotent**: a backup already present in S3 with a matching size is
  skipped, and a missed run simply uploads whatever is still missing — so backups
  aren't lost if a run is skipped.
- Large backups use multipart upload automatically.

### 4. How files are served

The bucket is kept **fully private** — no public ACLs are set. Every file is
served through a short-lived presigned URL, and private files additionally
require read permission on the corresponding File document. Objects are only
deleted from S3 **after** the database transaction that removed the File record
has committed, so a rolled-back delete never leaves a File pointing at a missing
object.

Image thumbnails are generated by reading the source object from S3 directly (via
boto3, so private files work without an HTTP round-trip) and are stored back in S3
as their own object, keeping the source file's privacy.

Only objects under `public/` and `private/` are ever reachable through the
download endpoint — backups and any other prefix are never web-servable, even to a
System Manager, so a database backup can never be handed out via a presigned URL.

**Privacy changes are honoured.** Flipping a File's *Private* flag copies the
object (and its thumbnail) to the matching prefix, updates the record, and removes
the old object after commit — a file marked private is no longer reachable under a
public key.

**Deletions are safe and durable.** An object is removed only after the File
delete commits, only when nothing still references it, and a delete that fails is
queued in **S3 Deletion Queue** and retried hourly so nothing is orphaned. Objects
uploaded inside a transaction that rolls back are cleaned up automatically.

"Nothing still references it" is checked in three steps, cheapest first: another
File's `s3_key`, another File's URL, and finally the **Attach fields of every
doctype**. That last step matters because an Attach field stores the URL itself,
and the value travels between documents (`fetch_from`, an Amend, a script copying
a Job Applicant's CV onto the Interview). The File it came from can be deleted
along with its own document while other documents still show the attachment —
without the scan the object would go with it and every one of those links would
die, with nothing in the bucket to restore. A field that cannot be scanned counts
as a reference: keeping an unused object is always cheaper than deleting a live
one. The field list is cached, and the scan is only reached for a real deletion
whose key no File record covers any more.

The one deletion that ignores document links is the old object left behind by a
**privacy change**: there the object is superseded by a copy of itself under the
other prefix, and it has to go, or a file just marked private stays readable under
its public key.

#### Guest uploads on Web Forms

A visitor who attaches a file to a Web Form uploads it as a **private** File owned
by `Guest`. Frappe grants a Guest no read permission on a private File, so the
attachment cannot be shown back to the visitor — neither in the preview right
after upload nor on the submitted document. The result is a broken image or a
`403` on a form that otherwise worked.

Turning on **Allow Guests to Download Their Own Uploads** closes exactly that gap:

- Only objects whose File record has `owner = "Guest"` qualify, i.e. only what a
  visitor uploaded themselves — never another user's private file.
- A file not attached to anything yet always passes: that is the state of every
  upload between the file being sent and the form being submitted, which is the
  preview the rule exists for.
- Fill in **Guest Readable Doctypes** with the Web Form's doctype to narrow the
  rule to that form's attachments once they are attached.

The object stays private in the bucket and is still served through a short-lived
presigned URL; what protects it from other visitors is the `uuid4` in its key —
exactly the protection a `public/` object already relies on. ⚠️ **If the form
collects sensitive documents (IDs, official papers), leave the checkbox off** and
show the attachment to logged-in users only.

The rule is applied at *serve* time, so files uploaded before it was enabled work
too: no object is moved and no `file_url` changes — which matters, because that
URL is stored inside the document's own Attach field and rewriting it would break
every existing link.

#### When a link has no File record left

The rule above needs a File record to reason about. An attachment can lose one —
a Web Form upload whose File was never created, a value written into an Attach
field with `db_set`/SQL, or a File deleted while another document still held its
URL. The URL then 403s for **everyone**, not just for a Guest, because there is
nothing left to check permission against.

The `restore_missing_file_records` patch repairs that on `bench migrate`. It walks
every stored Attach / Attach Image field and, for each S3 link with no File record:

- recreates the record **attached to the document that references it**, so read
  permission flows from that document like any normal attachment, or
- prints the link when the object is gone from the bucket too — that one cannot be
  repaired, the file has to be uploaded again.

No object is written and no URL is rewritten; only the missing rows come back. It
is idempotent, and can be re-run at any time:

```bash
bench --site <site> execute aws_s3_storage.patches.v1_0.restore_missing_file_records.execute
```

### 5. Attachments that deliberately stay on local disk

A few attachments are not opaque blobs: the app that owns them reopens the file
**by path** and rewrites it in place. ERPNext's **reposting data file** is the
known case — `erpnext/stock/stock_ledger.py` does

```python
path = file_doc.get_full_path()
with open(path, "wb") as f: ...
```

after every reposting batch. An S3-backed File has no path on disk, so every
repost fails with `FileNotFoundError: [Errno 2] No such file or directory:
'/api/method/…download_file?key=private/…/repost_item_valuation-….json.gz'`.

These files therefore **bypass S3 and use Frappe's local storage**:

| Rule | Value |
| --- | --- |
| `attached_to_doctype` | `Repost Item Valuation` |
| `attached_to_field` | `reposting_data_file` |
| File name | `repost_item_valuation-*.json.gz` |

They are small, short-lived, and ERPNext deletes them once the repost finishes,
so nothing meaningful accumulates on disk. Content-hash deduplication is also
skipped for them (two Repost Item Valuations must never share one file), and the
migration jobs never move them into the bucket.

Files uploaded to S3 *before* this rule existed are brought back to disk
automatically by the `move_local_only_files_to_disk` patch on `bench migrate`:
each object is downloaded, written to `private/files`, the File record and the
linked field are repointed, and the object is then removed from the bucket.

To keep another app's attachment local, add its doctype or fieldname to
`LOCAL_ONLY_ATTACHED_TO_DOCTYPES` / `LOCAL_ONLY_ATTACHED_TO_FIELDS` in
`aws_s3_storage/aws_s3_storage/s3_utils.py`.

### 6. Migrating existing local files

Files that were uploaded **before** installing the app stay on local disk. Move
them into S3 from **S3 Settings → S3 Operations**, which walks the safe order:

1. **Migrate Files — Keep Local Copies** — uploads + repoints records, keeps local.
2. **Audit Local Links** — read-only report of embedded links to review.
3. **Delete Verified Local Copies** — reclaims disk space (strong confirmation).
4. **Migration Status** — live counts (total / migrated / failed / missing /
   pending) **and the reason each file failed**; re-run step 1 to retry failed or
   remaining files.

> With **Limit S3 Storage to Specific Doctypes** on (§7), every step below — the
> buttons, the console commands and the daily job — only ever touches attachments
> inside that scope, and the pending count reflects it.

Every file that fails or is missing is recorded in the **S3 Migration Error**
doctype (file, reason, error) — the Migration Status dialog shows the most recent,
and the full list is in that doctype's list view. The list is cleared at the start
of each run.

#### Migrate a bit each day (scheduled)

For very large libraries you can drip-feed the migration instead of one long run.
Under **S3 Settings → Scheduled Migration**:

| Field | Description |
| --- | --- |
| **Enable Daily Auto-Migration** | The daily scheduler migrates the next batch automatically, until none remain. |
| **Files Per Day** | Cap per day (default 5000). At 5000/day, 100k files finish in ~20 days. |
| **Delete Local After Migration** | Delete each local copy right after it's migrated and verified in S3, so disk frees up **as files move**. |

Progress accumulates across days in **Migration Status**. It never overlaps a
manual run, and pauses (`Idle`) between days. Turn on **Delete Local After
Migration** only after you've verified a sample (attachments, images, print
formats) — until then, leave it off and reclaim space later with **Delete Verified
Local Copies**. Requires the bench scheduler to be enabled.

The migration runs in batches and is **resumable** — each migrated file records
its S3 key and drops out of the pending set, so it is safe to stop and restart.
Uploads **stream from disk** (multipart), so large files do not have to fit in
memory. There are two ways to run it, with different completion behaviour:

- **S3 Settings button** → a chained background job: each job works for a bounded
  time (~1000s) then re-enqueues itself, so migrating thousands of files on a
  managed platform (e.g. Frappe Cloud) never hits the worker's per-job timeout.
- **`bench execute … run_migration`** → runs to completion in that one process and
  finishes (status `Completed`); it does not detach or leave the status stuck on
  `Running`. Use this from the console for large one-shot migrations. Only **one** migration can run at a time (a second start is
refused while one is active; a crashed run can be cleared with
`reset_migration_status`). Files whose content is missing on disk are skipped for
the rest of a run rather than retried in a loop, and a local file shared by several
File records is kept until the last of them has been migrated.

The same steps are available on the console:

```bash
bench --site <site> execute aws_s3_storage.aws_s3_storage.migrate.run_migration \
    --kwargs '{"batch_size": 100, "delete_local": 0}'
bench --site <site> execute aws_s3_storage.aws_s3_storage.migrate.audit_local_links
bench --site <site> execute aws_s3_storage.aws_s3_storage.migrate.cleanup_migrated_local_files
```

**What gets repointed.** Migration updates the `File` record (`file_url`/`s3_key`)
**and** the linked document's own **Attach / Attach Image** field. Attachments
shown in the sidebar (linked by `attached_to_doctype`/`_name`) keep working
automatically. Links **embedded in rich text** — Text Editor / HTML fields, Print
Formats, old timeline comments — are **not** rewritten. Run the read-only audit to
find them and review them by hand before you rely on local files being gone:

```bash
bench --site <site> execute aws_s3_storage.aws_s3_storage.migrate.audit_local_links
```

**Recommended production rollout** (take a server/DB snapshot first):

1. **Migrate Files — Keep Local Copies** and watch **Migration Status** to
   completion.
2. Spot-check that upload / download / delete work for a **public** and a
   **private** file, and that uploading the same file twice behaves.
3. **Audit Local Links** and fix any embedded links it reports.
4. Compare object counts/sizes between the local `files` folders and the bucket's
   `public/` and `private/` prefixes.
5. Only then **Delete Verified Local Copies** to reclaim space. Do **not** re-run
   the migration to delete — migrated files have already left the pending set, so
   deletion is a separate step (each local copy is removed only after its S3 object
   is verified present **with a matching size**).

### 7. Limiting S3 to specific doctypes

By default the integration is **site-wide**: every upload goes to S3 and the
migration moves every local file into the bucket. Turn on **Limit S3 Storage to
Specific Doctypes** under **S3 Settings → Doctype Scope** to keep only part of
your data there — a cautious rollout (start with one doctype, widen later), a
cost or data-residency rule, or a doctype whose attachments must stay on the
server.

List the doctypes one per line:

```
Sales Invoice
Purchase Invoice
Employee
```

**What the scope is matched against.** Every attachment stores the document it
hangs off in `File.attached_to_doctype` — the same value the File list shows as
*Attached To DocType*. That is what the list is compared with, so it covers the
attachments of those documents, whether they were added from the sidebar or
through an **Attach** / **Attach Image** field. To see what your site actually
has, run in `bench --site <site> console`:

```python
frappe.db.sql("""
    SELECT COALESCE(NULLIF(attached_to_doctype, ''), '(not attached)') AS doctype,
           COUNT(*) AS files
    FROM `tabFile` WHERE is_folder = 0
    GROUP BY doctype ORDER BY files DESC
""", as_dict=True)
```

**What the scope changes**

| | In scope | Out of scope |
| --- | --- | --- |
| New uploads | Stored in S3 | Stored on local disk (Frappe's default) — the same fallback the master switch uses |
| Migration (manual **and** daily) | Migrated | Never touched; not counted as pending |
| Attached, or re-attached, later | Moved **into** S3 | Moved **back to local disk** |
| Duplicate of a file stored elsewhere | Written to S3 anyway | Written to disk anyway |
| Files already in S3, doctype still in scope | Served / moved / deleted as usual | — |
| Backups (§3) | — | Unaffected: backups are not File records and have their own switch |

**A file follows its document.** The scope can only be read from the document an
attachment hangs off, and that link is not always there when the file is written:
a Web Form uploads its attachment *before* the document exists, and an attachment
can be re-attached — or copied by an Amend — onto a different doctype much later.
So the decision is re-made every time a File record is saved, and the file moves
to where it now belongs:

- **into S3** when its doctype enters the scope (this is what makes the Web Form
  case work: the upload starts local, then moves once the form is submitted), and
- **back to local disk** when it lands on a doctype the scope excludes — the
  object is downloaded, written to the site's files folder, the record and the
  document field are repointed, and the object is deleted **only if no other File
  record or Attach field still uses it**. An object shared with an in-scope
  record stays in the bucket for that record.

The move runs as a background job after the save commits, so it never slows down
(or fails) the save that triggered it. It is a no-op unless the scope is
restricted, and re-checks everything when it runs, so it is safe to repeat.

**Deduplication follows the scope too.** Frappe reuses an existing file whenever
the content hash matches, before the storage hook runs — which would put an
out-of-scope attachment on an S3 object, or leave an in-scope one on local disk
because an identical file was uploaded before the bucket existed. Both are
corrected: when a reused file is on the wrong storage, the content is written
again where it belongs. Identical files stored in the *same* place still
deduplicate normally.

Narrowing the scope later never orphans anything: objects already in the bucket
keep being served through presigned URLs, follow a privacy change between the
`public/` and `private/` prefixes, and are removed when their File record is
deleted, exactly as before. Existing files do **not** move on their own — only a
record that is saved again is re-evaluated.

**Migration Status** shows the active scope, and its *Pending* count is the
number of files inside it — so you can widen the list and watch the count grow
before starting a run.

Notes and edge cases:

- Attachments that must stay on local disk (§5) are excluded first, whatever the
  scope says.
- Restricting the scope with **no** doctype listed and the unattached switch off
  means nothing is stored in S3 at all; the settings form warns you when you save
  it. A doctype name that does not exist (a typo) gets the same warning.
- Print formats, letter heads, site logo and anything uploaded straight from the
  File list are *unattached* — they follow **Include Files Not Attached to a
  Document**, not the doctype list. A Web Form upload is unattached only until
  the form is submitted; after that it follows the doctype it was attached to.
- **Delete Verified Local Copies** (§6) only ever walks records that already
  carry an S3 key, and it keeps any local file another File record still points
  at. A file the scope keeps on disk has no S3 key, so it is never a candidate —
  the button cannot delete it. What it does clean up is the local copy of a file
  that *is* in S3, including one migrated before the scope was narrowed; that
  copy is redundant, and each one is removed only after its object is verified
  present with a matching size.
- **Audit Local Links** (§6) stays site-wide on purpose: it reports every field
  that still contains a `/files/` link, whatever doctype the file itself belongs
  to. With a restricted scope many of those are simply files that are meant to
  stay on disk.
- Moving a file back to disk downloads the object into memory. That is fine for
  ordinary attachments; if you are about to re-attach something very large to an
  out-of-scope doctype, expect one download per file.

**Turning it on (the setting is off by default).** Installing this app — or this
branch — changes nothing on its own: storage stays site-wide until you tick the
box. A safe rollout:

1. Enable **Bucket Versioning** first (§8.2) — everything below deletes and moves
   objects, and versioning is what makes any of it reversible.
2. Run the query above and decide the list of doctypes.
3. Tick **Limit S3 Storage to Specific Doctypes**, fill the list, decide the
   unattached switch, save. Check the warning if one appears.
4. Open **Migration Status** and confirm the **Scope** line and the **Pending**
   count are what you expect *before* starting a migration.
5. Upload one attachment on an in-scope doctype and one on an out-of-scope
   doctype, and check where each landed (the File record's hidden **S3 Key**
   field is set only for the S3 one).
6. Then run the migration (§6).

### 8. Protecting the bucket from deletion (versioning, backup, Object Lock)

Once attachments live in S3, a delete inside Frappe reaches into the bucket: the
app removes the object when the File record that owns it is deleted. That is the
correct default — otherwise every deleted attachment would be billed forever —
but it means a mistaken bulk delete, a wrong cascade, a bad script or a stolen
key can destroy files. This section is about making that **recoverable**, at the
AWS layer, where nothing inside Frappe can undo it.

#### 8.1 What actually deletes an object

| Trigger | What the app does |
| --- | --- |
| A **File record is deleted** — from the File list, by a user deleting a document (attachments cascade), by a script, `frappe.delete_doc`, a bulk delete | `DeleteObject` **after the transaction commits**, and only if no other File record and no document's Attach field still holds that key (§4). A rolled-back delete removes nothing. |
| **Privacy changes** (public ↔ private) | The object is copied to the other prefix and the old key is deleted after commit (unless something still references it). |
| An attachment moves to a doctype **outside** the S3 scope (§7) | The object is downloaded to local disk, the record repointed, and the object deleted after commit — again only if nothing else references it. |
| A delete that **fails** (permissions, network) | The key is queued in **S3 Deletion Queue** and retried hourly, up to 10 attempts, then marked `Failed` and left alone. |
| **Migration step 3** / *Delete Local After Migration* | Deletes **local copies only** — never an S3 object. |
| **Backup sync** (§3) | Never deletes anything in S3 (only the local backup file, if you enable that). |
| Dropping / restoring the site database | Deletes nothing in S3. The File records disappear, the objects stay behind as orphans. |

Nothing else in the app issues a delete. So the whole exposure is: *a File record
disappears in Frappe → its object disappears in S3.* Everything below removes the
"permanently" from that sentence.

> Two things inside the app stop a delete outright, and **"Enable S3 Storage" is
> not one of them** — turning it off only changes where *new uploads* go.
> **Read-Only Mode** (§2) stops every modification from this site, and the
> ownership checks in §9 stop them from a site that does not own the bucket.

#### 8.2 Layer 1 — turn on Bucket Versioning (do this first)

Versioning is the single most valuable setting here, and it needs no change in
the app.

```bash
aws s3api put-bucket-versioning --bucket YOUR_BUCKET \
  --versioning-configuration Status=Enabled

aws s3api get-bucket-versioning --bucket YOUR_BUCKET   # -> {"Status": "Enabled"}
```

(Console: **Bucket → Properties → Bucket Versioning → Edit → Enable**.)

With versioning on, the `DeleteObject` the app issues no longer destroys
anything: S3 writes a **delete marker** on top of the key and keeps the bytes as
a noncurrent version. The file 404s in Frappe — and comes back the moment you
remove the marker.

```bash
# 1. find the object and its delete marker (the key is in the File record's
#    hidden "S3 Key" field, or in the ?key= part of its file_url)
aws s3api list-object-versions --bucket YOUR_BUCKET --prefix "private/" \
  --query "DeleteMarkers[?contains(Key, 'report.pdf')].[Key,VersionId,LastModified]" \
  --output table

# 2. delete the delete marker -> the object is live again under the same key
aws s3api delete-object --bucket YOUR_BUCKET \
  --key "private/9f3c1a2b4d5e/report.pdf" \
  --version-id "<delete-marker-version-id>"
```

Two things make versioning cheap here:

- The app **never overwrites** an object — every upload gets its own
  `public|private/<uuid>/<filename>` key — so versions only accumulate from
  deletes and privacy moves, not from ordinary editing.
- Presigned downloads are unaffected: a `GET` without a version id always serves
  the current version.

> Enable versioning **before** you run the migration (§6), so the whole library is
> covered from the moment it lands in the bucket.

#### 8.3 Layer 2 — take permanent deletion away from the app (IAM)

Versioning protects you only while nobody can delete the versions themselves. The
app never needs to: it always calls `DeleteObject` **without** a version id. So
deny the destructive actions on the *app's own* IAM user — add this statement to
the policy from §1.2:

```json
{
  "Sid": "NeverDestroyHistory",
  "Effect": "Deny",
  "Action": [
    "s3:DeleteObjectVersion",
    "s3:DeleteObjectVersionTagging",
    "s3:PutBucketVersioning",
    "s3:PutLifecycleConfiguration",
    "s3:PutBucketReplication",
    "s3:PutBucketPolicy",
    "s3:DeleteBucketPolicy",
    "s3:DeleteBucket",
    "s3:PutObjectRetention",
    "s3:PutObjectLegalHold",
    "s3:BypassGovernanceRetention"
  ],
  "Resource": [
    "arn:aws:s3:::YOUR_BUCKET",
    "arn:aws:s3:::YOUR_BUCKET/*"
  ]
}
```

An explicit `Deny` beats any `Allow`, so even a compromised key or a future
policy edit cannot purge version history, switch versioning off, or add a
lifecycle rule that expires everything tomorrow. Nothing the app does is
affected.

> Attach this to the **application's** IAM user only — not account-wide. An
> administrator still needs `s3:DeleteObjectVersion` to perform the restore in
> §8.2 and to clean up orphans.

#### 8.4 Layer 3 — no deletion at all (optional, stricter)

If you want Frappe to be *unable* to remove anything from the bucket, drop
`s3:DeleteObject` from the Allow statement in §1.2 as well. Know exactly what you
are choosing:

- Every delete fails, is queued in **S3 Deletion Queue**, retried hourly 10 times,
  then marked `Failed`. Nothing breaks in Frappe, and the queue stops growing per
  key — but it becomes a log of objects you must clean up yourself.
- Deleted attachments keep costing storage forever, with no lifecycle rule able to
  reap them (the objects stay *current*, so `NoncurrentVersionExpiration` never
  applies to them).
- **A privacy change stops being airtight.** Making a public file private copies
  the object to `private/…` but can no longer remove the old `public/…` copy — and
  a `public/` key is served to anyone who has the link, without a permission check.
  Anyone who kept the old URL keeps access.

For almost every site, §8.2 + §8.3 (versioning + deny version deletes) is the
better trade: deletion still works, but it is always reversible.

#### 8.5 Layer 4 — MFA Delete

Requires the **root** account credentials and the CLI (it cannot be set in the
console):

```bash
aws s3api put-bucket-versioning --bucket YOUR_BUCKET \
  --versioning-configuration Status=Enabled,MFADelete=Enabled \
  --mfa "arn:aws:iam::ACCOUNT_ID:mfa/root-account-mfa-device 123456"
```

Deleting a *version* or turning versioning off then requires a fresh MFA code.
The app is unaffected — a plain `DeleteObject` that writes a delete marker is
still allowed.

#### 8.6 Layer 5 — Object Lock (WORM / ransomware protection)

Object Lock makes versions genuinely immutable for a retention period. It
requires versioning, and is enabled at bucket creation or afterwards on a
versioned bucket:

```bash
aws s3api put-object-lock-configuration --bucket YOUR_BUCKET \
  --object-lock-configuration '{
    "ObjectLockEnabled": "Enabled",
    "Rule": { "DefaultRetention": { "Mode": "GOVERNANCE", "Days": 30 } }
  }'
```

- **GOVERNANCE** — versions cannot be deleted for 30 days; an administrator with
  `s3:BypassGovernanceRetention` can override. Recommended.
- **COMPLIANCE** — nobody can delete them before the period ends, root included.
  Use only if you must, and start with a short retention: you are committing to
  pay for that storage no matter what.

The app keeps working unchanged: uploads are new keys, and its version-less
delete still just writes a delete marker.

#### 8.7 Layer 6 — a copy outside the bucket

Versioning protects the objects; it does not protect the *bucket*. For a real
disaster copy, replicate to a second bucket — ideally in another AWS account, so
a compromise of the site's credentials cannot reach it:

```bash
aws s3api put-bucket-replication --bucket YOUR_BUCKET --replication-configuration '{
  "Role": "arn:aws:iam::ACCOUNT_ID:role/s3-replication-role",
  "Rules": [{
    "ID": "copy-everything",
    "Priority": 0,
    "Filter": {},
    "Status": "Enabled",
    "DeleteMarkerReplication": { "Status": "Disabled" },
    "Destination": {
      "Bucket": "arn:aws:s3:::YOUR_BUCKET-dr",
      "Account": "DESTINATION_ACCOUNT_ID",
      "StorageClass": "STANDARD_IA",
      "AccessControlTranslation": { "Owner": "Destination" }
    }
  }]
}'
```

Both buckets must have versioning enabled. Keep `DeleteMarkerReplication`
**disabled**: deletes in Frappe then never hide anything in the copy. (Version
deletions are never replicated by S3 in any case.)

**AWS Backup** is the managed alternative — point a backup plan at the bucket for
scheduled, point-in-time restores into a separate vault, and turn on **Vault
Lock** to make the recovery points themselves immutable. It also requires
versioning on the bucket.

#### 8.8 Keep the cost of all this bounded (lifecycle rules)

Versioning without a lifecycle rule grows forever. This one keeps 90 days of
deleted files, tidies up delete markers and aborted uploads, and ages backups
into cheap storage:

```json
{
  "Rules": [
    {
      "ID": "keep-90-days-of-deleted-files",
      "Filter": { "Prefix": "" },
      "Status": "Enabled",
      "NoncurrentVersionExpiration": { "NoncurrentDays": 90 },
      "Expiration": { "ExpiredObjectDeleteMarker": true },
      "AbortIncompleteMultipartUpload": { "DaysAfterInitiation": 7 }
    },
    {
      "ID": "backups-cheap-and-capped",
      "Filter": { "Prefix": "backups/" },
      "Status": "Enabled",
      "Transitions": [{ "Days": 30, "StorageClass": "GLACIER_IR" }],
      "Expiration": { "Days": 365 }
    }
  ]
}
```

```bash
aws s3api put-bucket-lifecycle-configuration --bucket YOUR_BUCKET \
  --lifecycle-configuration file://lifecycle.json
```

> ⚠️ `NoncurrentDays` **is** your recovery window: after it passes, a deleted
> attachment is gone for good. Pick a number you can live with (90 days is a
> reasonable default; regulated data usually wants more). Never apply an
> `Expiration.Days` rule to the `public/` or `private/` prefixes — that deletes
> live attachments.

#### 8.9 Inside Frappe

The AWS layers above are what actually protect the data; these reduce how often
you need them:

- The app already refuses to delete an object that another File record, or a
  document's **Attach** field, still points at (§4) — so an Amend, a duplicated
  upload or a URL copied onto another document never loses its file.
- Take `delete` on the **File** doctype away from everyone but System Manager
  (Role Permissions Manager). Note this stops deletion *from the File list*, not
  the cascade that removes attachments when their document is deleted — that runs
  with permissions ignored, which is exactly why §8.2 matters.
- Keep **Delete Local After Migration** off until you have verified a sample (§6);
  until then the local copy is a second copy.
- Turn on **Enable Daily Backup Sync** (§3). A database backup restores the File
  *records*; the objects are protected separately by versioning.

#### 8.10 Restoring a deleted attachment end to end

A file has two halves — the object in the bucket and the File record that
describes it — and a delete usually removes both.

1. **Bring the object back**: remove its delete marker (§8.2).
2. **Bring the record back**. If a document's Attach field still holds the URL,
   the app can rebuild it for you:

   ```bash
   bench --site <site> execute \
     aws_s3_storage.patches.v1_0.restore_missing_file_records.execute
   ```

   Otherwise recreate it in `bench --site <site> console`:

   ```python
   from aws_s3_storage.aws_s3_storage import s3_utils

   key = "private/9f3c1a2b4d5e/report.pdf"
   frappe.get_doc({
       "doctype": "File",
       "file_name": "report.pdf",
       "is_private": 1,
       "attached_to_doctype": "Sales Invoice",
       "attached_to_name": "SINV-0001",
       "file_url": s3_utils._build_file_url(key),
       "s3_key": key,
   }).insert(ignore_permissions=True)
   frappe.db.commit()
   ```

   Use `is_private: 1` for a `private/` key and `0` for a `public/` one — the
   prefix and the flag must agree, or the next save will move the object.

#### 8.11 Recommended baseline

1. **Bucket Versioning: Enabled** — before the migration runs.
2. The **`NeverDestroyHistory` deny** (§8.3) on the app's IAM user.
3. A **lifecycle rule**: noncurrent versions 90 days, expired delete markers,
   aborted multipart uploads after 7 days.
4. **Daily Backup Sync** on, with `backups/` transitioned to `GLACIER_IR`.
5. Regulated or high-value data: add **Object Lock (GOVERNANCE, 30 days)** and
   **replication to a second account**.
6. **Do the restore drill once** (§8.10) on a throwaway attachment. A recovery
   path you have never tested is not a backup.
7. Add §9 before you clone the site anywhere: versioning makes a mistaken delete
   *recoverable*, it does not stop a test copy from breaking live links.

---

### 9. Test / staging copies of a live site

> **Read 9.5 first.** The checks in this section run inside the application, and
> the application is not what holds the credentials. **Separate, read-only
> credentials for the test environment are the primary protection**; everything
> else here is a second line that catches the mistakes those credentials would
> not — and that stops the app doing damage in the window before anyone thinks
> to set them up. Do not treat this section as a substitute for 9.5.

#### 9.1 The problem

Restore a production database onto a test site and every `File` record comes
with it — the same S3 keys, the same bucket name, the same credentials in **S3
Settings**. Nothing *inside* the database distinguishes the two sites, so the
test site is not working on a copy of the files. It is working on **the files**.

| What you do on the test copy | What happens to production's files |
| --- | --- |
| Delete an attachment, or a document whose attachments cascade | The shared object is deleted once the test transaction commits |
| Flip a file from public to private (or back) | The object is copied to the other prefix and the old key deleted — production still points at the old one |
| Change the doctype scope, then save a file that is now out of scope | The object is downloaded to the test site's disk and the shared object deleted |
| Let the hourly scheduler run | `S3 Deletion Queue` rows restored *from production* are executed from the test site |
| Run `bench migrate` | Patches move attachments between storages and delete the originals |
| Let the daily scheduler run | The test site starts pushing its own backups into production's bucket |

Worse than any single case: the "is this object still used?" check before a
delete only queries **the database it is running in**. The test site cannot see
that production is still serving the attachment, so it concludes the delete is
safe.

A checkbox cannot fix this, because a checkbox lives in the database and is
copied along with it: the flag that says "this is production" arrives on the test
site saying exactly the same thing.

#### 9.2 How the app tells the two apart

Ownership is a match between two values kept in **different places**:

- `storage_owner_id` in **S3 Settings** — inside the database, so it travels with
  a restore;
- the same id under `aws_s3_storage_owner` in **`site_config.json`** — a file on
  disk. `bench backup` does not put it in the dump and `bench restore` does not
  write it, so it stays behind.

They agree only on the site that claimed the bucket:

| Situation | Database / on disk | State |
| --- | --- | --- |
| Fresh install | none / none | `unclaimed` — **may not modify the bucket** |
| The site that claimed the bucket | `X` / `X` | **owner** |
| Production's database restored onto test | `X` / none | **foreign** |
| Restored over a site that had claimed its own bucket | `X` / `Y` | **foreign** |
| Production restored onto itself (a normal recovery) | `X` / `X` | **owner** |
| The whole **site directory** copied, `site_config.json` included, under a different site name | `X` / `X` | **foreign** — the recorded site name no longer matches |
| The whole **server** copied, keeping the site name | `X` / `X` | **owner, locally** — see 9.3 |

`unclaimed` is refused, not allowed. Absence of proof is not permission: a
database saved before ownership existed carries no owner id, so it lands
unclaimed wherever it is restored, and that is exactly the backup someone is
most likely to experiment with. An unclaimed site serves files from S3, sends
new uploads to local disk, and modifies nothing.

The claim happens automatically in `after_install` / `after_migrate`, and **only
from the `unclaimed` state**. A restored database already carries an owner id, so
running `bench migrate` on the test site can never make it the owner by accident
— it prints the mismatch instead.

> ⚠️ **Upgrade the live site first.** A database from a version that predates
> this field carries no owner id at all, so it is `unclaimed` wherever it lands
> and the first site to run `bench migrate` on this version claims the bucket. Do
> that on production before you clone it anywhere. From then on, every copy of
> its database is detected.

#### 9.3 The copy that keeps the site name: the lease in the bucket

Everything in 9.2 compares values that are **on this server**. Copy the whole
server and keep the site name, and all three agree on the copy, because all
three were copied. Two environments then both believe they are the owner, and
neither can see the other — nothing they compare lives anywhere they share.

The bucket is the thing they share. `.aws_s3_storage/owner.json` names the
server that currently holds ownership. Before anything **destructive** —
deleting an object, replacing one (a privacy move, a regenerated thumbnail),
moving a file out of S3, running the deletion queue — the app checks that the
lease still names it:

| Lease says | Result |
| --- | --- |
| This server | Proceed |
| Any other server, **at any age** | **Conflict.** Stand down, and say so in red on the S3 Settings form |
| A different owner id entirely | **Conflict.** Two environments are pointed at one bucket |
| Missing, and this site is the recorded owner | Create it, conditionally (`If-None-Match: *`) so a simultaneous create elsewhere cannot also succeed |
| Unreadable (no access, S3 down) | **Unverified.** Destructive work waits; uploads carry on, because a failed delete is queued and nothing is lost, while a blocked upload would silently scatter files onto local disk |

##### Ownership never moves on its own

There is deliberately **no timeout** by which one server takes the lease from
another. The reason is worth stating, because a timeout looks obviously
sensible and is the single most dangerous thing that could be added here:

> The heartbeat is written when the lease is checked, and the lease is only
> checked before a destructive operation. A perfectly healthy production site
> that has simply not deleted anything for a while therefore has an old
> heartbeat. A timeout would read that as "production is gone" and hand the
> lease to whoever asked next — which, in the scenario this whole section
> exists for, is the test copy asking.

An old heartbeat means *"nobody has needed to delete anything"*, never *"that
server is gone"*. Only a person can tell the difference, so only a person moves
the lease: **Take Ownership of This Storage**. The age is shown so they can
judge it, and an hourly scheduler job re-stamps the owner's heartbeat so "last
seen" reflects the site being alive rather than the last time it happened to
delete something.

The cost is real and deliberate: a server whose identity legitimately changes —
a rename, a rebuilt container — stops deleting until someone presses the
button. A blocked delete is recoverable; a silent takeover is not. Deployments
where the identity changes routinely should pin it with
`AWS_S3_STORAGE_INSTANCE` (below) so it never changes in the first place.

##### Cost and timing

The lease is read at most once every **60 seconds** per server, and only on the
destructive path, so uploads never pay for it. That interval is also the bound
on the other direction: **a server that has just lost ownership can keep acting
on its previous answer for up to a minute.** Taking ownership stops the old
server within that window, not instantly.

Writes are conditional — `If-None-Match: *` to create, `If-Match: <etag>` to
replace — so two servers racing cannot both believe they won, and a heartbeat
never overwrites a takeover that happened since it read. An S3-compatible
endpoint that does not support conditional writes cannot give a safe answer, so
it does not get one: the state stays *unverified* and destructive work waits.
The one exception is **Take Ownership**, which falls back to a plain write and
logs that it did, because there a person has explicitly decided.

The lease needs `s3:GetObject` and `s3:PutObject` on `.aws_s3_storage/*`, which
the bucket-wide policy in §1.2 already grants.

**This is also what makes "take ownership" mean something on a second server.**
Claiming writes the lease, so the other server discovers on its next check that
it is no longer the holder and stands down. Without it, taking ownership on the
new server would leave the old one modifying the bucket, because nothing it can
see would have changed.

##### The one case nothing in the app can catch

A **bit-identical** copy — same machine id, same hostname, same site name, same
`site_config.json` — is indistinguishable from the original by construction: the
server identity is derived from the machine, and on such a copy nothing about
the machine differs. The lease sees one identity and lets both through.

If that is in your threat model (restoring a VM snapshot onto a second VM
without re-provisioning it, a block-level disk clone), set the identity from
outside the copied filesystem:

```ini
# systemd unit, container spec, orchestrator — wherever the deployment is
# defined, NOT in the site directory or the repository.
Environment=AWS_S3_STORAGE_INSTANCE=prod-blue
```

Set it to a different value on the copy (or simply leave it unset there and set
it on production) and the lease separates them. Without it, the two servers are
the same server as far as any software on them can tell.

#### 9.4 What a foreign environment can and cannot do

| Operation | On the owner | On a restored copy |
| --- | --- | --- |
| Read / download an inherited attachment | Yes | Yes (see 9.5 — restrict this with IAM, not with app settings) |
| Upload a new file | Goes to S3 | Goes to **the test site's own local disk** |
| Delete an attachment inherited from production | Normal delete policy (§8) | The `File` record goes, **the object stays** |
| Change a file's privacy, or move it between storages | Normal | Refused — the record keeps pointing at the object where it is |
| Generate a thumbnail for an inherited file | Normal | Refused — no object is written |
| Generate a thumbnail for an inherited file | Normal | Refused — the thumbnail key is derived from the file's own key, so writing it would replace the owning environment's object |
| Execute a deletion request inherited from a backup | Retried normally | Parked as `Blocked` in **S3 Deletion Queue** |
| Run the migration / scope moves / patches | Normal | Refused, with the reason recorded |
| Daily backup sync | Normal | Refused |

Nothing is refused silently: every refusal is logged, and an interactive action
shows *S3 storage was not modified: …* with the reason.

Upload behaviour is the part that keeps the test site usable. A new upload on a
foreign site goes to its own disk, and an upload that Frappe deduplicates onto an
*inherited* object is written out as a real, separate local copy — so editing a
file on the test copy never edits production's.

#### 9.5 The layer the app cannot enforce: credentials

The checks above live in the application. They stop *this app* from modifying the
bucket — they cannot stop a `bench execute`, another app, or a shell with the same
credentials. Treat them as the second line, not the first:

**Give the test environment its own credentials, read-only on the production
bucket.** Set `access_key_id` / `secret_access_key` in the test site's **S3
Settings** to an IAM user whose policy allows only `s3:GetObject` (plus
`s3:ListBucket` if you need it) and denies `s3:PutObject` and `s3:DeleteObject`.
A mistake then fails at AWS, not at a Python `if`.

```json
{
  "Sid": "TestCopyIsReadOnly",
  "Effect": "Deny",
  "Action": ["s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion"],
  "Resource": "arn:aws:s3:::PRODUCTION_BUCKET/*"
}
```

> There is no `s3:CopyObject` action — a copy is authorised as a read on the
> source plus a **write on the destination**, so denying `s3:PutObject` on the
> production bucket is what prevents copying *into* it. See the
> [CopyObject API reference](https://docs.aws.amazon.com/AmazonS3/latest/API/API_CopyObject.html).

Note that read-only credentials also stop this site from writing the ownership
lease (9.3), so its state shows as *unverified* — correct, and harmless: an
environment that cannot write to the bucket has nothing to hold back.

#### 9.6 Checklist: restoring production onto a test site

1. **Restore the database.** Do *not* copy `site_config.json` across — that file
   is what identifies the environment.
2. **Open S3 Settings.** The banner should read *"This site does not own the
   configured storage."* If it does not, stop: the two sites are not being told
   apart, and nothing below will help.
3. **Swap in read-only credentials** for the production bucket (9.5), or clear
   the bucket name entirely if the test site does not need to see the files.
4. **Park inherited deletion requests** — the *Park N Inherited Deletion(s)*
   button, or `bench execute aws_s3_storage.aws_s3_storage.environment.block_inherited_deletions`.
   They are requests production made, against a database this site cannot vouch
   for.
5. Optionally turn on **Read-Only Mode** as a belt-and-braces switch. Unlike
   *Enable S3 Storage*, this one really does stop every modification.
6. If the test site should have real S3 storage of its own: point **Bucket Name**
   at a *different* bucket, then use **Take Ownership of This Storage**. It
   becomes a normal owner — of that bucket.

#### 9.7 Taking ownership deliberately

**Take Ownership of This Storage** (S3 Settings → *Storage Ownership*) makes this
site the owner of the bucket it is currently pointed at. Use it when:

- you moved production to a new server and the new site must own the bucket;
- you gave a test/staging site its own bucket (point it there **first**);
- the state says `unclaimed` and you want it recorded now.

Do **not** use it to "make the warning go away" on a copy that shares a bucket
with a live site — that is precisely the mistake this exists to prevent.

The dialog offers *Also tag existing S3 files as owned by this site*. It stamps
every S3-backed `File` that has no recorded owner (everything uploaded before
this field existed) with this environment's id, so that a site which later adopts
the same bucket is still kept away from them. Tick it on the site that genuinely
owns the bucket; leave it off if the bucket holds another environment's files.

#### 9.8 A queued deletion is a request, not a fact

Each **S3 Deletion Queue** row records the environment that asked for it
(`Requested By Environment`) and the environment that owned the object
(`Object Owner`). Both are re-checked when the row is executed, not only when it
is written, because a row outlives the request that created it and is restored
along with the database.

So a row queued by production is never executed by any other environment — not
even by one that has deliberately taken ownership of the bucket. Taking
ownership makes this site responsible for the bucket from now on; it does not
make production's past decisions, taken against a database this site cannot
vouch for, this site's to carry out. Those rows show as `Blocked` with the
reason, and can be reviewed.

A row with **no** environment recorded is parked too, not run. Rows written
before this column existed cannot be told apart from rows a restored copy
brought with it, and "we do not know who asked for this deletion" is not
permission to carry it out. On the site that really owns the bucket, **Adopt N
Unattributed Deletion(s)** attributes them to it and puts them back in the
queue — a person saying "yes, these are mine", on the only machine where the
answer is known.

#### 9.9 Two related cases

- **Restoring an old backup onto production.** The database goes back in time;
  the bucket does not. Records return that point at objects deleted since, and
  objects uploaded after the backup date are left with no record. Ownership is
  unaffected (the id is in the dump and on disk, and they still match), so the
  app keeps working — but audit before you trust the links: `bench execute
  aws_s3_storage.aws_s3_storage.migrate.audit_local_links` for local links, and
  the `S3 Deletion Queue` for requests the restore brought back.
- **Running the old and new server at once during a move.** Both point at the
  same bucket, and only one of them can be the owner — the other one's
  scheduled jobs stand down instead of racing it. Claim the bucket on the new
  server only once the old one is out of service.

#### 9.10 What is still on your side

The checks above cover this app's own code paths. They do not cover:

- **anything that is not this app.** Another app, a `bench execute`, a shell, a
  script — all of them hold the same credentials and none of them go through
  any of this. 9.5 is the only answer to that, and it is why 9.5 is the primary
  protection and this section is the second line;
- a **bit-identical** copy of a server, unless `AWS_S3_STORAGE_INSTANCE` is set
  per deployment (9.3);
- the first **60 seconds** after ownership moves, during which the previous
  holder may still act on its cached answer (9.3);
- links embedded in rich text, HTML fields and Print Formats (see §6, *Audit
  Local Links*): those are not `File` records and are not repointed;
- a bucket or endpoint change: files do not record which storage they came from,
  so pointing the site at a different bucket changes where every existing key is
  read from, without moving anything.

---

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/aws_s3_storage
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

### License

mit
