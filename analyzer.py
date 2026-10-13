"""Failure-pattern detection (plain SQL) + troubleshooting advice.

Rule-based advice always works. If ANTHROPIC_API_KEY is set, the app can also
ask an AI model for a plain-language explanation. Only the error TYPE, counts
and a sanitised message are sent -- never file paths or credentials.
"""
import json
import re
import urllib.request
from datetime import datetime, timedelta, timezone

import config
from database import get_conn, now

ADVICE = {
    "TIMEOUT": {
        "title": "Connection timeouts",
        "meaning": "The upload started but the cloud did not answer in time.",
        "steps": [
            "Check the internet connection and run a speed test.",
            "Schedule backups at off-peak hours or when the network is stable.",
            "Increase MAX_RETRIES and BACKOFF_SECONDS in .env.",
            "If you are on a VPN or proxy, test the backup without it.",
        ],
    },
    "NETWORK": {
        "title": "Network errors",
        "meaning": "The computer could not reach the cloud service.",
        "steps": [
            "Confirm the device is online and DNS works (try opening aws.amazon.com).",
            "Check firewall/antivirus rules that may block outgoing HTTPS (port 443).",
            "Verify AWS_REGION matches the bucket's region.",
        ],
    },
    "ACCESS_DENIED": {
        "title": "Access denied / bad credentials",
        "meaning": "AWS refused the request: the keys are missing, wrong, or lack permission.",
        "steps": [
            "Re-run `aws configure` or check AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY.",
            "Give the IAM user s3:PutObject, s3:GetObject, s3:ListBucket and s3:GetObjectVersion.",
            "Check the bucket policy does not block this user.",
        ],
    },
    "NO_SUCH_BUCKET": {
        "title": "Bucket not found",
        "meaning": "The S3 bucket name in the settings does not exist in this region.",
        "steps": [
            "Check S3_BUCKET for typos (bucket names are global and case-sensitive).",
            "Confirm AWS_REGION matches where the bucket was created.",
            "Create the bucket and enable versioning if it was deleted.",
        ],
    },
    "FILE_NOT_FOUND": {
        "title": "Files disappearing during backup",
        "meaning": "A file was listed but deleted or moved before it could be read.",
        "steps": [
            "Avoid backing up temporary or cache folders.",
            "Make sure no other program is deleting files while a backup runs.",
        ],
    },
    "LOCAL_PERMISSION": {
        "title": "Local file permission problems",
        "meaning": "The app is not allowed to read some files in the source folder.",
        "steps": [
            "Close programs that lock the file (editors, databases).",
            "Run the app as a user that can read the folder.",
            "Exclude system or protected folders from the source directory.",
        ],
    },
    "CHECKSUM_MISMATCH": {
        "title": "Verification failures",
        "meaning": "A file reached the cloud but its content differs from the original.",
        "steps": [
            "This often follows an unstable connection: retry the backup.",
            "Check the file is not being modified while it uploads.",
            "If it keeps happening, test the disk and network hardware.",
        ],
    },
    "UNKNOWN": {
        "title": "Unclassified errors",
        "meaning": "The error did not match a known category.",
        "steps": [
            "Open the failed job and read the full error message.",
            "Search the message text or share it with your administrator.",
        ],
    },
}


def detect_patterns():
    """Return error types that failed >= PATTERN_THRESHOLD times in the window."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=config.PATTERN_WINDOW_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT error_type, COUNT(*) AS occurrences,
                      COUNT(DISTINCT job_id) AS jobs, MAX(created) AS last_seen
               FROM job_files
               WHERE status='FAILED' AND created >= ?
               GROUP BY error_type
               HAVING COUNT(*) >= ?
               ORDER BY occurrences DESC""",
            (cutoff, config.PATTERN_THRESHOLD),
        ).fetchall()
    patterns = []
    for r in rows:
        info = ADVICE.get(r["error_type"], ADVICE["UNKNOWN"])
        patterns.append({**dict(r), "title": info["title"], "meaning": info["meaning"], "steps": info["steps"]})
    return patterns


def sanitize(text):
    """Remove file paths and keys before anything leaves this machine."""
    text = re.sub(r"([A-Za-z]:)?[\\/][^\s'\"]+", "<path>", text or "")
    text = re.sub(r"AKIA[0-9A-Z]{16}", "<access-key>", text)
    return text[:200]


def ask_ai(error_type):
    """Ask Claude for a plain-language explanation. Returns text or None."""
    if not config.ANTHROPIC_API_KEY:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) c, MAX(error_message) m FROM job_files WHERE status='FAILED' AND error_type=?",
            (error_type,),
        ).fetchone()
    prompt = (
        "You are a cloud backup troubleshooting assistant. A backup tool keeps failing.\n"
        f"Error type: {error_type}\nTimes failed: {row['c']}\n"
        f"Sample error message: {sanitize(row['m'])}\n"
        f"Storage: {config.STORAGE_BACKEND}\n\n"
        "In under 150 words, explain the likely cause in simple language and give 3 numbered fixes."
    )
    body = json.dumps({
        "model": config.ANTHROPIC_MODEL,
        "max_tokens": 500,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": config.ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
        text = "".join(b.get("text", "") for b in data.get("content", []))
        if text:
            with get_conn() as conn:
                conn.execute("INSERT INTO ai_analysis (error_type,summary,created) VALUES (?,?,?)",
                             (error_type, text, now()))
        return text
    except Exception as exc:  # noqa: BLE001
        return f"AI service unavailable ({type(exc).__name__}). Showing rule-based advice only."


def analysis_history(error_type, limit=3):
    """Previously generated AI explanations for this error type."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT summary, created FROM ai_analysis WHERE error_type=? ORDER BY id DESC LIMIT ?",
            (error_type, limit),
        ).fetchall()
