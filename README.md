### Aws S3 Storage

AWS S3 integration

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch develop
bench install-app aws_s3_storage
```

### Configuration

Open **S3 Settings** (single doctype) and fill in:

| Field | Purpose |
| --- | --- |
| Bucket Name / Region / Access Key ID / Secret Access Key | Required AWS credentials and target bucket. |
| Endpoint URL | Optional. Set for S3-compatible services (MinIO, DigitalOcean Spaces, Wasabi); path-style addressing is used automatically. Leave blank for AWS S3. |
| Storage Class | S3 tier for uploaded objects (default `STANDARD`). |
| Presigned URL Expiry (seconds) | Lifetime of generated download links, clamped to 60–604800. |
| Verify Upload Integrity | Sends a Content-MD5 header so S3 rejects corrupted uploads (recommended on). |
| Enable Daily Backup Sync | Off by default. When on, the daily scheduler uploads site backups to `backups/<site>/`. |

Use the **Test Connection** button to confirm the credentials can reach the bucket.

#### How files are served

The bucket is kept **fully private** — no public ACLs are set. Every file is served
through a short-lived presigned URL, and private files additionally require read
permission on the corresponding File document. Objects are only deleted from S3
**after** the database transaction that removed the File record has committed, so a
rolled-back delete never leaves a File pointing at a missing object.

#### Required IAM permissions

The configured credentials need the following actions on the bucket and its objects:

```
s3:PutObject
s3:GetObject
s3:DeleteObject
s3:ListBucket      (HeadBucket — used by Test Connection)
```

`s3:GetObject` on `HeadObject` is also used to make backup sync idempotent.

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
