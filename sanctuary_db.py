"""Data access for the Sanctuary page — the owner's private journal and ledger.

Every row is keyed to the owning user id; the tables are created by db.py's
schema list and cleared by account deletion alongside every other user-owned
table.  Amount arithmetic happens in SQL where possible so month summaries
stay consistent with the rows they summarise.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import aiosqlite

import config


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── State (settings + small JSON collections) ────────────────────


async def get_state(user_id: int, key: str, default: str = "") -> str:
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            "SELECT value FROM sanctuary_state WHERE user_id = ? AND key = ?",
            (int(user_id), key),
        )
        row = await cursor.fetchone()
        return row[0] if row else default


async def set_state(user_id: int, key: str, value: str) -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            """INSERT INTO sanctuary_state (user_id, key, value, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id, key)
               DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (int(user_id), key, value, _now_iso()),
        )
        await db.commit()


async def get_json_state(user_id: int, key: str, default):
    raw = await get_state(user_id, key, "")
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


async def set_json_state(user_id: int, key: str, value) -> None:
    await set_state(user_id, key, json.dumps(value, ensure_ascii=False))


# ── Journal entries ──────────────────────────────────────────────


def _entry_from_row(row: aiosqlite.Row) -> dict:
    entry = dict(row)
    try:
        entry["photos"] = json.loads(entry.get("photos") or "[]")
    except (ValueError, TypeError):
        entry["photos"] = []
    return entry


async def list_entries(
    user_id: int,
    month: str | None = None,
    kind: str | None = None,
    query: str | None = None,
    limit: int = 200,
) -> list[dict]:
    sql = "SELECT * FROM sanctuary_entries WHERE user_id = ?"
    params: list = [int(user_id)]
    if month:
        sql += " AND entry_date LIKE ?"
        params.append(f"{month}%")
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    if query:
        sql += " AND (title LIKE ? OR body LIKE ? OR music LIKE ?)"
        needle = f"%{query}%"
        params.extend([needle, needle, needle])
    sql += " ORDER BY entry_date DESC, id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 500)))
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(sql, params)
        return [_entry_from_row(row) for row in await cursor.fetchall()]


async def get_entry(user_id: int, entry_id: int) -> dict | None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM sanctuary_entries WHERE user_id = ? AND id = ?",
            (int(user_id), int(entry_id)),
        )
        row = await cursor.fetchone()
        return _entry_from_row(row) if row else None


async def create_entry(user_id: int, fields: dict) -> int:
    now = _now_iso()
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO sanctuary_entries
               (user_id, entry_date, kind, title, body, mood, music, photos, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(user_id),
                fields["entry_date"],
                fields.get("kind", "note"),
                fields.get("title", ""),
                fields.get("body", ""),
                fields.get("mood"),
                fields.get("music", ""),
                json.dumps(fields.get("photos", []), ensure_ascii=False),
                now,
                now,
            ),
        )
        await db.commit()
        return int(cursor.lastrowid or 0)


async def update_entry(user_id: int, entry_id: int, fields: dict) -> bool:
    allowed = {"entry_date", "kind", "title", "body", "mood", "music", "photos"}
    sets, params = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        sets.append(f"{key} = ?")
        params.append(json.dumps(value, ensure_ascii=False) if key == "photos" else value)
    if not sets:
        return False
    sets.append("updated_at = ?")
    params.extend([_now_iso(), int(user_id), int(entry_id)])
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            f"UPDATE sanctuary_entries SET {', '.join(sets)} WHERE user_id = ? AND id = ?",  # nosec B608
            params,
        )
        await db.commit()
        return cursor.rowcount > 0


async def delete_entry(user_id: int, entry_id: int) -> dict | None:
    """Delete and return the entry so the caller can remove its photo files."""
    entry = await get_entry(user_id, entry_id)
    if entry is None:
        return None
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "DELETE FROM sanctuary_entries WHERE user_id = ? AND id = ?",
            (int(user_id), int(entry_id)),
        )
        await db.commit()
    return entry


