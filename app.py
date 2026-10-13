"""BackupBuddy AI - Flask web app.  Run with:  python app.py"""
import csv
import io
import json
import hmac
import random
import threading
import time
from datetime import timedelta
from pathlib import Path

from flask import Flask, Response, abort, flash, redirect, render_template, request, send_file, session, url_for
from flask_wtf.csrf import CSRFProtect

import analyzer
import ai_predictor
import audit
import backup_engine
import config
from database import get_conn, init_db, now
from storage import get_storage

app = Flask(__name__)
app.config.update(
    SECRET_KEY=config.SECRET_KEY,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=config.COOKIE_SECURE,
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=config.SESSION_MINUTES),
    WTF_CSRF_TIME_LIMIT=3600,
)
csrf = CSRFProtect(app)
init_db()


@app.before_request
def require_login():
    """Enforce optional single-admin login; production mode requires it."""
    if not config.AUTH_ENABLED:
        return None
    if request.endpoint in {"login", "static", "health"}:
        return None
    if session.get("authenticated") is True:
        session.permanent = True
        return None
    next_url = request.full_path if request.method == "GET" else url_for("dashboard")
    if next_url.endswith("?"):
        next_url = next_url[:-1]
    return redirect(url_for("login", next=next_url))


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if config.APP_ENV == "production":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.route("/health")
def health():
    return {"status": "ok"}, 200


@app.route("/login", methods=["GET", "POST"])
def login():
    if not config.AUTH_ENABLED:
        return redirect(url_for("dashboard"))
    if session.get("authenticated") is True:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        valid_user = hmac.compare_digest(username, config.ADMIN_USERNAME)
        valid_password = hmac.compare_digest(password, config.ADMIN_PASSWORD)
        if valid_user and valid_password:
            session.clear()
            session["authenticated"] = True
            session["username"] = config.ADMIN_USERNAME
            session.permanent = True
            audit.record("LOGIN", config.ADMIN_USERNAME, details="Admin login succeeded")
            next_url = request.args.get("next", "")
            # Only allow relative, same-site redirects; prevent open redirects.
            if next_url.startswith("/") and not next_url.startswith("//") and not next_url.startswith("/\\"):
                return redirect(next_url)
            return redirect(url_for("dashboard"))
        audit.record("LOGIN", username[:100], outcome="FAILED", details="Invalid credentials")
        flash("Invalid username or password.", "bad")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    audit.record("LOGOUT", session.get("username", "admin"), details="Admin logged out")
    session.clear()
    flash("You have been logged out.", "ok")
    return redirect(url_for("login"))


# ---------- background scheduler (simple thread, no extra packages) ----------
def _scheduler_loop():
    while True:
        time.sleep(config.BACKUP_INTERVAL_MINUTES * 60)
        backup_engine.run_backup(trigger="scheduled")


_started = False


def start_scheduler():
    global _started
    if config.BACKUP_INTERVAL_MINUTES > 0 and not _started:
        _started = True
        threading.Thread(target=_scheduler_loop, daemon=True).start()


start_scheduler()


