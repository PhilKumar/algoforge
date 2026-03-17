#!/usr/bin/env python3
"""
migrate_to_sqlite.py — AlgoForge JSON → SQLite Migration

Migrates existing JSON flat-file data into the SQLite database.
Idempotent — safe to run multiple times (skips existing records).

Usage:
    cd /path/to/algoforge
    python scripts/migrate_to_sqlite.py
"""

import asyncio
import json
import os
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


async def migrate():
    print("=" * 60)
    print("  AlgoForge — JSON → SQLite Migration")
    print("=" * 60)
    print(f"  DB path: {config.DB_PATH}")
    print()

    # 1. Initialize database (sync schema creation)
    db._init_db_sync()
    print("✅ Database initialized")

    # 2. Create admin user
    admin = await db.get_user_by_username("admin")
    if admin:
        print(f"ℹ️  Admin user already exists (id={admin['id']})")
        admin_id = admin["id"]
    else:
        pin = os.getenv("ALGOFORGE_PIN") or os.getenv("ALGOFORGE_PASSWORD") or "123456"
        admin_name = os.getenv("ADMIN_USERNAME", "admin")
        hashed = auth.hash_password(pin)
        admin_id = await db.create_user(admin_name, hashed, role="admin")
        print(f"✅ Created admin user '{admin_name}' (id={admin_id})")

    # Use a single connection for bulk migration
    async with aiosqlite.connect(config.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row

        # 3. Migrate strategies.json
        strats_file = os.path.join(_ROOT, "strategies.json")
        if os.path.exists(strats_file):
            try:
                with open(strats_file) as f:
                    strats = json.load(f)
                cursor = await conn.execute("SELECT COUNT(*) FROM strategies WHERE user_id = ?", (admin_id,))
                existing = (await cursor.fetchone())[0]
                if existing > 0:
                    print(f"ℹ️  Strategies already migrated ({existing} records)")
                else:
                    count = 0
                    items = []
                    if isinstance(strats, dict):
                        items = [(name, cfg) for name, cfg in strats.items()]
                    elif isinstance(strats, list):
                        items = [(s.get("run_name", s.get("name", f"Strategy_{i}")), s) for i, s in enumerate(strats)]

                    for name, cfg in items:
                        cfg_copy = dict(cfg)
                        folder = cfg_copy.pop("_folder", "")
                        versions = cfg_copy.pop("_versions", [])
                        now = _now_iso()
                        await conn.execute(
                            "INSERT INTO strategies (user_id, name, folder, config, versions, created_at, updated_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (admin_id, name, folder, json.dumps(cfg_copy), json.dumps(versions), now, now),
                        )
                        count += 1
                    await conn.commit()
                    print(f"✅ Migrated {count} strategies")
            except Exception as e:
                print(f"❌ Failed to migrate strategies: {e}")
        else:
            print("ℹ️  No strategies.json found — skipping")

        # 4. Migrate runs.json
        runs_file = os.path.join(_ROOT, "runs.json")
        if os.path.exists(runs_file):
            try:
                with open(runs_file) as f:
                    runs = json.load(f)
                cursor = await conn.execute("SELECT COUNT(*) FROM runs WHERE user_id = ?", (admin_id,))
                existing = (await cursor.fetchone())[0]
                if existing > 0:
                    print(f"ℹ️  Runs already migrated ({existing} records)")
                else:
                    count = 0
                    for run in runs:
                        mode = run.get("mode", "backtest")
                        strat_name = run.get("strategy_name", run.get("name", ""))
                        cfg = json.dumps(run.get("config", {}))
                        trades = json.dumps(run.get("trades", []))
                        summary = json.dumps(run.get("summary", {}))
                        trade_count = run.get("trade_count", len(run.get("trades", [])))
                        total_pnl = run.get("total_pnl", 0)
                        created = run.get("created_at", run.get("timestamp", _now_iso()))
                        await conn.execute(
                            "INSERT INTO runs (user_id, mode, strategy_name, config, trades, summary, trade_count, total_pnl, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (admin_id, mode, strat_name, cfg, trades, summary, trade_count, total_pnl, created),
                        )
                        count += 1
                    await conn.commit()
                    print(f"✅ Migrated {count} runs")
            except Exception as e:
                print(f"❌ Failed to migrate runs: {e}")
        else:
            print("ℹ️  No runs.json found — skipping")

        # 5. Migrate trade_history.json
        th_file = os.path.join(_ROOT, "trade_history.json")
        if os.path.exists(th_file):
            try:
                with open(th_file) as f:
                    th = json.load(f)
                cursor = await conn.execute("SELECT COUNT(*) FROM trade_history WHERE user_id = ?", (admin_id,))
                existing = (await cursor.fetchone())[0]
                if existing > 0:
                    print(f"ℹ️  Trade history already migrated ({existing} records)")
                else:
                    count = 0
                    if isinstance(th, dict):
                        for trade_date, data in th.items():
                            await conn.execute(
                                "INSERT INTO trade_history (user_id, trade_date, data) VALUES (?, ?, ?)",
                                (admin_id, trade_date, json.dumps(data)),
                            )
                            count += 1
                    elif isinstance(th, list):
                        for entry in th:
                            td = entry.get("date", entry.get("trade_date", _now_iso()))
                            await conn.execute(
                                "INSERT INTO trade_history (user_id, trade_date, data) VALUES (?, ?, ?)",
                                (admin_id, td, json.dumps(entry)),
                            )
                            count += 1
                    await conn.commit()
                    print(f"✅ Migrated {count} trade history entries")
            except Exception as e:
                print(f"❌ Failed to migrate trade history: {e}")
        else:
            print("ℹ️  No trade_history.json found — skipping")

        # 6. Migrate scalp_trades.json
        scalp_file = os.path.join(_ROOT, "scalp_trades.json")
        if os.path.exists(scalp_file):
            try:
                with open(scalp_file) as f:
                    scalp = json.load(f)
                cursor = await conn.execute("SELECT COUNT(*) FROM scalp_trades WHERE user_id = ?", (admin_id,))
                existing = (await cursor.fetchone())[0]
                if existing > 0:
                    print(f"ℹ️  Scalp trades already migrated ({existing} records)")
                else:
                    count = 0
                    for trade in scalp:
                        created = trade.get("closed_at", trade.get("created_at", _now_iso()))
                        await conn.execute(
                            "INSERT INTO scalp_trades (user_id, trade_data, created_at) VALUES (?, ?, ?)",
                            (admin_id, json.dumps(trade), created),
                        )
                        count += 1
                    await conn.commit()
                    print(f"✅ Migrated {count} scalp trades")
            except Exception as e:
                print(f"❌ Failed to migrate scalp trades: {e}")
        else:
            print("ℹ️  No scalp_trades.json found — skipping")

        # 7. Migrate journals
        journals_dir = os.path.join(_ROOT, "journals")
        if os.path.isdir(journals_dir):
            try:
                cursor = await conn.execute("SELECT COUNT(*) FROM journals WHERE user_id = ?", (admin_id,))
                existing = (await cursor.fetchone())[0]
                if existing > 0:
                    print(f"ℹ️  Journals already migrated ({existing} records)")
                else:
                    count = 0
                    for fname in sorted(os.listdir(journals_dir)):
                        if not fname.endswith(".json"):
                            continue
                        fpath = os.path.join(journals_dir, fname)
                        with open(fpath) as f:
                            data = json.load(f)
                        entry_date = fname.replace(".json", "")
                        await conn.execute(
                            "INSERT INTO journals (user_id, entry_date, data) VALUES (?, ?, ?)",
                            (admin_id, entry_date, json.dumps(data)),
                        )
                        count += 1
                    await conn.commit()
                    print(f"✅ Migrated {count} journal entries")
            except Exception as e:
                print(f"❌ Failed to migrate journals: {e}")
        else:
            print("ℹ️  No journals/ directory found — skipping")

    print()
    print("=" * 60)
    print("  Migration complete! Original JSON files are preserved.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(migrate())
