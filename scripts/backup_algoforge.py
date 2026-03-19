#!/usr/bin/env python3
"""Create a timestamped AlgoForge backup archive.

Backs up:
- SQLite database via sqlite3 backup API (safe with WAL)
- per-user data root (charts, engine state, etc.)
- optional legacy flat files/directories for migration rollback

Output:
- tar.gz archive under config.BACKUP_ROOT or --output-dir
- latest symlink for convenience

Safety:
- streams large trees directly into the archive instead of staging a full copy
- aborts early when free disk space is too low for a safe local backup
"""

from __future__ import annotations

import argparse
import importlib
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

config = importlib.import_module("config")


LEGACY_PATHS = (
    ".env",
    "strategies.json",
    "runs.json",
    "trade_history.json",
    "scalp_trades.json",
    "journals",
    "Daily Charts",
)
ENGINE_STATE_PATTERNS = ("live_state*.json", "paper_state*.json", "paper_history*.json", "scalp_state*.json")
BACKUP_META_OVERHEAD_BYTES = 64 * 1024 * 1024


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _archive_name(ts: datetime) -> str:
    return f"algoforge-backup-{ts.strftime('%Y%m%d-%H%M%S')}.tar.gz"


def _human_bytes(num_bytes: int) -> str:
    size = float(max(num_bytes, 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def _path_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            total += child.stat().st_size
    return total


def _tree_items(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for _ in path.rglob("*"))


def _snapshot_db(src_path: Path, dest_path: Path) -> bool:
    if not src_path.exists():
        return False
    src = sqlite3.connect(src_path)
    try:
        dest = sqlite3.connect(dest_path)
        try:
            src.backup(dest)
        finally:
            dest.close()
    finally:
        src.close()
    return True


def _discover_legacy_sources(root: Path) -> list[tuple[Path, str]]:
    sources: list[tuple[Path, str]] = []
    for rel in LEGACY_PATHS:
        path = root / rel
        if path.exists():
            sources.append((path, f"algoforge-backup/legacy/{rel}"))
    for pattern in ENGINE_STATE_PATTERNS:
        for path in sorted(root.glob(pattern)):
            sources.append((path, f"algoforge-backup/legacy/engine-state/{path.name}"))
    return sources


def _estimate_required_bytes(db_src: Path, user_data_src: Path, legacy_sources: list[tuple[Path, str]]) -> int:
    total = _path_size(db_src) + _path_size(user_data_src)
    total += sum(_path_size(path) for path, _ in legacy_sources)
    # Compression may reduce space, but a safe local same-disk backup should
    # assume little to no savings for binary chart/image assets.
    return total + BACKUP_META_OVERHEAD_BYTES


def _ensure_free_space(output_dir: Path, required_bytes: int, min_free_mb: int) -> dict[str, int]:
    usage = shutil.disk_usage(output_dir)
    min_free_bytes = max(min_free_mb, 0) * 1024 * 1024
    if usage.free < required_bytes + min_free_bytes:
        raise RuntimeError(
            "Insufficient free space for backup: "
            f"free={_human_bytes(usage.free)}, "
            f"estimated_required={_human_bytes(required_bytes)}, "
            f"minimum_free_after_backup={_human_bytes(min_free_bytes)}"
        )
    return {"free_bytes": usage.free, "required_bytes": required_bytes, "minimum_free_bytes": min_free_bytes}


def _write_manifest(
    path: Path,
    db_src: Path,
    db_present: bool,
    user_data_src: Path,
    archive_name: str,
    user_data_items: int,
    legacy_sources: list[tuple[Path, str]],
    disk_budget: dict[str, int],
) -> None:
    manifest = {
        "created_at_utc": _now_utc().isoformat(),
        "hostname": socket.gethostname(),
        "db_source": str(db_src),
        "db_present": db_present,
        "user_data_source": str(user_data_src),
        "archive_name": archive_name,
        "user_data_items": user_data_items,
        "legacy_sources": [arcname for _, arcname in legacy_sources],
        "disk_budget": disk_budget,
    }
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _build_archive(
    archive_path: Path,
    db_snapshot_path: Path,
    db_present: bool,
    manifest_path: Path,
    user_data_src: Path,
    legacy_sources: list[tuple[Path, str]],
) -> None:
    with tarfile.open(archive_path, "w:gz") as tf:
        if db_present:
            tf.add(db_snapshot_path, arcname="algoforge-backup/algoforge.db")
        tf.add(manifest_path, arcname="algoforge-backup/manifest.json")

        if user_data_src.exists():
            tf.add(user_data_src, arcname="algoforge-backup/user-data")
        else:
            with tempfile.TemporaryDirectory(prefix="algoforge-empty-user-data-") as tmp_root:
                empty_dir = Path(tmp_root) / "user-data"
                empty_dir.mkdir()
                tf.add(empty_dir, arcname="algoforge-backup/user-data")

        for src_path, arcname in legacy_sources:
            tf.add(src_path, arcname=arcname)


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
    parser.add_argument(
        "--include-legacy",
        action="store_true",
        help="Also include legacy JSON/files used for single-user rollback or migration recovery.",
    )
    parser.add_argument(
        "--legacy-root",
        default=os.getcwd(),
        help="Root directory used to discover legacy files when --include-legacy is set.",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    db_src = Path(config.DB_PATH).expanduser().resolve()
    user_data_src = Path(config.USER_DATA_ROOT).expanduser().resolve()
    legacy_root = Path(args.legacy_root).expanduser().resolve()
    legacy_sources = _discover_legacy_sources(legacy_root) if args.include_legacy else []
    timestamp = _now_utc()
    archive_path = output_dir / _archive_name(timestamp)
    estimated_required = _estimate_required_bytes(db_src, user_data_src, legacy_sources)
    disk_budget = _ensure_free_space(output_dir, estimated_required, config.BACKUP_MIN_FREE_MB)

    with tempfile.TemporaryDirectory(prefix="algoforge-backup-meta-", dir=str(output_dir)) as tmp_root:
        tmp_dir = Path(tmp_root)
        db_dest = tmp_dir / "algoforge.db"
        manifest_path = tmp_dir / "manifest.json"
        db_present = _snapshot_db(db_src, db_dest)
        _write_manifest(
            manifest_path,
            db_src,
            db_present,
            user_data_src,
            archive_path.name,
            _tree_items(user_data_src),
            legacy_sources,
            disk_budget,
        )
        _build_archive(archive_path, db_dest, db_present, manifest_path, user_data_src, legacy_sources)

    _update_latest_symlink(output_dir, archive_path)
    removed = _prune_old_archives(output_dir, args.retention_days)

    print(
        json.dumps(
            {
                "status": "ok",
                "archive": str(archive_path),
                "pruned": removed,
                "included_legacy": len(legacy_sources),
                "estimated_required_bytes": estimated_required,
                "free_bytes_before_backup": disk_budget["free_bytes"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
