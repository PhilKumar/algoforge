#!/usr/bin/env python3
"""Move PhilForge runtime state outside the Git checkout without deleting sources.

The migration is deliberately maintenance-only.  A dry-run is the default;
the caller must supply both ``--apply`` and ``--maintenance-confirmed`` after
stopping every PhilForge worker that can write runtime state.

Safety properties:
- SQLite is copied through its online backup API, never with a raw file copy.
- The copied database must pass ``PRAGMA quick_check`` before activation.
- User data, option history and backups are copied; original paths are kept as
  rollback copies and are never removed by this script.
- The .env path switch is the final atomic operation and secret values are
  never printed.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


PATH_KEYS = {
    "PHILFORGE_DB": "philforge.db",
    "PHILFORGE_USER_DATA_ROOT": "users",
    "PHILFORGE_BACKUP_ROOT": "backups",
    "PHILFORGE_OPTION_ARCHIVE_ROOT": "option-archive",
}


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _require_safe_destination(state_root: Path, app_root: Path) -> None:
    if state_root == app_root or _inside(state_root, app_root):
        raise RuntimeError("State root must be outside the PhilForge Git checkout")
    if _inside(app_root, state_root):
        raise RuntimeError("State root must not contain the PhilForge Git checkout")
    if state_root == Path(state_root.anchor):
        raise RuntimeError("Refusing to use a filesystem root as the state root")


def _sqlite_snapshot(source: Path, destination: Path) -> str:
    if not source.exists():
        raise RuntimeError(f"Source database does not exist: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".philforge-db-", suffix=".tmp", dir=destination.parent)
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with sqlite3.connect(source) as src, sqlite3.connect(tmp_path) as dest:
            src.backup(dest)
        with sqlite3.connect(tmp_path) as copied:
            quick_check = str(copied.execute("PRAGMA quick_check").fetchone()[0])
        if quick_check.lower() != "ok":
            raise RuntimeError(f"Copied database failed SQLite quick_check: {quick_check}")
        os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp_path, destination)
        return quick_check
    finally:
        tmp_path.unlink(missing_ok=True)


def _copy_tree(source: Path, destination: Path) -> dict[str, int | bool]:
    if source.resolve() == destination.resolve():
        return {"source_present": source.exists(), "files_copied": 0, "already_external": True}
    if not source.exists():
        destination.mkdir(parents=True, exist_ok=True)
        return {"source_present": False, "files_copied": 0, "already_external": False}
    destination.mkdir(parents=True, exist_ok=True)
    before = {path.relative_to(destination) for path in destination.rglob("*") if path.is_file()}
    shutil.copytree(source, destination, dirs_exist_ok=True, copy_function=shutil.copy2)
    after = {path.relative_to(destination) for path in destination.rglob("*") if path.is_file()}
    return {
        "source_present": True,
        "files_copied": len(after - before),
        "already_external": False,
    }


def _update_env(env_path: Path, destinations: dict[str, Path]) -> None:
    existing_text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    output: list[str] = []
    seen: set[str] = set()
    for line in existing_text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _ = line.split("=", 1)
            key = key.strip()
            if key in destinations:
                output.append(f"{key}={destinations[key]}")
                seen.add(key)
                continue
        output.append(line)
    for key, destination in destinations.items():
        if key not in seen:
            output.append(f"{key}={destination}")

    env_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".env-storage-", dir=env_path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(output).rstrip() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp_path, env_path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _plan(app_root: Path, state_root: Path, env_path: Path) -> dict:
    config = importlib.import_module("config")
    sources = {
        "PHILFORGE_DB": Path(config.DB_PATH).expanduser().resolve(),
        "PHILFORGE_USER_DATA_ROOT": Path(config.USER_DATA_ROOT).expanduser().resolve(),
        "PHILFORGE_BACKUP_ROOT": Path(config.BACKUP_ROOT).expanduser().resolve(),
        "PHILFORGE_OPTION_ARCHIVE_ROOT": Path(config.OPTION_ARCHIVE_ROOT).expanduser().resolve(),
    }
    destinations = {key: (state_root / rel).resolve() for key, rel in PATH_KEYS.items()}
    return {
        "app_root": str(app_root),
        "state_root": str(state_root),
        "env_file": str(env_path),
        "sources": {key: str(value) for key, value in sources.items()},
        "destinations": {key: str(value) for key, value in destinations.items()},
        "source_paths_preserved": True,
        "requires_stopped_writers": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely migrate PhilForge runtime storage outside its checkout.")
    parser.add_argument("--app-root", default=str(REPO_ROOT))
    parser.add_argument("--state-root", default="/home/ec2-user/.local/share/philforge")
    parser.add_argument("--env-file", default="")
    parser.add_argument("--apply", action="store_true", help="Copy and activate the planned runtime paths")
    parser.add_argument(
        "--maintenance-confirmed",
        action="store_true",
        help="Confirm every PhilForge writer has been stopped for the migration window",
    )
    args = parser.parse_args()

    app_root = Path(args.app_root).expanduser().resolve()
    state_root = Path(args.state_root).expanduser().resolve()
    env_path = Path(args.env_file).expanduser().resolve() if args.env_file else app_root / ".env"
    _require_safe_destination(state_root, app_root)
    plan = _plan(app_root, state_root, env_path)

    if not args.apply:
        print(json.dumps({"status": "planned", **plan}, indent=2))
        return 0
    if not args.maintenance_confirmed:
        raise RuntimeError("--maintenance-confirmed is required with --apply")

    state_root.mkdir(parents=True, exist_ok=True)
    os.chmod(state_root, stat.S_IRWXU)
    sources = {key: Path(value) for key, value in plan["sources"].items()}
    destinations = {key: Path(value) for key, value in plan["destinations"].items()}

    db_quick_check = "already_external"
    if sources["PHILFORGE_DB"] != destinations["PHILFORGE_DB"]:
        db_quick_check = _sqlite_snapshot(sources["PHILFORGE_DB"], destinations["PHILFORGE_DB"])
    elif sources["PHILFORGE_DB"].exists():
        with sqlite3.connect(sources["PHILFORGE_DB"]) as conn:
            db_quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])

    copies = {
        "user_data": _copy_tree(sources["PHILFORGE_USER_DATA_ROOT"], destinations["PHILFORGE_USER_DATA_ROOT"]),
        "backups": _copy_tree(sources["PHILFORGE_BACKUP_ROOT"], destinations["PHILFORGE_BACKUP_ROOT"]),
        "option_archive": _copy_tree(
            sources["PHILFORGE_OPTION_ARCHIVE_ROOT"], destinations["PHILFORGE_OPTION_ARCHIVE_ROOT"]
        ),
    }
    for destination in destinations.values():
        if destination.suffix != ".db":
            destination.mkdir(parents=True, exist_ok=True)
            os.chmod(destination, stat.S_IRWXU)

    _update_env(env_path, destinations)
    print(
        json.dumps(
            {
                "status": "migrated",
                "completed_at_utc": datetime.now(timezone.utc).isoformat(),
                "db_quick_check": db_quick_check,
                "copies": copies,
                **plan,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
