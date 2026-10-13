"""SQLite helpers. Tables: files, jobs, job_files, verifications, ai_analysis, restores."""
import sqlite3
from datetime import datetime, timezone

from config import DB_PATH


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS files (
                path        TEXT PRIMARY KEY,
                last_hash   TEXT NOT NULL,
                last_backup TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                started    TEXT NOT NULL,
                finished   TEXT,
                trigger    TEXT NOT NULL,
                status     TEXT NOT NULL,
                uploaded   INTEGER DEFAULT 0,
                skipped    INTEGER DEFAULT 0,
                failed     INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS job_files (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id        INTEGER NOT NULL,
                path          TEXT NOT NULL,
                status        TEXT NOT NULL,       -- SUCCESS or FAILED
                attempts      INTEGER DEFAULT 1,
                verified      INTEGER DEFAULT 0,   -- 1 = checksum matched after upload
                checksum      TEXT,
                version_id    TEXT,
                error_type    TEXT,
                error_message TEXT,
                size_bytes    INTEGER DEFAULT 0,
                duration_ms   INTEGER DEFAULT 0,
                created       TEXT NOT NULL,
                FOREIGN KEY (job_id) REFERENCES jobs(id)
            );
            CREATE TABLE IF NOT EXISTS verifications (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id   INTEGER NOT NULL,
                run_at   TEXT NOT NULL,
                path     TEXT NOT NULL,
                expected TEXT,
                observed TEXT,
                status   TEXT NOT NULL            -- OK, MISMATCH, MISSING, ERROR
            );
            CREATE TABLE IF NOT EXISTS ai_analysis (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                error_type TEXT NOT NULL,
                summary    TEXT NOT NULL,
                created    TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS restores (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                path       TEXT NOT NULL,
                version_id TEXT NOT NULL,
                dest       TEXT NOT NULL,
                created    TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                resource TEXT NOT NULL DEFAULT '',
                outcome TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '',
                created TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                severity TEXT NOT NULL,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                job_id INTEGER,
                created TEXT NOT NULL,
                read_at TEXT,
                FOREIGN KEY (job_id) REFERENCES jobs(id)
            );
            """
        )
        # upgrade older databases that lack the new columns
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(job_files)")}
        for col in ("size_bytes", "duration_ms"):
            if col not in cols:
                conn.execute(f"ALTER TABLE job_files ADD COLUMN {col} INTEGER DEFAULT 0")
