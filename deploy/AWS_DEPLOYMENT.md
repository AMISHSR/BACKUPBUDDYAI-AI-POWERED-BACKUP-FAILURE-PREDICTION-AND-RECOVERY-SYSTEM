# BackupBuddyAI: deployment options

## Recommended for this project: one EC2 instance + Amazon S3

This is the simplest practical cloud-hosted setup for the current code. The Flask website runs on an EC2 Linux instance, the SQLite metadata database lives on that instance's EBS-backed disk, and backup file versions are stored in an S3 bucket. The included systemd and Nginx examples are under `deploy/`.

**Important:** the application backs up files in `SOURCE_DIR` on the machine where it runs. Hosting the application does not automatically give it access to files on your personal laptop. This demo backs up `sample_source` on EC2. Backing up a laptop folder needs a separate sync/agent/upload mechanism.

### A. Create the S3 bucket

1. In the AWS Console, open S3 and create a private bucket in your preferred Region (Mumbai is `ap-south-1`). Use a globally unique name.
2. Keep Block Public Access enabled and leave default encryption enabled.
3. In **Properties → Bucket Versioning**, enable versioning. Wait approximately 15 minutes after enabling it for first-time propagation before the first uploads.
4. Keep the bucket private; do not serve backup objects publicly.

### B. Create a least-privilege EC2 IAM role

1. In IAM, create a policy using `deploy/aws/s3-iam-policy.example.json` after replacing `REPLACE_WITH_BUCKET_NAME` in both resource ARNs.
2. Create an IAM role trusted by the EC2 service (`ec2.amazonaws.com`) and attach this policy.
3. Attach the role to the EC2 instance during launch or later in **EC2 → Instances → Actions → Security → Modify IAM role**.
4. The app role can list bucket versions and read/write objects but cannot delete objects. If you configure a customer-managed KMS key, grant the required KMS permissions separately.
5. Do not put permanent AWS keys in the app source or `.env` on EC2. Boto3 will obtain short-lived role credentials automatically.

### C. Launch EC2

1. Launch an Ubuntu Server LTS instance. A small general-purpose instance is normally enough for the sample project; model training and large files require more memory/CPU.
2. Attach the IAM role from Step B. Use an encrypted EBS root volume and keep it attached/persistent.
3. Security group: allow SSH (22) only from **My IP**; allow HTTP (80) and HTTPS (443) for web access; do **not** open port 5000 to the public Internet.
4. For a stable public URL, allocate an Elastic IP or use a domain A record pointing to the instance. A domain is needed for the typical Let's Encrypt HTTPS workflow.

### D. Copy and install the project

From PowerShell on your computer, upload the final ZIP to the instance (replace paths, IP, and key file):

```powershell
scp -i .\your-key.pem .\BackupBuddyAI_Final.zip ubuntu@YOUR_EC2_PUBLIC_IP:/tmp/BackupBuddyAI_Final.zip
```

SSH into EC2, then run:

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip unzip nginx certbot python3-certbot-nginx
sudo mkdir -p /opt/backupbuddyai /var/lib/backupbuddyai /etc/backupbuddyai
sudo unzip -q /tmp/BackupBuddyAI_Final.zip -d /tmp/bb-extract
sudo cp -a /tmp/bb-extract/BackupBuddyAI/. /opt/backupbuddyai/
sudo chown -R ubuntu:ubuntu /opt/backupbuddyai /var/lib/backupbuddyai
python3 -m venv /opt/backupbuddyai/venv
/opt/backupbuddyai/venv/bin/pip install --upgrade pip
/opt/backupbuddyai/venv/bin/pip install -r /opt/backupbuddyai/requirements.txt
```

### E. Configure the service secrets and paths

Generate strong random values on EC2:

```bash
openssl rand -hex 32
openssl rand -hex 20
```

Use the first output for `SECRET_KEY` and the second for `ADMIN_PASSWORD` (or create your own stronger password). Do not commit these values to GitHub or paste them into public chats.

Create the service environment file:

```bash
sudo nano /etc/backupbuddyai/backupbuddy.env
```

Enter values similar to these, replacing the bucket, region, and secret values:

```dotenv
APP_ENV=production
AUTH_ENABLED=true
ADMIN_USERNAME=your-admin-name
ADMIN_PASSWORD=REPLACE_WITH_LONG_RANDOM_PASSWORD
SECRET_KEY=REPLACE_WITH_RANDOM_HEX_SECRET
COOKIE_SECURE=true
SESSION_MINUTES=60

