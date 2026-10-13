"""Leakage-aware prototype for backup-failure risk estimation.

Features for each historical outcome use only information available before that
operation: current file size and the file's prior history. The model remains a
student-project prototype; small or imbalanced history falls back to transparent
rules instead of implying that an AI model was trained.
"""
from collections import defaultdict
from pathlib import Path

import config
from database import get_conn


def _features(size_bytes, prior_count, prior_failures, prior_streak, prior_avg_ms, prior_last_failed):
    count = int(prior_count or 0)
    fails = int(prior_failures or 0)
    rate_pct = (100.0 * fails / count) if count else 0.0
    return [
        min(max(int(size_bytes or 0), 0), 2_000_000_000),
        min(count, 1000),
        round(rate_pct, 2),
        min(int(prior_streak or 0), 100),
        min(max(float(prior_avg_ms or 0), 0.0), 600_000.0),
        1 if prior_last_failed else 0,
    ]


def _new_state():
    return {"count": 0, "failures": 0, "streak": 0, "duration_sum": 0.0,
            "duration_count": 0, "last_failed": False, "last_status": "NO HISTORY",
            "last_size": 0, "last_attempts": 1, "last_duration": 0}


def build_risk_report(limit=100):
    with get_conn() as conn:
        rows = conn.execute("""SELECT jf.path,jf.status,jf.attempts,jf.size_bytes,jf.duration_ms,
                                     jf.error_type,jf.created,j.id AS job_id
                              FROM job_files jf JOIN jobs j ON j.id=jf.job_id
                              WHERE jf.status IN ('SUCCESS','FAILED')
                              ORDER BY jf.id ASC LIMIT 5000""").fetchall()
        tracked = conn.execute("SELECT path,last_hash,last_backup FROM files ORDER BY path").fetchall()

    history = [dict(row) for row in rows]
    state = defaultdict(_new_state)
    X, y = [], []

    # Build examples chronologically. No current outcome/error field is included
    # in that row's features, avoiding direct label leakage into training.
    for row in history:
        key = row["path"]
        prior = state[key]
        current_features = _features(
            row["size_bytes"], prior["count"], prior["failures"], prior["streak"],
            (prior["duration_sum"] / prior["duration_count"] if prior["duration_count"] else 0),
            prior["last_failed"],
        )
        X.append(current_features)
        y.append(1 if row["status"] == "FAILED" else 0)

        failed = row["status"] == "FAILED"
        prior["count"] += 1
        prior["failures"] += int(failed)
        prior["streak"] = prior["streak"] + 1 if failed else 0
        prior["duration_sum"] += max(0, int(row["duration_ms"] or 0))
        prior["duration_count"] += 1
        prior["last_failed"] = failed
        prior["last_status"] = row["status"]
        prior["last_size"] = int(row["size_bytes"] or 0)
        prior["last_attempts"] = int(row["attempts"] or 1)
        prior["last_duration"] = int(row["duration_ms"] or 0)

    failure_count = sum(y)
    model = None
    model_accuracy = None
    model_note = (
        "Rule-based baseline: collect at least 30 labelled outcomes with both "
        "successful and failed operations (and at least 2 examples of each) "
        "before attempting the Random Forest model."
    )

    # Require enough history and at least two examples of each class so the
    # stratified holdout split has a chance to contain both classes.
    if len(y) >= 30 and y.count(0) >= 2 and y.count(1) >= 2:
        try:
            from sklearn.ensemble import RandomForestClassifier
            from sklearn.metrics import accuracy_score
            from sklearn.model_selection import train_test_split

            X_train, X_test, y_train, y_test = train_test_split(
                X, y, test_size=0.25, random_state=42, stratify=y
            )
            candidate = RandomForestClassifier(
                n_estimators=120, class_weight="balanced", random_state=42,
                min_samples_leaf=2,
            )
            candidate.fit(X_train, y_train)
            model_accuracy = round(accuracy_score(y_test, candidate.predict(X_test)) * 100, 1)
            model = candidate
            model_note = (
                "Random Forest trained from chronological history features. The displayed "
                "accuracy comes from a random holdout split and is indicative only; more "
                "data and time-based evaluation are needed for a reliable performance claim."
            )
        except Exception as exc:  # model is optional; dashboard must keep working
            model_note = f"Rule-based baseline used because model training was unavailable ({type(exc).__name__})."

    output = []
    for item in tracked:
        key = item["path"]
        prior = state[key]
        src = config.SOURCE_DIR.joinpath(*key.split("/"))
        try:
            size_now = src.stat().st_size if src.is_file() else prior["last_size"]
        except OSError:
            size_now = prior["last_size"]
        avg_ms = prior["duration_sum"] / prior["duration_count"] if prior["duration_count"] else 0
        features = _features(size_now, prior["count"], prior["failures"], prior["streak"], avg_ms, prior["last_failed"])

        if model:
            probabilities = model.predict_proba([features])[0]
            classes = list(model.classes_)
            risk = round(float(probabilities[classes.index(1)]) * 100) if 1 in classes else 0
            method = "Random Forest"
        else:
            # Transparent risk baseline; this is not a calibrated probability.
            failure_rate = prior["failures"] / prior["count"] if prior["count"] else 0
            risk = min(95, round(50 * failure_rate + 25 * min(prior["streak"], 2)
                                 + 15 * int(prior["last_failed"])
                                 + 10 * (avg_ms > 5000)))
            method = "Rule-based baseline"

        if prior["streak"] >= 2:
            reason = f"{prior['streak']} consecutive recent failures"
        elif prior["last_failed"]:
            reason = "Most recent backup failed"
        elif prior["failures"]:
            reason = f"{prior['failures']} historical failure(s)"
        else:
            reason = "No historical failures recorded"

        output.append({
            "path": key, "risk": risk, "failures": prior["failures"],
            "last_status": prior["last_status"], "method": method,
            "last_backup": item["last_backup"], "reason": reason,
        })

    output.sort(key=lambda row: row["risk"], reverse=True)
    return {
        "items": output[:limit], "model_note": model_note,
        "model_accuracy": model_accuracy, "history_count": len(y),
        "failure_count": failure_count, "tracked_count": len(tracked),
    }