# ── Moods ────────────────────────────────────────────────────────


async def upsert_mood(user_id: int, mood_date: str, mood: int, note: str = "") -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            """INSERT INTO sanctuary_moods (user_id, mood_date, mood, note)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id, mood_date)
               DO UPDATE SET mood = excluded.mood, note = excluded.note""",
            (int(user_id), mood_date, int(mood), note),
        )
        await db.commit()


async def moods_for_range(user_id: int, start: str, end: str) -> dict[str, dict]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT mood_date, mood, note FROM sanctuary_moods
               WHERE user_id = ? AND mood_date >= ? AND mood_date <= ?""",
            (int(user_id), start, end),
        )
        return {row["mood_date"]: dict(row) for row in await cursor.fetchall()}


# ── Ledger ───────────────────────────────────────────────────────


async def add_ledger(user_id: int, fields: dict) -> int:
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO sanctuary_ledger
               (user_id, entry_date, category, amount, note, source, ref_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(user_id),
                fields["entry_date"],
                fields.get("category", "Other"),
                float(fields.get("amount", 0)),
                fields.get("note", ""),
                fields.get("source", "manual"),
                fields.get("ref_id", ""),
                _now_iso(),
            ),
        )
        await db.commit()
        return int(cursor.lastrowid or 0)


async def delete_ledger_row(user_id: int, row_id: int) -> bool:
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM sanctuary_ledger WHERE user_id = ? AND id = ?",
            (int(user_id), int(row_id)),
        )
        await db.commit()
        return cursor.rowcount > 0


async def list_ledger(user_id: int, month: str) -> list[dict]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT * FROM sanctuary_ledger
               WHERE user_id = ? AND entry_date LIKE ?
               ORDER BY entry_date DESC, id DESC""",
            (int(user_id), f"{month}%"),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def ledger_ref_exists(user_id: int, ref_id: str) -> bool:
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            "SELECT 1 FROM sanctuary_ledger WHERE user_id = ? AND ref_id = ? LIMIT 1",
            (int(user_id), ref_id),
        )
        return await cursor.fetchone() is not None


async def ledger_month_trend(user_id: int, months: list[str]) -> dict[str, float]:
    """Total ledger amount per requested YYYY-MM month."""
    if not months:
        return {}
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        placeholders = ",".join("?" for _ in months)
        cursor = await db.execute(
            f"""SELECT substr(entry_date, 1, 7) AS month, SUM(amount) AS total
                FROM sanctuary_ledger
                WHERE user_id = ? AND substr(entry_date, 1, 7) IN ({placeholders})
                GROUP BY month""",  # nosec B608
            [int(user_id), *months],
        )
        return {row["month"]: float(row["total"] or 0) for row in await cursor.fetchall()}


# ── Loans and EMI schedules ──────────────────────────────────────


async def list_loans(user_id: int, include_inactive: bool = True) -> list[dict]:
    sql = "SELECT * FROM sanctuary_loans WHERE user_id = ?"
    if not include_inactive:
        sql += " AND active = 1"
    sql += " ORDER BY active DESC, id"
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(sql, (int(user_id),))
        return [dict(row) for row in await cursor.fetchall()]


async def get_loan(user_id: int, loan_id: int) -> dict | None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM sanctuary_loans WHERE user_id = ? AND id = ?",
            (int(user_id), int(loan_id)),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def create_loan(user_id: int, fields: dict) -> int:
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO sanctuary_loans
               (user_id, name, lender, emi_amount, due_day, start_date, note, account_no, details, active, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(user_id),
                fields["name"],
                fields.get("lender", ""),
                float(fields.get("emi_amount", 0)),
                int(fields.get("due_day", 5)),
                fields.get("start_date", ""),
                fields.get("note", ""),
                fields.get("account_no", ""),
                fields.get("details", ""),
                1 if fields.get("active", True) else 0,
                _now_iso(),
            ),
        )
        await db.commit()
        return int(cursor.lastrowid or 0)


