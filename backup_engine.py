"""Core backup logic: scan -> hash -> skip unchanged -> upload -> verify -> log.
Also verify_all(): re-checks every stored backup (finds missing / corrupted objects).
"""
import hashlib
import threading
import time

import config
from database import get_conn, now
from audit import record as audit_record
from storage import get_storage

_lock = threading.Lock()  # only one backup at a time

RETRYABLE = {"TIMEOUT", "NETWORK", "CHECKSUM_MISMATCH", "UNKNOWN"}


class ChecksumMismatch(Exception):
    pass


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def classify_error(exc):
    """Turn any exception into a short error type we can count and analyse."""
    if isinstance(exc, ChecksumMismatch):
        return "CHECKSUM_MISMATCH"
    if isinstance(exc, FileNotFoundError):
        return "FILE_NOT_FOUND"
    if isinstance(exc, PermissionError):
        return "LOCAL_PERMISSION"
    if isinstance(exc, TimeoutError):
        return "TIMEOUT"
    if isinstance(exc, ConnectionError):
        return "NETWORK"

    name = type(exc).__name__
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    if code in ("AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch") or name == "NoCredentialsError":
        return "ACCESS_DENIED"
    if code in ("NoSuchBucket",):
        return "NO_SUCH_BUCKET"
    if "Timeout" in name:
        return "TIMEOUT"
    if "EndpointConnection" in name or "ConnectionClosed" in name:
        return "NETWORK"
    return "UNKNOWN"


def _is_missing(exc):
    """True when the cloud object simply does not exist."""
    if isinstance(exc, FileNotFoundError):
        return True
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    return code in ("404", "NoSuchKey", "NoSuchVersion", "NotFound")


def _scan(source):
    """Yield (absolute_path, key) for every file under the source folder."""
    for p in sorted(source.rglob("*")):
        if p.is_file():
            yield p, p.relative_to(source).as_posix()


def _upload_with_retry(storage, path, key, digest):
    """Upload + verify. Retries temporary errors with exponential backoff."""
    last_exc, attempts = None, 0
    for attempt in range(1, config.MAX_RETRIES + 1):
        attempts = attempt
        try:
            version_id = storage.upload(path, key, digest)
            remote = storage.get_checksum(key, version_id)
            if remote != digest:
                raise ChecksumMismatch("Cloud copy checksum does not match the original file")
            return True, attempts, version_id, None
        except Exception as exc:  # noqa: BLE001 - we classify every error
            last_exc = exc
            if classify_error(exc) not in RETRYABLE or attempt == config.MAX_RETRIES:
                break
            time.sleep(config.BACKOFF_SECONDS * (2 ** (attempt - 1)))
    return False, attempts, None, last_exc


