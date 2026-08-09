### Aws S3 Storage

AWS S3 integration for Frappe — stores uploaded files (and, optionally, site
backups) in an S3 bucket instead of on the local disk. The bucket stays fully
private and every file is served through short-lived presigned URLs.

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
| **Enable S3 Storage** | — | Master switch (on by default). When on, new uploads go to S3. When off — or before a bucket is configured — uploads fall back to Frappe's local storage instead of failing. Existing S3 files keep being served and deleted correctly either way. |
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

#### Guest Access

| Field | Default | Description |
| --- | --- | --- |
| **Allow Guests to Download Their Own Uploads** | Off | Lets an anonymous visitor open a **private** object when the File record is owned by `Guest` — the Web Form attachment case (see §4). Off by default. |
| **Guest Readable Doctypes** | empty | Optional allowlist, one doctype per line. Restricts the rule above to guest uploads attached to those doctypes. Empty means any guest upload. |

#### Test Connection

After saving, click **Test Connection**. It runs a `HeadBucket` call and reports
whether the credentials, region, and bucket name are correct.

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
delete commits, only when no other File still references it (deduplicated uploads
share one object), and a delete that fails is queued in **S3 Deletion Queue** and
retried hourly so nothing is orphaned. Objects uploaded inside a transaction that
rolls back are cleaned up automatically.

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
