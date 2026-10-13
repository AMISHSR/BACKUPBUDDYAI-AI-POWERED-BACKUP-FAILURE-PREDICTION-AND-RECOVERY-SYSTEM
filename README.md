# BackupBuddy AI

Intelligent Cloud Backup Monitoring and Recovery Assistant (Python + Flask + SQLite + Amazon S3).

It backs up a folder to the cloud, skips unchanged files, verifies every upload with a SHA-256 checksum,
retries temporary failures, detects recurring failure patterns, explains them (rules + optional AI),
and restores older versions of files.

## 1. Run it in VS Code (2 minutes, no AWS needed)

1. Extract the zip and open the folder in VS Code (File > Open Folder).
2. Open the terminal (Ctrl + `) and run:

   Windows:
   ```
   python -m venv venv
   venv\Scripts\activate
   pip install -r requirements.txt
   copy .env.example .env
   python app.py
   ```
   Mac / Linux:
   ```
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env
   python3 app.py
   ```
   (or just double-click `run_windows.bat` / run `./run_mac_linux.sh`)
3. Open http://localhost:5000 and select **Back up now**.

By default `STORAGE_BACKEND=local`, so the "cloud" is the `local_cloud` folder. Everything works
the same as with S3.

## 2. Try the features (demo script)

| Step | What to do | What you should see |
|------|-----------|---------------------|
| 1 | **run backup** | Job #1: 8 files uploaded, each verified by checksum |
| 2 | **run backup** again | Job #2: 0 uploaded, 8 skipped (incremental backup) |
| 3 | Edit `sample_source/notes.txt`, run backup | Only 1 file uploaded |
| 4 | **integrity** > run verification | All files OK |
| 5 | Delete one folder inside `local_cloud/`, run verification again | That file shows **MISSING** |
| 6 | Open **files** > notes.txt > restore / download | Old version appears in `restored/` or downloads, and is logged under "recovery log" |
| 7 | Set `SIMULATE_FAILURE_RATE=1` in `.env`, restart, edit a few sample files, run backup | FAIL lines in the live log, retries, a red **threat detection** box |
| 8 | Select **analyze with AI** | Troubleshooting steps (and AI text if a key is set) |
| 9 | Set `SIMULATE_FAILURE_RATE=0`, restart, open the failed job, **retry failed files** | Files upload; retry recovery metric goes up |
| 10 | **export report.csv** | Full per-file log for your report |

## Dashboard metrics

| Metric | Meaning |
|--------|---------|
| files processed | files attempted (uploaded + failed), skipped files not counted |
| successful / failed backups | final result of each attempted file |
| success rate | successful / attempted |
| integrity mismatches | checksum mismatches at upload + mismatched or missing objects in the last verification scan |
| verified | share of files confirmed by the last scan (or by the upload check if no scan yet) |
| retry attempts | extra attempts made after a first failure |
| retry recovery | files that had trouble (retried or failed) and later succeeded |
| avg upload time | mean time per successful file |
| recurring errors | error types that reached the threshold in the time window |
| health score | 60% success rate + 40% verified share, minus 10 per recurring problem |

Upload success and integrity verification are shown separately, because a file can upload and still need checking.

## 3. Use real Amazon S3

1. Create an S3 bucket and **enable Versioning** (needed for restore).
2. Create an IAM user with `s3:PutObject`, `s3:GetObject`, `s3:GetObjectVersion`, `s3:ListBucket`,
   `s3:ListBucketVersions` on that bucket.
3. Run `aws configure` (or put the keys in `.env`).
4. In `.env` set `STORAGE_BACKEND=s3`, `S3_BUCKET=<your bucket>`, `AWS_REGION=<region>`.
5. Restart `python app.py`.

Verification: each file's SHA-256 is sent with the upload (S3 rejects mismatching data) and read back
from S3 with `head_object`, then compared with the local hash.

## 4. Optional AI explanations

Put `ANTHROPIC_API_KEY=...` in `.env`. On the advice page the app sends only the error type, the failure
count and a sanitised message (paths and keys removed). Without a key, the built-in rule-based advice is used.

## 5. Deploy to the cloud

**Recommended for the current code:** one EC2 instance + S3. The website runs on EC2, S3 holds the backup objects/versions, and SQLite job history is stored on the instance's persistent EBS-backed disk. Follow [`deploy/AWS_DEPLOYMENT.md`](deploy/AWS_DEPLOYMENT.md) for a detailed step-by-step guide, including IAM policy, HTTPS, Nginx, and a systemd service.

Other options:

- **Elastic Beanstalk:** convenient managed Flask deployment; first move SQLite metadata to RDS/PostgreSQL before using instance replacement or multiple instances.
- **App Runner / ECS / Fargate:** suitable for a containerized service; use RDS/PostgreSQL for persistent metadata and a separate scheduler/worker. Container-local files are not a persistent backup store.

For a local container test only:

```bash
docker build -t backupbuddyai .
docker volume create backupbuddy-data
docker run --rm -p 127.0.0.1:5000:5000 -v backupbuddy-data:/data --env-file .env backupbuddyai
```

For any public cloud deployment, set `APP_ENV=production`, `AUTH_ENABLED=true`, a private `ADMIN_USERNAME` / strong `ADMIN_PASSWORD`, a random `SECRET_KEY` of at least 32 characters, and `COOKIE_SECURE=true` behind HTTPS. Do not open port 5000 publicly; proxy HTTPS traffic through Nginx or a managed load balancer. Prefer an EC2 IAM role for S3 access rather than permanent access keys.

**Scope reminder:** `SOURCE_DIR` is the folder on the machine running the application. A cloud-hosted app backs up files available on that cloud host; hosting the dashboard does not automatically expose folders from your laptop.

## 6. Project structure

```
app.py            Flask routes, metrics, background scheduler
backup_engine.py  scan, hash, incremental skip, upload, verify, retry, error classification, verify_all()
storage.py        LocalStorage and S3Storage (same interface)
analyzer.py       recurring-failure detection (SQL) + advice + optional AI call (answers saved)
database.py       SQLite: files, jobs, job_files, verifications, ai_analysis, restores
config.py         settings from .env / environment (including production safety checks)
templates/        HTML pages including administrator login; CSRF protected forms
static/style.css   styling
deploy/            AWS deployment guide and IAM/systemd/Nginx examples
sample_source/    files to back up (demo)
```

## 7. Limitations (good points for the viva)

- Files deleted from the source folder are not deleted from the cloud (safer for backups).
- Single-administrator login only (not multi-user RBAC); schedule and source folder are environment settings. Add rate limiting and independent security review before internet-facing production use.
- Files are uploaded in one request (suitable for files up to a few GB).
- SQLite is suitable for the single-instance demo; use PostgreSQL/RDS and a separate scheduler for multi-instance hosting.
- Scheduler is a simple thread; use cron or a managed scheduler for production.

## Four engineering modules

### 1. Data Engineering
SQLite stores file metadata, backup jobs, per-file outcomes, verification results, restores, alerts, and audit events. Use **Export report.csv** to inspect job outcomes and integrity metadata.

### 2. Cloud Engineering
`storage.py` provides a local simulation backend and a real Amazon S3 backend through Boto3. Local mode does not send data to AWS. To use S3, configure `STORAGE_BACKEND=s3`, `S3_BUCKET`, and `AWS_REGION`; prefer an IAM role or AWS CLI profile rather than hard-coding access keys. Enable S3 bucket versioning for version-aware restores. AWS charges may apply.

### 3. AI Engineering
The **AI risk** page estimates file-level failure risk from historical backup outcomes. It trains a Random Forest only when enough labelled history contains both successful and failed outcomes. Otherwise, it explicitly labels results as a rule-based baseline. The displayed holdout accuracy is an estimate and should not be presented as a guarantee.

### 4. Security Engineering
The **Security audit** page records backup, verification, restore, and risk-page access events. SHA-256 integrity verification is also built into the backup path. Before public deployment, add authenticated user accounts, role-based authorization, CSRF protection, HTTPS, and production secret management; the local demo is not a production-hardened multi-user service.

## Install and run

```bash
python -m venv venv
# Windows: venv\\Scripts\\activate
# macOS/Linux: source venv/bin/activate
pip install -r requirements.txt
python app.py
```

Use a copy of `.env.example` as `.env`. Local mode is the safe default for the demo. The first AI risk page may show a rule-based baseline until enough history is collected.