def run_backup(trigger="manual", only_keys=None):
    """Run one backup job. Returns job id, or None if one is already running."""
    if not _lock.acquire(blocking=False):
        return None
    try:
        storage = get_storage()
        conn = get_conn()
        job_id = conn.execute(
            "INSERT INTO jobs (started, trigger, status) VALUES (?, ?, 'RUNNING')",
            (now(), trigger),
        ).lastrowid
        conn.commit()

        uploaded = skipped = failed = 0
        config.SOURCE_DIR.mkdir(parents=True, exist_ok=True)

        for path, key in _scan(config.SOURCE_DIR):
            if only_keys is not None and key not in only_keys:
                continue

            t0 = time.time()
            error, digest, size = None, None, 0
            try:
                size = path.stat().st_size
                digest = sha256_file(path)
            except Exception as exc:  # noqa: BLE001
                error = exc

            if error is None:
                row = conn.execute("SELECT last_hash FROM files WHERE path=?", (key,)).fetchone()
                if row and row["last_hash"] == digest:
                    skipped += 1  # unchanged -> incremental backup skips it
                    continue
                ok, attempts, version_id, error = _upload_with_retry(storage, path, key, digest)
            else:
                ok, attempts, version_id = False, 1, None

            ms = int((time.time() - t0) * 1000)
            if ok:
                uploaded += 1
                conn.execute(
                    "INSERT INTO files (path,last_hash,last_backup) VALUES (?,?,?) "
                    "ON CONFLICT(path) DO UPDATE SET last_hash=excluded.last_hash, last_backup=excluded.last_backup",
                    (key, digest, now()),
                )
                conn.execute(
                    "INSERT INTO job_files (job_id,path,status,attempts,verified,checksum,version_id,size_bytes,duration_ms,created) "
                    "VALUES (?,?,'SUCCESS',?,1,?,?,?,?,?)",
                    (job_id, key, attempts, digest, version_id, size, ms, now()),
                )
            else:
                failed += 1
                conn.execute(
                    "INSERT INTO job_files (job_id,path,status,attempts,verified,error_type,error_message,size_bytes,duration_ms,created) "
                    "VALUES (?,?,'FAILED',?,0,?,?,?,?,?)",
                    (job_id, key, attempts, classify_error(error), str(error)[:500], size, ms, now()),
                )
            conn.commit()

        if failed == 0:
            status = "SUCCESS"
        elif uploaded == 0:
            status = "FAILED"
        else:
            status = "PARTIAL"
        conn.execute(
            "UPDATE jobs SET finished=?, status=?, uploaded=?, skipped=?, failed=? WHERE id=?",
            (now(), status, uploaded, skipped, failed, job_id),
        )
        conn.commit()
        if failed:
            severity = "critical" if status == "FAILED" else "warning"
            conn.execute(
                "INSERT INTO alerts (severity,title,message,job_id,created) VALUES (?,?,?,?,?)",
                (severity, f"Backup job #{job_id} {status.lower()}",
                 f"{failed} file(s) failed; {uploaded} uploaded and {skipped} unchanged file(s) skipped. Open job #{job_id} to inspect errors.", job_id, now()),
            )
        elif uploaded:
            conn.execute(
                "INSERT INTO alerts (severity,title,message,job_id,created) VALUES (?,?,?,?,?)",
                ("success", f"Backup job #{job_id} completed", f"{uploaded} file(s) uploaded and checksum-verified successfully.", job_id, now()),
            )
        conn.commit()
        conn.close()
        audit_record("BACKUP_JOB", f"job={job_id}", outcome=status,
                     details=f"trigger={trigger}; uploaded={uploaded}; skipped={skipped}; failed={failed}")
        return job_id
    finally:
        _lock.release()


def verify_all():
    """Re-check every backed-up file against the cloud. Returns (run_id, counts).

    OK       cloud copy matches the checksum we recorded
    MISMATCH cloud copy exists but its content is different
    MISSING  cloud object is gone
    ERROR    could not check (network, permissions...)
    """
    storage = get_storage()
    conn = get_conn()
    run_id = conn.execute("SELECT COALESCE(MAX(run_id),0)+1 FROM verifications").fetchone()[0]
    rows = conn.execute(
        """SELECT f.path, f.last_hash,
                  (SELECT j.version_id FROM job_files j
                    WHERE j.path=f.path AND j.status='SUCCESS' AND j.checksum=f.last_hash
                    ORDER BY j.id DESC LIMIT 1) AS version_id
           FROM files f ORDER BY f.path"""
    ).fetchall()

    counts = {"OK": 0, "MISMATCH": 0, "MISSING": 0, "ERROR": 0}
    for r in rows:
        observed = None
        if not r["version_id"]:
            status = "MISSING"
        else:
            try:
                observed = storage.get_checksum(r["path"], r["version_id"])
                status = "OK" if observed == r["last_hash"] else "MISMATCH"
            except Exception as exc:  # noqa: BLE001
                if _is_missing(exc):
                    status = "MISSING"
                else:
                    status, observed = "ERROR", str(exc)[:150]
        counts[status] += 1
        conn.execute(
            "INSERT INTO verifications (run_id,run_at,path,expected,observed,status) VALUES (?,?,?,?,?,?)",
            (run_id, now(), r["path"], r["last_hash"], observed, status),
        )
    conn.commit()
    conn.close()
    return run_id, counts
