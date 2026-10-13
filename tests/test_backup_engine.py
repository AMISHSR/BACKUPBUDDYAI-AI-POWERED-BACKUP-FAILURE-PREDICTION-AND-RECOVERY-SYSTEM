"""Local smoke tests for upload, incremental skip, failure alerts, retry and verification.
Run: python -m unittest discover -s tests -v
These tests use only the local simulator and never contact AWS.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import database
import backup_engine


class BackupEngineSmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.source = root / "source"
        self.cloud = root / "local_cloud"
        self.restored = root / "restored"
        self.db = root / "backupbuddy_test.db"
        self.source.mkdir()
        (self.source / "a.txt").write_text("alpha", encoding="utf-8")
        (self.source / "nested").mkdir()
        (self.source / "nested" / "b.txt").write_text("beta", encoding="utf-8")

        self.patches = [
            patch.object(database, "DB_PATH", self.db),
            patch.object(config, "SOURCE_DIR", self.source),
            patch.object(config, "LOCAL_CLOUD_DIR", self.cloud),
            patch.object(config, "RESTORE_DIR", self.restored),
            patch.object(config, "MAX_RETRIES", 1),
            patch.object(config, "BACKOFF_SECONDS", 0),
            patch.object(config, "SIMULATE_FAILURE_RATE", 0),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        database.init_db()

    def tearDown(self):
        self.tmp.cleanup()

    def test_backup_skip_and_integrity(self):
        first = backup_engine.run_backup()
        self.assertIsNotNone(first)
        with database.get_conn() as conn:
            row = conn.execute("SELECT status,uploaded,skipped,failed FROM jobs WHERE id=?", (first,)).fetchone()
            self.assertEqual(tuple(row), ("SUCCESS", 2, 0, 0))

        second = backup_engine.run_backup()
        with database.get_conn() as conn:
            row = conn.execute("SELECT status,uploaded,skipped,failed FROM jobs WHERE id=?", (second,)).fetchone()
            self.assertEqual(tuple(row), ("SUCCESS", 0, 2, 0))

        _, counts = backup_engine.verify_all()
        self.assertEqual(counts, {"OK": 2, "MISMATCH": 0, "MISSING": 0, "ERROR": 0})

    def test_failure_alert_and_retry_recovery(self):
        first = backup_engine.run_backup()
        self.assertIsNotNone(first)
        (self.source / "a.txt").write_text("changed", encoding="utf-8")
        config.SIMULATE_FAILURE_RATE = 1.0
        failed_job = backup_engine.run_backup()
        with database.get_conn() as conn:
            row = conn.execute("SELECT status,failed FROM jobs WHERE id=?", (failed_job,)).fetchone()
            self.assertEqual(row["status"], "FAILED")
            self.assertEqual(row["failed"], 1)
            keys = {r["path"] for r in conn.execute("SELECT path FROM job_files WHERE job_id=? AND status='FAILED'", (failed_job,))}
            self.assertGreater(conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 0)

        config.SIMULATE_FAILURE_RATE = 0.0
        recovered_job = backup_engine.run_backup(trigger="retry", only_keys=keys)
        with database.get_conn() as conn:
            row = conn.execute("SELECT status,uploaded,failed FROM jobs WHERE id=?", (recovered_job,)).fetchone()
            self.assertEqual(row["status"], "SUCCESS")
            self.assertEqual(row["uploaded"], 1)
            self.assertEqual(row["failed"], 0)


if __name__ == "__main__":
    unittest.main()
