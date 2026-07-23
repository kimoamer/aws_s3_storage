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

### 5. Migrating existing local files

Files that were uploaded **before** installing the app stay on local disk. To move
them into S3 and reclaim server space:

- **UI:** *S3 Settings → Migrate Local Files* (runs in the background).
- **Console:**

  ```bash
  bench --site <site> execute aws_s3_storage.aws_s3_storage.migrate.run_migration
  ```

The migration works in batches and is **resumable** — each migrated file records
its S3 key and drops out of the pending set, so it is safe to stop and restart.
Every file is verified in S3 (size check) before its local copy is removed. Files
whose content is missing on disk are skipped for the rest of the run rather than
retried in a loop, and a local file shared by several File records is kept until
the last of them has been migrated.

**Recommended production rollout.** Do the first pass **without** deleting local
files, verify, then reclaim space:

1. Upload and rewrite records, keeping local copies:

   ```bash
   bench --site <site> execute \
       aws_s3_storage.aws_s3_storage.migrate.run_migration \
       --kwargs '{"batch_size": 100, "delete_local": 0}'
   ```

2. Spot-check that new uploads, downloads and deletes work for both a **public**
   and a **private** file, and that uploading the same file twice behaves.
3. Compare object counts/sizes between the local `files` folders and the bucket's
   `public/` and `private/` prefixes.
4. Only then re-run with `"delete_local": 1` (or use the UI button) to remove the
   verified local copies.

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
