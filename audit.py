"""Minimal security audit trail for sensitive BackupBuddyAI actions."""
from database import get_conn, now

def record(action, resource="", outcome="SUCCESS", details=""):
    # Never put credentials or full file contents in audit details.
    with get_conn() as conn:
        conn.execute("INSERT INTO audit_logs(action,resource,outcome,details,created) VALUES(?,?,?,?,?)",
                     (str(action)[:80], str(resource)[:240], str(outcome)[:30], str(details)[:400], now()))

def recent(limit=200):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