async def update_loan(user_id: int, loan_id: int, fields: dict) -> bool:
    allowed = {"name", "lender", "emi_amount", "due_day", "start_date", "note", "account_no", "details", "active"}
    sets, params = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        sets.append(f"{key} = ?")
        params.append(value)
    if not sets:
        return False
    params.extend([int(user_id), int(loan_id)])
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            f"UPDATE sanctuary_loans SET {', '.join(sets)} WHERE user_id = ? AND id = ?",  # nosec B608
            params,
        )
        await db.commit()
        return cursor.rowcount > 0


async def delete_loan(user_id: int, loan_id: int) -> bool:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "DELETE FROM sanctuary_emis WHERE user_id = ? AND loan_id = ?",
            (int(user_id), int(loan_id)),
        )
        cursor = await db.execute(
            "DELETE FROM sanctuary_loans WHERE user_id = ? AND id = ?",
            (int(user_id), int(loan_id)),
        )
        await db.commit()
        return cursor.rowcount > 0


async def replace_schedule(user_id: int, loan_id: int, rows: list[dict]) -> int:
    """Swap in a freshly parsed schedule, preserving paid marks by due date."""
    now = _now_iso()
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT due_date, paid_on FROM sanctuary_emis WHERE user_id = ? AND loan_id = ? AND paid_on != ''",
            (int(user_id), int(loan_id)),
        )
        paid_marks = {row["due_date"]: row["paid_on"] for row in await cursor.fetchall()}
        await db.execute(
            "DELETE FROM sanctuary_emis WHERE user_id = ? AND loan_id = ?",
            (int(user_id), int(loan_id)),
        )
        for row in rows:
            await db.execute(
                """INSERT INTO sanctuary_emis
                   (user_id, loan_id, due_date, amount, principal_part, interest_part,
                    outstanding, paid_on, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    int(user_id),
                    int(loan_id),
                    row["due_date"],
                    float(row.get("amount", 0)),
                    row.get("principal_part"),
                    row.get("interest_part"),
                    row.get("outstanding"),
                    paid_marks.get(row["due_date"], ""),
                    now,
                ),
            )
        await db.commit()
        return len(rows)


async def list_emis(user_id: int, start: str, end: str) -> list[dict]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT e.*, l.name AS loan_name, l.lender AS loan_lender
               FROM sanctuary_emis e JOIN sanctuary_loans l ON l.id = e.loan_id
               WHERE e.user_id = ? AND e.due_date >= ? AND e.due_date <= ?
               ORDER BY e.due_date, e.id""",
            (int(user_id), start, end),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def emis_for_loan(user_id: int, loan_id: int) -> list[dict]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM sanctuary_emis WHERE user_id = ? AND loan_id = ? ORDER BY due_date",
            (int(user_id), int(loan_id)),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def unpaid_emis_through(user_id: int, through: str) -> list[dict]:
    """Unpaid EMIs due on or before `through` — the alert feed."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT e.*, l.name AS loan_name, l.lender AS loan_lender
               FROM sanctuary_emis e JOIN sanctuary_loans l ON l.id = e.loan_id
               WHERE e.user_id = ? AND e.paid_on = '' AND e.due_date <= ? AND l.active = 1
               ORDER BY e.due_date""",
            (int(user_id), through),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def settle_past_emis(user_id: int, loan_id: int, through: str) -> int:
    """Mark every unpaid EMI due on or before `through` as paid on its due date."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            """UPDATE sanctuary_emis SET paid_on = due_date
               WHERE user_id = ? AND loan_id = ? AND paid_on = '' AND due_date <= ?""",
            (int(user_id), int(loan_id), through),
        )
        await db.commit()
        return cursor.rowcount


async def set_emi_paid(user_id: int, emi_id: int, paid_on: str) -> bool:
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE sanctuary_emis SET paid_on = ? WHERE user_id = ? AND id = ?",
            (paid_on, int(user_id), int(emi_id)),
        )
        await db.commit()
        return cursor.rowcount > 0
