"""Storage back-ends. Both expose the same 4 methods so the rest of the app
does not care whether files go to real S3 or to a local folder.

    upload(local_path, key, sha256_hex) -> version_id
    get_checksum(key, version_id)       -> sha256_hex  (what the cloud holds)
    list_versions(key)                  -> [{version_id, modified, size}, ...]
    download(key, version_id, dest)     -> None
"""
import base64
import hashlib
import random
import shutil
from datetime import datetime, timezone
from pathlib import Path

import config


class LocalStorage:
    """Simulated cloud: every upload becomes a new version file on disk."""

    name = "Local folder (simulated cloud)"

    def __init__(self):
        self.root = Path(config.LOCAL_CLOUD_DIR)
        self.root.mkdir(parents=True, exist_ok=True)

    def upload(self, local_path, key, sha256_hex):
        rate = config.SIMULATE_FAILURE_RATE
        roll = random.random()
        # simulated outage: mostly timeouts, so a recurring pattern appears in the demo
        if roll < rate * 0.9:
            raise TimeoutError("Connection timed out while uploading")

        version_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        dest = self.root / key / version_id
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, dest)

        if roll < rate:  # remaining 10%: silent corruption (caught by checksum)
            with open(dest, "ab") as f:
                f.write(b"\x00corrupt")
        return version_id

    def get_checksum(self, key, version_id):
        h = hashlib.sha256()
        with open(self.root / key / version_id, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def list_versions(self, key):
        folder = self.root / key
        if not folder.is_dir():
            return []
        out = []
        for p in sorted(folder.iterdir(), reverse=True):
            if p.is_file():
                st = p.stat()
                out.append({
                    "version_id": p.name,
                    "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    "size": st.st_size,
                })
        return out

    def download(self, key, version_id, dest):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.root / key / version_id, dest)


class S3Storage:
    """Real Amazon S3. Turn on bucket versioning so restores work."""

    name = "Amazon S3"

    def __init__(self):
        import boto3  # imported here so local mode works without boto3
        self.bucket = config.S3_BUCKET.strip()
        if not self.bucket or self.bucket == "your-bucket-name":
            raise RuntimeError("STORAGE_BACKEND=s3 requires S3_BUCKET to be set to your real bucket name.")
        self.s3 = boto3.client("s3", region_name=config.AWS_REGION)

    def upload(self, local_path, key, sha256_hex):
        checksum_b64 = base64.b64encode(bytes.fromhex(sha256_hex)).decode()
        with open(local_path, "rb") as f:
            # S3 itself rejects the upload if the data does not match this hash
            resp = self.s3.put_object(
                Bucket=self.bucket, Key=key, Body=f, ChecksumSHA256=checksum_b64
            )
        return resp.get("VersionId", "null")

    def get_checksum(self, key, version_id):
        args = {"Bucket": self.bucket, "Key": key, "ChecksumMode": "ENABLED"}
        if version_id and version_id != "null":
            args["VersionId"] = version_id
        head = self.s3.head_object(**args)
        value = head.get("ChecksumSHA256")
        if not value:
            raise RuntimeError("S3 returned no SHA-256 checksum for this object")
        return base64.b64decode(value).hex()

    def list_versions(self, key):
        resp = self.s3.list_object_versions(Bucket=self.bucket, Prefix=key)
        return [
            {
                "version_id": v["VersionId"],
                "modified": v["LastModified"].strftime("%Y-%m-%d %H:%M:%S"),
                "size": v["Size"],
            }
            for v in resp.get("Versions", [])
            if v["Key"] == key
        ]

    def download(self, key, version_id, dest):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        args = {"Bucket": self.bucket, "Key": key}
        if version_id and version_id != "null":
            args["VersionId"] = version_id
        body = self.s3.get_object(**args)["Body"]
        with open(dest, "wb") as f:
            for chunk in iter(lambda: body.read(1024 * 1024), b""):
                f.write(chunk)


def get_storage():
    return S3Storage() if config.STORAGE_BACKEND == "s3" else LocalStorage()
