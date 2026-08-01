#!/usr/bin/env python3
"""Verify the newest PhilForge backup without restoring over live data."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import sqlite3
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
config = importlib.import_module("config")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _expected_checksum(archive: Path) -> str:
    candidates = [archive.with_suffix(archive.suffix + ".sha256")]
    if archive.name == "latest.tar.gz":
        candidates.insert(0, archive.parent / "latest.sha256")
    for candidate in candidates:
        if candidate.exists():
            value = candidate.read_text(encoding="utf-8").strip().split()
            if value:
                return value[0]
    raise RuntimeError(f"Checksum sidecar missing for {archive}")


def _safe_member(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(path.parts) and not path.is_absolute() and ".." not in path.parts


def verify(archive: Path) -> dict:
    archive = archive.expanduser().resolve()
    expected = _expected_checksum(archive)
    actual = _sha256(archive)
    if actual != expected:
        raise RuntimeError(f"Backup checksum mismatch: expected {expected}, got {actual}")

    with tarfile.open(archive, "r:gz") as tf:
        members = tf.getmembers()
        unsafe = [member.name for member in members if not _safe_member(member.name)]
        if unsafe:
            raise RuntimeError(f"Unsafe archive member: {unsafe[0]}")
        names = {member.name for member in members}
        manifest_member = tf.getmember("philforge-backup/manifest.json")
        manifest_file = tf.extractfile(manifest_member)
        if manifest_file is None:
            raise RuntimeError("Backup manifest is unreadable")
        manifest = json.loads(manifest_file.read().decode("utf-8"))
        if not any(
            name == "philforge-backup/user-data" or name.startswith("philforge-backup/user-data/") for name in names
        ):
            raise RuntimeError("Backup has no user-data tree")

        db_quick_check = "not_present"
        if manifest.get("db_present"):
            db_member = tf.getmember("philforge-backup/philforge.db")
            db_file = tf.extractfile(db_member)
            if db_file is None:
                raise RuntimeError("Database snapshot is unreadable")
            with tempfile.TemporaryDirectory(prefix="philforge-verify-") as tmp:
                db_path = Path(tmp) / "philforge.db"
                db_path.write_bytes(db_file.read())
                with sqlite3.connect(db_path) as conn:
                    db_quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
                if db_quick_check.lower() != "ok":
                    raise RuntimeError(f"SQLite quick_check failed: {db_quick_check}")

    return {
        "status": "ok",
        "archive": str(archive),
        "sha256": actual,
        "members": len(members),
        "db_quick_check": db_quick_check,
        "created_at_utc": manifest.get("created_at_utc"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a PhilForge backup archive and its SQLite snapshot.")
    parser.add_argument("--archive", default=str(Path(config.BACKUP_ROOT) / "latest.tar.gz"))
    args = parser.parse_args()
    print(json.dumps(verify(Path(args.archive)), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
