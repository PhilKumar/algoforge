#!/usr/bin/env python3
"""Create a timestamped PhilForge backup archive.

Backs up:
- SQLite database via sqlite3 backup API (safe with WAL)
- per-user data root (charts, engine state, etc.)
- optional legacy flat files/directories for migration rollback

Output:
- tar.gz archive under config.BACKUP_ROOT or --output-dir
- latest symlink for convenience

Safety:
- streams large trees directly into the archive instead of staging a full copy
- prunes expired local archives before enforcing the free-space reserve
- aborts early when free disk space is too low for a safe local backup
"""

from __future__ import annotations

import argparse
import hashlib
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
from urllib.parse import urlparse

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
    return f"philforge-backup-{ts.strftime('%Y%m%d-%H%M%S')}.tar.gz"


def _human_bytes(num_bytes: int) -> str:
    size = float(max(num_bytes, 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
            sources.append((path, f"philforge-backup/legacy/{rel}"))
    for pattern in ENGINE_STATE_PATTERNS:
        for path in sorted(root.glob(pattern)):
            sources.append((path, f"philforge-backup/legacy/engine-state/{path.name}"))
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
    option_archive_src: Path,
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
        "option_archive_source": str(option_archive_src),
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
    option_archive_src: Path,
    legacy_sources: list[tuple[Path, str]],
) -> None:
    with tarfile.open(archive_path, "w:gz") as tf:
        if db_present:
            tf.add(db_snapshot_path, arcname="philforge-backup/philforge.db")
        tf.add(manifest_path, arcname="philforge-backup/manifest.json")

        if user_data_src.exists():
            tf.add(user_data_src, arcname="philforge-backup/user-data")
        else:
            with tempfile.TemporaryDirectory(prefix="philforge-empty-user-data-") as tmp_root:
                empty_dir = Path(tmp_root) / "user-data"
                empty_dir.mkdir()
                tf.add(empty_dir, arcname="philforge-backup/user-data")

        if option_archive_src.exists():
            tf.add(option_archive_src, arcname="philforge-backup/option-archive")

        for src_path, arcname in legacy_sources:
            tf.add(src_path, arcname=arcname)


def _update_latest_symlink(output_dir: Path, archive_path: Path, checksum_path: Path) -> None:
    latest = output_dir / "latest.tar.gz"
    try:
        if latest.exists() or latest.is_symlink():
            latest.unlink()
        latest.symlink_to(archive_path.name)
    except OSError:
        shutil.copy2(archive_path, latest)
    latest_checksum = output_dir / "latest.sha256"
    try:
        if latest_checksum.exists() or latest_checksum.is_symlink():
            latest_checksum.unlink()
        latest_checksum.symlink_to(checksum_path.name)
    except OSError:
        shutil.copy2(checksum_path, latest_checksum)


def _prune_old_archives(output_dir: Path, retention_days: int) -> int:
    if retention_days <= 0:
        return 0
    cutoff = _now_utc().timestamp() - retention_days * 86400
    removed = 0
    for path in output_dir.glob("philforge-backup-*.tar.gz"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                checksum = path.with_suffix(path.suffix + ".sha256")
                checksum.unlink(missing_ok=True)
                removed += 1
        except FileNotFoundError:
            continue

    # Avoid leaving convenience links dangling when their expired target was
    # pruned before a replacement backup can be created.
    for latest_name in ("latest.tar.gz", "latest.sha256"):
        latest = output_dir / latest_name
        if latest.is_symlink() and not latest.exists():
            latest.unlink(missing_ok=True)
    return removed


def _upload_offsite(archive_path: Path, checksum_path: Path) -> dict[str, str]:
    """Upload a backup to S3 with mandatory server-side encryption when configured."""
    destination = str(getattr(config, "BACKUP_S3_URI", "") or "").strip().rstrip("/")
    if not destination:
        return {"status": "not_configured"}
    parsed = urlparse(destination)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise RuntimeError("PHILFORGE_BACKUP_S3_URI must be an s3://bucket/prefix URI")
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("boto3 is required when PHILFORGE_BACKUP_S3_URI is configured") from exc

    prefix = parsed.path.strip("/")
    archive_key = "/".join(part for part in (prefix, archive_path.name) if part)
    checksum_key = "/".join(part for part in (prefix, checksum_path.name) if part)
    kms_key = str(getattr(config, "BACKUP_S3_KMS_KEY_ID", "") or "").strip()
    extra = (
        {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": kms_key} if kms_key else {"ServerSideEncryption": "AES256"}
    )
    client = boto3.client("s3")
    client.upload_file(str(archive_path), parsed.netloc, archive_key, ExtraArgs=extra)
    client.upload_file(str(checksum_path), parsed.netloc, checksum_key, ExtraArgs=extra)
    return {
        "status": "uploaded",
        "archive": f"s3://{parsed.netloc}/{archive_key}",
        "encryption": "aws:kms" if kms_key else "AES256",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a timestamped PhilForge backup archive.")
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
    option_archive_src = Path(config.OPTION_ARCHIVE_ROOT).expanduser().resolve()
    legacy_root = Path(args.legacy_root).expanduser().resolve()
    legacy_sources = _discover_legacy_sources(legacy_root) if args.include_legacy else []
    timestamp = _now_utc()
    archive_path = output_dir / _archive_name(timestamp)
    # Prune first so an expired local backup cannot prevent its own
    # replacement by tripping the minimum-free-space safety check.
    removed = _prune_old_archives(output_dir, args.retention_days)
    estimated_required = _estimate_required_bytes(db_src, user_data_src, legacy_sources)
    if option_archive_src != user_data_src and user_data_src not in option_archive_src.parents:
        estimated_required += _path_size(option_archive_src)
    disk_budget = _ensure_free_space(output_dir, estimated_required, config.BACKUP_MIN_FREE_MB)

    with tempfile.TemporaryDirectory(prefix="philforge-backup-meta-", dir=str(output_dir)) as tmp_root:
        tmp_dir = Path(tmp_root)
        db_dest = tmp_dir / "philforge.db"
        manifest_path = tmp_dir / "manifest.json"
        db_present = _snapshot_db(db_src, db_dest)
        _write_manifest(
            manifest_path,
            db_src,
            db_present,
            user_data_src,
            option_archive_src,
            archive_path.name,
            _tree_items(user_data_src),
            legacy_sources,
            disk_budget,
        )
        _build_archive(
            archive_path,
            db_dest,
            db_present,
            manifest_path,
            user_data_src,
            option_archive_src,
            legacy_sources,
        )

    checksum_path = archive_path.with_suffix(archive_path.suffix + ".sha256")
    checksum = _sha256_file(archive_path)
    checksum_path.write_text(f"{checksum}  {archive_path.name}\n", encoding="utf-8")
    _update_latest_symlink(output_dir, archive_path, checksum_path)
    offsite = _upload_offsite(archive_path, checksum_path)
    removed += _prune_old_archives(output_dir, args.retention_days)

    print(
        json.dumps(
            {
                "status": "ok",
                "archive": str(archive_path),
                "pruned": removed,
                "included_legacy": len(legacy_sources),
                "estimated_required_bytes": estimated_required,
                "free_bytes_before_backup": disk_budget["free_bytes"],
                "sha256": checksum,
                "offsite": offsite,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
