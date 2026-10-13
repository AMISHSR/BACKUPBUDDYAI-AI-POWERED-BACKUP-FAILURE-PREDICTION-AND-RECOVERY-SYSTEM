"""All settings in one place. Values come from the .env file or environment."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_env_file():
    """Tiny .env reader (so we don't need an extra package)."""
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        # Support simple inline comments in .env files and avoid exporting
        # empty AWS credential variables that would shadow AWS CLI profiles.
        if " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if value:
            os.environ.setdefault(key, value)


_load_env_file()


def _path(name, default):
    p = Path(os.getenv(name) or default)
    return p if p.is_absolute() else BASE_DIR / p


STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "local").lower()
SOURCE_DIR = _path("SOURCE_DIR", "sample_source")
LOCAL_CLOUD_DIR = _path("LOCAL_CLOUD_DIR", "local_cloud")
RESTORE_DIR = _path("RESTORE_DIR", "restored")
DB_PATH = _path("DB_PATH", "backupbuddy.db")

S3_BUCKET = os.getenv("S3_BUCKET", "")
AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")

BACKUP_INTERVAL_MINUTES = int(os.getenv("BACKUP_INTERVAL_MINUTES", "0"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
BACKOFF_SECONDS = float(os.getenv("BACKOFF_SECONDS", "1"))
SIMULATE_FAILURE_RATE = float(os.getenv("SIMULATE_FAILURE_RATE", "0"))

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")

PATTERN_THRESHOLD = int(os.getenv("PATTERN_THRESHOLD", "3"))
PATTERN_WINDOW_HOURS = int(os.getenv("PATTERN_WINDOW_HOURS", "24"))

SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")
APP_ENV = os.getenv("APP_ENV", "development").lower()
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true" if APP_ENV == "production" else "false").lower() in {"1", "true", "yes", "on"}
SESSION_MINUTES = int(os.getenv("SESSION_MINUTES", "60"))

if APP_ENV == "production":
    if SECRET_KEY == "change-me-in-production" or len(SECRET_KEY) < 32:
        raise RuntimeError("Production requires a strong SECRET_KEY (at least 32 characters).")
    if not AUTH_ENABLED or not ADMIN_USERNAME or not ADMIN_PASSWORD:
        raise RuntimeError("Production requires AUTH_ENABLED=true, ADMIN_USERNAME, and ADMIN_PASSWORD.")
    if not COOKIE_SECURE:
        raise RuntimeError("Production requires COOKIE_SECURE=true and HTTPS.")
if AUTH_ENABLED and (not ADMIN_USERNAME or not ADMIN_PASSWORD):
    raise RuntimeError("When AUTH_ENABLED=true, set ADMIN_USERNAME and ADMIN_PASSWORD.")