STORAGE_BACKEND=s3
S3_BUCKET=REPLACE_WITH_YOUR_BUCKET
AWS_REGION=ap-south-1

SOURCE_DIR=/opt/backupbuddyai/sample_source
DB_PATH=/var/lib/backupbuddyai/backupbuddy.db
RESTORE_DIR=/var/lib/backupbuddyai/restored
LOCAL_CLOUD_DIR=/var/lib/backupbuddyai/local_cloud
BACKUP_INTERVAL_MINUTES=0
MAX_RETRIES=3
BACKOFF_SECONDS=1
SIMULATE_FAILURE_RATE=0
```

Do **not** add `AWS_ACCESS_KEY_ID` or `AWS_SECRET_ACCESS_KEY` here when using the EC2 IAM role. Restrict the environment file:

```bash
sudo chown root:root /etc/backupbuddyai/backupbuddy.env
sudo chmod 600 /etc/backupbuddyai/backupbuddy.env
```

The app refuses to start in `APP_ENV=production` unless login credentials, a strong secret key, and secure cookies are configured. `COOKIE_SECURE=true` means you should finish HTTPS setup before signing in.

### F. Configure HTTPS with Nginx and Certbot

First, point your DNS A record at the instance's public/Elastic IP and wait until it resolves. Then:

```bash
sudo cp /opt/backupbuddyai/deploy/nginx/backupbuddyai.conf.example /etc/nginx/sites-available/backupbuddyai
sudo nano /etc/nginx/sites-available/backupbuddyai
```

Replace `backupbuddy.example.com` with your real domain. Enable and validate the site:

```bash
sudo ln -s /etc/nginx/sites-available/backupbuddyai /etc/nginx/sites-enabled/backupbuddyai
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx -d YOUR_REAL_DOMAIN
```

Certbot should configure HTTPS for the domain. Confirm the site works over `https://YOUR_REAL_DOMAIN` before signing in. Keep ports 80 and 443 open so HTTP validation and HTTPS access can work.

### G. Start BackupBuddyAI as a service

```bash
sudo cp /opt/backupbuddyai/deploy/systemd/backupbuddyai.service.example /etc/systemd/system/backupbuddyai.service
sudo systemctl daemon-reload
sudo systemctl enable --now backupbuddyai
sudo systemctl status backupbuddyai --no-pager
```

If startup fails, inspect logs:

```bash
sudo journalctl -u backupbuddyai -n 100 --no-pager
```

Open `https://YOUR_REAL_DOMAIN`, sign in, run a backup, and verify that objects exist in S3. `DB_PATH` is outside the application code directory so it is retained across code updates/redeployments as long as the EBS volume remains. Back up/snapshot the EC2 disk too; S3 backup objects alone do not preserve the app's local job history database.

### H. Cloud validation checklist

- `https://YOUR_REAL_DOMAIN/health` responds with `{"status":"ok"}`.
- The login page appears before other application routes.
- An ordinary backup creates objects in the S3 bucket.
- Updating a sample file and backing up again creates a new S3 object version.
- The integrity page checks the stored object checksum.
- A restore produces the selected version under `/var/lib/backupbuddyai/restored/`.
- IAM permissions do not allow the app to delete object versions.

## Other hosting options

### Option 2: AWS Elastic Beanstalk

Elastic Beanstalk can deploy Flask/Python apps with a managed environment and deployment workflow. It is a good next step after adapting persistence. The current app uses SQLite and writes restore files locally, so do not assume the local database directory is durable across instance replacement or deployment. Before using multiple instances, move metadata to PostgreSQL (for example Amazon RDS), send backup objects to S3, and run scheduled backup work in a separate single scheduler/worker. Keep authentication enabled and configure environment variables in the environment configuration.

AWS guide: https://docs.aws.amazon.com/elasticbeanstalk/latest/dg/create-deploy-python-flask.html

### Option 3: Container hosting (App Runner / ECS / Fargate)

The included Dockerfile is useful for a container demo. Container filesystems are not a reliable persistent database store. For an internet-facing or multi-instance container deployment, use S3 for objects, PostgreSQL/RDS for metadata, a managed secrets service, HTTPS, and a separate scheduled worker. Mount persistent storage for any restored files that must survive container replacement.

## Cost and cleanup

AWS charges can apply to EC2, EBS, public IPv4/Elastic IP, S3 storage and requests, data transfer, and domain/HTTPS-related services. Check the current regional prices and your Free Tier/plan eligibility before launch. Stop or terminate the EC2 instance when not needed, and delete test objects/buckets and other resources only after confirming you no longer need them.