# ---------- helpers ----------
@app.template_filter("fsize")
def fsize(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


@app.context_processor
def inject_globals():
    with get_conn() as conn:
        unread = conn.execute("SELECT COUNT(*) FROM alerts WHERE read_at IS NULL").fetchone()[0]
    return {"backend_label": "aws-s3" if config.STORAGE_BACKEND == "s3" else "local-sandbox",
            "unread_alerts": unread,
            "failure_rate": config.SIMULATE_FAILURE_RATE,
            "auth_enabled": config.AUTH_ENABLED,
            "logged_in_user": session.get("username", "admin")}


def safe_key(key):
    """Reject keys that try to escape the backup/restore folders."""
    parts = key.replace("\\", "/").split("/")
    return bool(key) and not key.startswith("/") and ".." not in parts and ":" not in key


def safe_version_id(version_id):
    """S3 version IDs are opaque identifiers and may contain slashes or plus signs."""
    return bool(version_id) and len(version_id) <= 1024 and not any(ord(ch) < 32 for ch in version_id)


def compute_metrics(conn, pattern_count):
    s = conn.execute(
        """SELECT
             COALESCE(SUM(status='SUCCESS'),0) AS ok,
             COALESCE(SUM(status='FAILED'),0)  AS bad,
             COALESCE(SUM(verified),0)         AS verified,
             COALESCE(SUM(MAX(attempts-1,0)),0) AS retries,
             COALESCE(AVG(CASE WHEN status='SUCCESS' THEN duration_ms END),0) AS avg_ms,
             COALESCE(SUM(error_type='CHECKSUM_MISMATCH'),0) AS upload_mismatch
           FROM job_files"""
    ).fetchone()
    total = s["ok"] + s["bad"]

    # "trouble" = a file that needed a retry or failed; recovered = it later succeeded
    t = conn.execute(
        """SELECT COUNT(*) AS trouble,
                  COALESCE(SUM(CASE WHEN j.status='SUCCESS' THEN 1
                                    WHEN EXISTS (SELECT 1 FROM job_files j2 WHERE j2.path=j.path
                                                  AND j2.id>j.id AND j2.status='SUCCESS') THEN 1
                                    ELSE 0 END),0) AS recovered
           FROM job_files j WHERE j.attempts>1 OR j.status='FAILED'"""
    ).fetchone()

    last_run = conn.execute("SELECT MAX(run_id) FROM verifications").fetchone()[0]
    ver = {"OK": 0, "MISMATCH": 0, "MISSING": 0, "ERROR": 0}
    if last_run:
        for r in conn.execute("SELECT status, COUNT(*) c FROM verifications WHERE run_id=? GROUP BY status", (last_run,)):
            ver[r["status"]] = r["c"]

    success_rate = round(100 * s["ok"] / total) if total else None
    if last_run:
        ver_total = sum(ver.values())
        ver_rate = round(100 * ver["OK"] / ver_total) if ver_total else 100
    else:
        ver_rate = round(100 * s["verified"] / s["ok"]) if s["ok"] else 100

    if total:
        score = max(0, round(0.6 * success_rate + 0.4 * ver_rate - 10 * pattern_count))
        label = "healthy" if score >= 90 else "degraded" if score >= 70 else "critical"
    else:
        score, label = None, "no data"

    return {
        "total": total, "ok": s["ok"], "bad": s["bad"],
        "success_rate": success_rate, "ver_rate": ver_rate,
        "integrity_issues": s["upload_mismatch"] + ver["MISMATCH"] + ver["MISSING"],
        "retries": s["retries"],
        "retry_recovery": round(100 * t["recovered"] / t["trouble"]) if t["trouble"] else None,
        "avg_ms": round(s["avg_ms"]),
        "recurring": pattern_count,
        "score": score, "label": label, "has_verification": bool(last_run),
    }


# ---------- pages ----------
@app.route("/")
def dashboard():
    patterns = analyzer.detect_patterns()
    with get_conn() as conn:
        m = compute_metrics(conn, len(patterns))
        jobs_total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        tracked = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        recent = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 12").fetchall()
        logs = conn.execute("SELECT * FROM job_files ORDER BY id DESC LIMIT 14").fetchall()
        restores = conn.execute("SELECT * FROM restores ORDER BY id DESC LIMIT 5").fetchall()

    bars = []
    for j in reversed(recent):
        attempted = j["uploaded"] + j["failed"]
        pct = round(100 * j["uploaded"] / attempted) if attempted else 100
        bars.append({"id": j["id"], "pct": pct, "status": j["status"]})

    return render_template(
        "dashboard.html", m=m, bars=bars, logs=list(reversed(logs)), restores=restores,
        patterns=patterns, jobs_total=jobs_total, tracked=tracked,
        source=config.SOURCE_DIR, interval=config.BACKUP_INTERVAL_MINUTES,
    )


@app.route("/run", methods=["POST"])
def run():
    job_id = backup_engine.run_backup(trigger="manual")
    if job_id is None:
        flash("A backup is already running. Wait for it to finish.", "warn")
        return redirect(url_for("dashboard"))
    return redirect(url_for("job_detail", job_id=job_id))



@app.route("/alerts")
def alerts():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT 100").fetchall()
        conn.execute("UPDATE alerts SET read_at=? WHERE read_at IS NULL", (now(),))
    return render_template("alerts.html", alerts=rows)


@app.route("/demo/failure", methods=["POST"])
def demo_failure():
    if config.STORAGE_BACKEND == "s3":
        flash("Demo failure controls are disabled when using real S3.", "warn")
        return redirect(url_for("dashboard"))
    level = request.form.get("level", "off")
    rates = {"off": 0.0, "light": 0.30, "heavy": 0.70, "outage": 1.0}
    if level not in rates:
        flash("Unknown failure level.", "bad")
    else:
        config.SIMULATE_FAILURE_RATE = rates[level]
        flash(f"Demo failure level set to {level} ({int(rates[level]*100)}%). Run a backup to test alerts.", "ok")
    return redirect(url_for("dashboard"))


@app.route("/demo/change-files", methods=["POST"])
def demo_change_files():
    if config.STORAGE_BACKEND == "s3":
        flash("Demo file changes are disabled when using real S3.", "warn")
        return redirect(url_for("dashboard"))
    source = config.SOURCE_DIR
    source.mkdir(parents=True, exist_ok=True)
    candidates = [p for p in source.rglob("*") if p.is_file()]
    changed = 0
    for path in candidates[:20]:
        try:
            with path.open("ab") as f:
                f.write(("\n# BackupBuddyAI demo change " + str(random.randint(1000,9999))).encode())
            changed += 1
        except OSError:
            pass
    demo_dir = source / "demo_generated"
    demo_dir.mkdir(exist_ok=True)
    for i in range(1, 4):
        (demo_dir / f"demo_file_{i}.txt").write_text(
            f"Generated by BackupBuddyAI demo on {now()}\n", encoding="utf-8")
    flash(f"Changed {changed} existing files and created 3 demo files. Run backup to see uploads/failures.", "ok")
    return redirect(url_for("dashboard"))


@app.route("/ai-risk")
def ai_risk():
    """Show per-file failure risk from history; clearly labels fallback estimates."""
    report = ai_predictor.build_risk_report()
    audit.record("VIEW_AI_RISK", "backup_history", details=f"history_rows={report['history_count']}")
    return render_template("ai_risk.html", report=report)


@app.route("/audit")
def audit_page():
    rows = audit.recent()
    return render_template("audit.html", rows=rows)


@app.route("/jobs")
def jobs():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 100").fetchall()
    return render_template("jobs.html", jobs=rows)


@app.route("/jobs/<int:job_id>")
def job_detail(job_id):
    with get_conn() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        files = conn.execute("SELECT * FROM job_files WHERE job_id=? ORDER BY status DESC, path", (job_id,)).fetchall()
    if job is None:
        flash("Job not found.", "warn")
        return redirect(url_for("jobs"))
    return render_template("job_detail.html", job=job, files=files)


@app.route("/jobs/<int:job_id>/retry", methods=["POST"])
def retry(job_id):
    with get_conn() as conn:
        keys = {r["path"] for r in conn.execute(
            "SELECT path FROM job_files WHERE job_id=? AND status='FAILED'", (job_id,))}
    if not keys:
        flash("This job has no failed files to retry.", "warn")
        return redirect(url_for("job_detail", job_id=job_id))
    new_id = backup_engine.run_backup(trigger="retry", only_keys=keys)
    if new_id is None:
        flash("A backup is already running. Try again in a moment.", "warn")
        return redirect(url_for("job_detail", job_id=job_id))
    return redirect(url_for("job_detail", job_id=new_id))


@app.route("/verify", methods=["GET", "POST"])
def verify():
    if request.method == "POST":
        try:
            run_id, counts = backup_engine.verify_all()
            audit.record("VERIFY_BACKUPS", f"run={run_id}", details=str(counts))
        except Exception as exc:  # noqa: BLE001
            audit.record("VERIFY_BACKUPS", "all", outcome="FAILED", details=type(exc).__name__)
            flash(f"Verification could not finish: {exc}", "bad")
        return redirect(url_for("verify"))
    with get_conn() as conn:
        last = conn.execute("SELECT MAX(run_id) FROM verifications").fetchone()[0]
        rows = conn.execute("SELECT * FROM verifications WHERE run_id=? ORDER BY status='OK', path", (last,)).fetchall() if last else []
    counts = {"OK": 0, "MISMATCH": 0, "MISSING": 0, "ERROR": 0}
    for r in rows:
        counts[r["status"]] += 1
    return render_template("verify.html", rows=rows, counts=counts, run_at=rows[0]["run_at"] if rows else None)


@app.route("/files")
def files():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM files ORDER BY path").fetchall()
    return render_template("files.html", files=rows)


@app.route("/files/<path:key>")
def file_versions(key):
    if not safe_key(key):
        flash("Invalid file name.", "bad")
        return redirect(url_for("files"))
    try:
        versions = get_storage().list_versions(key)
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not list versions: {exc}", "bad")
        versions = []
    return render_template("versions.html", key=key, versions=versions)


@app.route("/restore", methods=["POST"])
def restore():
    key, version_id = request.form["key"], request.form["version_id"]
    if not safe_key(key) or not safe_version_id(version_id):
        flash("Invalid file name.", "bad")
        return redirect(url_for("files"))
    dest = config.RESTORE_DIR / key
    try:
        get_storage().download(key, version_id, dest)
        with get_conn() as conn:
            conn.execute("INSERT INTO restores (path,version_id,dest,created) VALUES (?,?,?,?)",
                         (key, version_id, str(dest), now()))
        audit.record("RESTORE_FILE", key, details=f"version={version_id}")
        flash(f"Restored to {dest}", "ok")
    except Exception as exc:  # noqa: BLE001
        audit.record("RESTORE_FILE", key, outcome="FAILED", details=type(exc).__name__)
        flash(f"Restore failed: {exc}", "bad")
    return redirect(url_for("file_versions", key=key))


@app.route("/download")
def download():
    key, version_id = request.args.get("key", ""), request.args.get("version_id", "")
    if not safe_key(key) or not safe_version_id(version_id):
        flash("Invalid file name.", "bad")
        return redirect(url_for("files"))
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp()
    try:
        dest = Path(tmp) / "download.bin"
        get_storage().download(key, version_id, dest)
        data = dest.read_bytes()
    except Exception as exc:  # noqa: BLE001
        flash(f"Download failed: {exc}", "bad")
        return redirect(url_for("file_versions", key=key))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return send_file(io.BytesIO(data), as_attachment=True, download_name=Path(key).name)


@app.route("/report.csv")
def report():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT job_id,created,path,status,attempts,verified,size_bytes,duration_ms,error_type,error_message "
            "FROM job_files ORDER BY id").fetchall()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["job_id", "time_utc", "file", "status", "attempts", "verified", "size_bytes", "duration_ms", "error_type", "error_message"])
    for r in rows:
        w.writerow(list(r))
    return Response(out.getvalue(), mimetype="text/csv",
                   headers={"Content-Disposition": "attachment; filename=backupbuddy_report.csv"})


@app.route("/analyze/<error_type>")
def analyze(error_type):
    info = analyzer.ADVICE.get(error_type, analyzer.ADVICE["UNKNOWN"])
    ai_text = analyzer.ask_ai(error_type)
    return render_template(
        "analyze.html", error_type=error_type, info=info, ai_text=ai_text,
        ai_enabled=bool(config.ANTHROPIC_API_KEY), history=analyzer.analysis_history(error_type),
    )


if __name__ == "__main__":
    # debug=False so the scheduler thread is not started twice
    import os
    app.run(host=os.getenv("APP_HOST", "127.0.0.1"), port=int(os.getenv("APP_PORT", "5000")), debug=False)
