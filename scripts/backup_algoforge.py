#!/usr/bin/env python3
"""Create a timestamped AlgoForge data backup archive.

Backs up:
- SQLite database via sqlite3 backup API (safe with WAL)
- per-user data root (charts, engine state, etc.)

Output:
- tar.gz archive under config.BACKUP_ROOT or --output-dir
- latest symlink for convenience
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sqlite3
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _archive_name(ts: datetime) -> str:
    return f"algoforge-backup-{ts.strftime('%Y%m%d-%H%M%S')}.tar.gz"


def _snapshot_db(src_path: Path, dest_path: Path) -> None:
    if not src_path.exists():
        raise FileNotFoundError(f"Database not found: {src_path}")
    src = sqlite3.connect(src_path)
    try:
        dest = sqlite3.connect(dest_path)
        try:
            src.backup(dest)
        finally:
            dest.close()
    finally:
        src.close()


def _copy_user_data(src_root: Path, dest_root: Path) -> int:
    if not src_root.exists():
        dest_root.mkdir(parents=True, exist_ok=True)
        return 0
    if dest_root.exists():
        shutil.rmtree(dest_root)
    shutil.copytree(src_root, dest_root)
    return sum(1 for _ in dest_root.rglob("*"))


def _write_manifest(path: Path, db_src: Path, user_data_src: Path, archive_name: str, copied_items: int) -> None:
    manifest = {
        "created_at_utc": _now_utc().isoformat(),
        "hostname": socket.gethostname(),
        "db_source": str(db_src),
        "user_data_source": str(user_data_src),
        "archive_name": archive_name,
        "copied_user_data_items": copied_items,
    }
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _build_archive(staging_dir: Path, archive_path: Path) -> None:
    with tarfile.open(archive_path, "w:gz") as tf:
        tf.add(staging_dir, arcname="algoforge-backup")


def _update_latest_symlink(output_dir: Path, archive_path: Path) -> None:
    latest = output_dir / "latest.tar.gz"
    try:
        if latest.exists() or latest.is_symlink():
            latest.unlink()
        latest.symlink_to(archive_path.name)
    except OSError:
        shutil.copy2(archive_path, latest)


def _prune_old_archives(output_dir: Path, retention_days: int) -> int:
    if retention_days <= 0:
        return 0
    cutoff = _now_utc().timestamp() - retention_days * 86400
    removed = 0
    for path in output_dir.glob("algoforge-backup-*.tar.gz"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except FileNotFoundError:
            continue
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a timestamped AlgoForge backup archive.")
    parser.add_argument("--output-dir", default=config.BACKUP_ROOT, help="Directory to store backup archives")
    parser.add_argument(
        "--retention-days",
        type=int,
        default=config.BACKUP_RETENTION_DAYS,
        help="Delete archives older than this many days (0 disables pruning)",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    db_src = Path(config.DB_PATH).expanduser().resolve()
    user_data_src = Path(config.USER_DATA_ROOT).expanduser().resolve()
    timestamp = _now_utc()
    archive_path = output_dir / _archive_name(timestamp)

    with tempfile.TemporaryDirectory(prefix="algoforge-backup-", dir=str(output_dir)) as tmp_root:
        staging_root = Path(tmp_root) / "algoforge-backup"
        staging_root.mkdir(parents=True, exist_ok=True)
        db_dest = staging_root / "algoforge.db"
        user_data_dest = staging_root / "user-data"
        _snapshot_db(db_src, db_dest)
        copied_items = _copy_user_data(user_data_src, user_data_dest)
        _write_manifest(staging_root / "manifest.json", db_src, user_data_src, archive_path.name, copied_items)
        _build_archive(staging_root, archive_path)

    _update_latest_symlink(output_dir, archive_path)
    removed = _prune_old_archives(output_dir, args.retention_days)

    print(json.dumps({"status": "ok", "archive": str(archive_path), "pruned": removed}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
