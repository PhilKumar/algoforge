#!/usr/bin/env python3
"""
migrate_to_sqlite.py — AlgoForge JSON → SQLite Migration

Migrates existing JSON flat-file data into the SQLite database.
Idempotent — safe to run multiple times (skips existing records/files).

Usage:
    cd /path/to/algoforge
    python scripts/migrate_to_sqlite.py
"""

import asyncio
import glob
import json
import os
import shutil
import sys

# Add project root to path
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

from dotenv import load_dotenv

load_dotenv()

import aiosqlite

import auth
import config
import db


def _now_iso():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _bootstrap_password() -> str:
    password = (os.getenv("ALGOFORGE_PIN") or os.getenv("ALGOFORGE_PASSWORD") or "").strip()
    if not password:
        raise RuntimeError(
            "No admin account exists yet. Set ALGOFORGE_PIN or ALGOFORGE_PASSWORD before running the migration."
        )
    return password


async def _ensure_admin_user() -> int:
    admin = await db.get_admin_user(config.ADMIN_USERNAME)
    if admin:
        print(f"ℹ️  Admin user already exists as '{admin['username']}' (id={admin['id']})")
        return admin["id"]

    hashed = auth.hash_password(_bootstrap_password())
    admin_id = await db.create_user(config.ADMIN_USERNAME, hashed, role="admin")
    print(f"✅ Created admin user '{config.ADMIN_USERNAME}' (id={admin_id})")
    return admin_id


async def _record_exists(conn: aiosqlite.Connection, query: str, params: tuple) -> bool:
    cursor = await conn.execute(query, params)
    row = await cursor.fetchone()
    return bool(row and row[0])


def _copy_tree_if_missing(src_root: str, dst_root: str) -> tuple[int, int]:
    """Copy only missing files from src_root into dst_root."""
    copied = 0
    skipped = 0
    if not os.path.isdir(src_root):
        return copied, skipped
    for current_root, _, files in os.walk(src_root):
        rel_root = os.path.relpath(current_root, src_root)
        target_root = dst_root if rel_root == "." else os.path.join(dst_root, rel_root)
        os.makedirs(target_root, exist_ok=True)
        for name in files:
            src_path = os.path.join(current_root, name)
            dst_path = os.path.join(target_root, name)
            if os.path.exists(dst_path):
                skipped += 1
                continue
            shutil.copy2(src_path, dst_path)
            copied += 1
    return copied, skipped


def _copy_file_if_missing(src_path: str, dst_path: str) -> bool:
    if not os.path.isfile(src_path) or os.path.exists(dst_path):
        return False
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    shutil.copy2(src_path, dst_path)
    return True


