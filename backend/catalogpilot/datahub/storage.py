"""Object storage seam. Local filesystem here; S3-compatible storage implements the same calls.

Two buckets with different trust: ``quarantine`` holds unverified uploads and is read only by
the scanner; ``clean`` holds disarmed content. Keys are always server-generated.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from ..config import get_settings

BUCKETS = ("quarantine", "clean", "exports")
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,380}$")


def object_path(bucket: str, key: str) -> Path:
    if bucket not in BUCKETS or not _KEY.match(key) or ".." in key.split("/"):
        raise ValueError("invalid object reference")
    root = (get_settings().data_dir / "objects" / bucket).resolve()
    path = (root / key).resolve()
    if root not in path.parents:
        raise ValueError("invalid object reference")
    return path


def write_at(bucket: str, key: str, offset: int, data: bytes) -> None:
    path = object_path(bucket, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "r+b" if path.exists() else "wb"
    with path.open(mode) as fh:
        fh.truncate(offset)  # drop any bytes past the durable offset (torn previous write)
        fh.seek(offset)
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def put(bucket: str, key: str, data: bytes) -> None:
    path = object_path(bucket, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def copy(src_bucket: str, src_key: str, dst_bucket: str, dst_key: str) -> None:
    dst = object_path(dst_bucket, dst_key)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(object_path(src_bucket, src_key), dst)


def exists(bucket: str, key: str) -> bool:
    return object_path(bucket, key).is_file()


def delete(bucket: str, key: str) -> None:
    object_path(bucket, key).unlink(missing_ok=True)


def tenant_usage_bytes(tenant_id: str) -> int:
    total = 0
    for bucket in ("quarantine", "clean"):
        root = get_settings().data_dir / "objects" / bucket / tenant_id
        if root.is_dir():
            total += sum(f.stat().st_size for f in root.rglob("*") if f.is_file())
    return total