async def migrate():
    print("=" * 60)
    print("  AlgoForge — JSON → SQLite Migration")
    print("=" * 60)
    print(f"  DB path: {config.DB_PATH}")
    print()

    db._init_db_sync()
    print("✅ Database initialized")

    admin_id = await _ensure_admin_user()

    async with aiosqlite.connect(config.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row

        # 1. Migrate strategies.json
        strats_file = os.path.join(_ROOT, "strategies.json")
        if os.path.exists(strats_file):
            try:
                with open(strats_file, encoding="utf-8") as f:
                    strats = json.load(f)
                count = 0
                skipped = 0
                items = []
                if isinstance(strats, dict):
                    items = [(name, cfg) for name, cfg in strats.items()]
                elif isinstance(strats, list):
                    items = [(s.get("run_name", s.get("name", f"Strategy_{i}")), s) for i, s in enumerate(strats)]

                for name, cfg in items:
                    exists = await _record_exists(
                        conn,
                        "SELECT 1 FROM strategies WHERE user_id = ? AND name = ? LIMIT 1",
                        (admin_id, name),
                    )
                    if exists:
                        skipped += 1
                        continue
                    cfg_copy = dict(cfg)
                    now = _now_iso()
                    if "_folder" in cfg_copy and "folder" not in cfg_copy:
                        cfg_copy["folder"] = cfg_copy.pop("_folder")
                    if "_versions" in cfg_copy and "versions" not in cfg_copy:
                        cfg_copy["versions"] = cfg_copy.pop("_versions")
                    cfg_copy["run_name"] = cfg_copy.get("run_name") or name
                    cfg_copy["name"] = cfg_copy.get("name") or name
                    cfg_copy["created_at"] = cfg_copy.get("created_at") or now
                    cfg_copy["updated_at"] = cfg_copy.get("updated_at") or cfg_copy["created_at"]
                    cfg_copy["version"] = int(cfg_copy.get("version", 1) or 1)
                    await db.create_strategy_record(admin_id, cfg_copy)
                    count += 1
                await conn.commit()
                print(f"✅ Migrated {count} strategies ({skipped} skipped)")
            except Exception as e:
                print(f"❌ Failed to migrate strategies: {e}")
        else:
            print("ℹ️  No strategies.json found — skipping")

        # 2. Migrate runs.json
        runs_file = os.path.join(_ROOT, "runs.json")
        if os.path.exists(runs_file):
            try:
                with open(runs_file, encoding="utf-8") as f:
                    runs = json.load(f)
                count = 0
                skipped = 0
                for run in runs:
                    mode = run.get("mode", "backtest")
                    strat_name = run.get("run_name") or run.get("strategy_name") or run.get("name", "")
                    trade_count = run.get("trade_count", len(run.get("trades", [])))
                    total_pnl = run.get("total_pnl", 0)
                    created = run.get("created_at", run.get("timestamp", _now_iso()))
                    exists = await _record_exists(
                        conn,
                        "SELECT 1 FROM runs WHERE user_id = ? AND mode = ? AND strategy_name = ? AND created_at = ? "
                        "AND trade_count = ? LIMIT 1",
                        (admin_id, mode, strat_name, created, trade_count),
                    )
                    if exists:
                        skipped += 1
                        continue
                    run_payload = dict(run)
                    run_payload.pop("id", None)
                    run_payload["mode"] = mode
                    run_payload["run_name"] = run_payload.get("run_name") or strat_name or f"Run_{count + skipped + 1}"
                    run_payload["created_at"] = created
                    run_payload["trade_count"] = trade_count
                    run_payload["total_pnl"] = total_pnl
                    if run.get("summary") and "summary" not in run_payload and "stats" not in run_payload:
                        run_payload["summary"] = run["summary"]
                    await db.create_run_record(admin_id, run_payload)
                    count += 1
                await conn.commit()
                print(f"✅ Migrated {count} runs ({skipped} skipped)")
            except Exception as e:
                print(f"❌ Failed to migrate runs: {e}")
        else:
            print("ℹ️  No runs.json found — skipping")

        # 3. Migrate trade_history.json
        th_file = os.path.join(_ROOT, "trade_history.json")
        if os.path.exists(th_file):
            try:
                with open(th_file, encoding="utf-8") as f:
                    th = json.load(f)
                count = 0
                skipped = 0
                if isinstance(th, dict):
                    items = [(trade_date, data) for trade_date, data in th.items()]
                else:
                    items = []
                    for entry in th:
                        trade_date = entry.get("date", entry.get("trade_date", _now_iso()))
                        items.append((trade_date, entry))

                for trade_date, data in items:
                    exists = await _record_exists(
                        conn,
                        "SELECT 1 FROM trade_history WHERE user_id = ? AND trade_date = ? LIMIT 1",
                        (admin_id, trade_date),
                    )
                    if exists:
                        skipped += 1
                        continue
                    await conn.execute(
                        "INSERT INTO trade_history (user_id, trade_date, data) VALUES (?, ?, ?)",
                        (admin_id, trade_date, json.dumps(data)),
                    )
                    count += 1
                await conn.commit()
                print(f"✅ Migrated {count} trade history entries ({skipped} skipped)")
            except Exception as e:
                print(f"❌ Failed to migrate trade history: {e}")
        else:
            print("ℹ️  No trade_history.json found — skipping")

        # 4. Migrate scalp_trades.json
        scalp_file = os.path.join(_ROOT, "scalp_trades.json")
        if os.path.exists(scalp_file):
            try:
                with open(scalp_file, encoding="utf-8") as f:
                    scalp = json.load(f)
                count = 0
                skipped = 0
                for trade in scalp:
                    created = trade.get("closed_at", trade.get("created_at", _now_iso()))
                    trade_blob = json.dumps(trade, sort_keys=True)
                    exists = await _record_exists(
                        conn,
                        "SELECT 1 FROM scalp_trades WHERE user_id = ? AND created_at = ? AND trade_data = ? LIMIT 1",
                        (admin_id, created, trade_blob),
                    )
                    if exists:
                        skipped += 1
                        continue
                    await conn.execute(
                        "INSERT INTO scalp_trades (user_id, trade_data, created_at) VALUES (?, ?, ?)",
                        (admin_id, trade_blob, created),
                    )
                    count += 1
                await conn.commit()
                print(f"✅ Migrated {count} scalp trades ({skipped} skipped)")
            except Exception as e:
                print(f"❌ Failed to migrate scalp trades: {e}")
        else:
            print("ℹ️  No scalp_trades.json found — skipping")

        # 5. Migrate journals
        journals_dir = os.path.join(_ROOT, "journals")
        if os.path.isdir(journals_dir):
            try:
                count = 0
                skipped = 0
                for fname in sorted(os.listdir(journals_dir)):
                    if not fname.endswith(".json"):
                        continue
                    entry_date = fname.removesuffix(".json")
                    exists = await _record_exists(
                        conn,
                        "SELECT 1 FROM journals WHERE user_id = ? AND entry_date = ? LIMIT 1",
                        (admin_id, entry_date),
                    )
                    if exists:
                        skipped += 1
                        continue
                    fpath = os.path.join(journals_dir, fname)
                    with open(fpath, encoding="utf-8") as f:
                        data = json.load(f)
                    await conn.execute(
                        "INSERT INTO journals (user_id, entry_date, data) VALUES (?, ?, ?)",
                        (admin_id, entry_date, json.dumps(data)),
                    )
                    count += 1
                await conn.commit()
                print(f"✅ Migrated {count} journal entries ({skipped} skipped)")
            except Exception as e:
                print(f"❌ Failed to migrate journals: {e}")
        else:
            print("ℹ️  No journals/ directory found — skipping")

    # 6. Copy Daily Charts into per-user storage
    charts_src = os.path.join(_ROOT, "Daily Charts")
    charts_dst = os.path.join(config.USER_DATA_ROOT, str(admin_id), "charts")
    copied, skipped = _copy_tree_if_missing(charts_src, charts_dst)
    if copied or skipped:
        print(f"✅ Copied {copied} chart files to per-user storage ({skipped} skipped)")
    else:
        print("ℹ️  No Daily Charts/ directory found — skipping")

    # 7. Copy engine state files into per-user storage
    state_dst_root = os.path.join(config.USER_DATA_ROOT, str(admin_id), "engine_state")
    state_patterns = ("live_state*.json", "paper_state*.json", "paper_history*.json", "scalp_state*.json")
    state_copied = 0
    state_skipped = 0
    for pattern in state_patterns:
        for src_path in glob.glob(os.path.join(_ROOT, pattern)):
            dst_path = os.path.join(state_dst_root, os.path.basename(src_path))
            if _copy_file_if_missing(src_path, dst_path):
                state_copied += 1
            else:
                state_skipped += 1
    if state_copied or state_skipped:
        print(f"✅ Copied {state_copied} engine state files ({state_skipped} skipped)")
    else:
        print("ℹ️  No engine state files found — skipping")

    print()
    print("=" * 60)
    print("  Migration complete! Original JSON/files are preserved.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(migrate())
