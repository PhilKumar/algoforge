"""
db.py — AlgoForge SQLite Data Layer (Multi-Tenant)

Async SQLite via aiosqlite with WAL mode for concurrent reads.
All queries filter by user_id for data isolation.
"""

import logging
import sqlite3
from datetime import datetime, timezone

import aiosqlite

import config

_logger = logging.getLogger(__name__)

# Module-level: whether schema has been initialized
_initialized = False


# ── Schema (individual statements) ───────────────────────────────
_SCHEMA_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS users (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        username        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
        email           TEXT    UNIQUE,
        password_hash   TEXT    NOT NULL,
        role            TEXT    NOT NULL DEFAULT 'user',
        is_active       INTEGER NOT NULL DEFAULT 1,
        dhan_client_id  TEXT    DEFAULT '',
        dhan_access_token TEXT  DEFAULT '',
        dhan_pin        TEXT    DEFAULT '',
        dhan_totp_secret TEXT   DEFAULT '',
        created_at      TEXT    NOT NULL,
        last_login      TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS sessions (
        token       TEXT    PRIMARY KEY,
        user_id     INTEGER NOT NULL,
        expires_at  TEXT    NOT NULL,
        created_at  TEXT    NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )""",
    """CREATE TABLE IF NOT EXISTS strategies (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        name        TEXT    NOT NULL,
        folder      TEXT    DEFAULT '',
        config      TEXT    NOT NULL DEFAULT '{}',
        version     INTEGER DEFAULT 1,
        versions    TEXT    DEFAULT '[]',
        created_at  TEXT    NOT NULL,
        updated_at  TEXT    NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_strategies_user ON strategies(user_id)",
    """CREATE TABLE IF NOT EXISTS runs (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         INTEGER NOT NULL,
        mode            TEXT    NOT NULL DEFAULT 'backtest',
        strategy_name   TEXT    DEFAULT '',
        config          TEXT    DEFAULT '{}',
        trades          TEXT    DEFAULT '[]',
        summary         TEXT    DEFAULT '{}',
        trade_count     INTEGER DEFAULT 0,
        total_pnl       REAL    DEFAULT 0.0,
        created_at      TEXT    NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_runs_user ON runs(user_id)",
    """CREATE TABLE IF NOT EXISTS trade_history (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        trade_date  TEXT    NOT NULL,
        data        TEXT    NOT NULL DEFAULT '{}',
        FOREIGN KEY (user_id) REFERENCES users(id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_trade_history_user ON trade_history(user_id)",
    """CREATE TABLE IF NOT EXISTS journals (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        entry_date  TEXT    NOT NULL,
        data        TEXT    NOT NULL DEFAULT '{}',
        FOREIGN KEY (user_id) REFERENCES users(id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_journals_user ON journals(user_id)",
    """CREATE TABLE IF NOT EXISTS scalp_trades (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        trade_data  TEXT    NOT NULL DEFAULT '{}',
        created_at  TEXT    NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_scalp_trades_user ON scalp_trades(user_id)",
]


def _init_db_sync():
    """Synchronous schema initialization (runs once at import/startup)."""
    global _initialized
    if _initialized:
        return
    db_path = config.DB_PATH
    _logger.info(f"[DB] Initializing SQLite schema at {db_path}")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    for stmt in _SCHEMA_STATEMENTS:
        conn.execute(stmt)
    conn.commit()
    conn.close()
    _initialized = True
    _logger.info("[DB] Schema initialized")


async def init_db():
    """Initialize the database (sync schema creation, safe to call multiple times)."""
    _init_db_sync()


async def _connect() -> aiosqlite.Connection:
    """Open a fresh async connection with WAL mode."""
    db = await aiosqlite.connect(config.DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode = WAL")
    await db.execute("PRAGMA foreign_keys = ON")
    return db


async def get_db() -> aiosqlite.Connection:
    """Get a new database connection. Caller should close when done, or use _connect()."""
    if not _initialized:
        _init_db_sync()
    return await _connect()


async def close_db():
    """No-op for connection-per-call pattern. Kept for API compatibility."""
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_SENSITIVE_USER_FIELDS = frozenset(
    {
        "dhan_client_id",
        "dhan_access_token",
        "dhan_pin",
        "dhan_totp_secret",
    }
)


def _encrypt_user_fields(fields: dict) -> dict:
    """Encrypt broker credential fields before storing them."""
    if not fields:
        return fields
    encrypted = dict(fields)
    for key in _SENSITIVE_USER_FIELDS & encrypted.keys():
        value = encrypted.get(key)
        if value in (None, ""):
            encrypted[key] = ""
            continue
        from auth import encrypt_value

        encrypted[key] = encrypt_value(str(value))
    return encrypted


def _decrypt_user_row(row: aiosqlite.Row | sqlite3.Row | dict | None) -> dict | None:
    """Decrypt broker credential fields on read, tolerating legacy plaintext rows."""
    if not row:
        return None
    user = dict(row)
    for key in _SENSITIVE_USER_FIELDS:
        value = user.get(key)
        if not value:
            user[key] = ""
            continue
        from auth import decrypt_value

        user[key] = decrypt_value(value)
    return user


# ── Users ────────────────────────────────────────────────────────


async def create_user(username: str, password_hash: str, role: str = "user", email: str | None = None) -> int:
    """Create a new user and return their id."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        now = _now_iso()
        cursor = await db.execute(
            "INSERT INTO users (username, email, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
            (username, email, password_hash, role, now),
        )
        await db.commit()
        return cursor.lastrowid


async def get_user_by_username(username: str) -> dict | None:
    """Fetch a user by username (case-insensitive)."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM users WHERE username = ?", (username,))
        row = await cursor.fetchone()
        return _decrypt_user_row(row)


async def get_user_by_id(user_id: int) -> dict | None:
    """Fetch a user by id."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = await cursor.fetchone()
        return _decrypt_user_row(row)


async def get_admin_user(preferred_username: str | None = None) -> dict | None:
    """Fetch the preferred admin account, falling back to any existing admin."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        candidates: list[str | None] = []
        if preferred_username:
            candidates.append(preferred_username)
        if (preferred_username or "").lower() != "admin":
            candidates.append("admin")
        for username in candidates:
            if not username:
                continue
            cursor = await db.execute(
                "SELECT * FROM users WHERE role = 'admin' AND username = ? COLLATE NOCASE ORDER BY id LIMIT 1",
                (username,),
            )
            row = await cursor.fetchone()
            if row:
                return _decrypt_user_row(row)
        cursor = await db.execute("SELECT * FROM users WHERE role = 'admin' ORDER BY id LIMIT 1")
        row = await cursor.fetchone()
        return _decrypt_user_row(row)


async def list_users() -> list[dict]:
    """List all users (admin use)."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, username, email, role, is_active, created_at, last_login FROM users ORDER BY id"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


_ALLOWED_USER_FIELDS = frozenset(
    {
        "username",
        "email",
        "password_hash",
        "role",
        "is_active",
        "dhan_client_id",
        "dhan_access_token",
        "dhan_pin",
        "dhan_totp_secret",
        "last_login",
    }
)


async def update_user(user_id: int, **fields) -> bool:
    """Update user fields. Pass only the fields you want to change."""
    if not fields:
        return False
    # Whitelist column names to prevent SQL injection via kwargs
    bad = set(fields) - _ALLOWED_USER_FIELDS
    if bad:
        raise ValueError(f"Invalid user fields: {bad}")
    fields = _encrypt_user_fields(fields)
    async with aiosqlite.connect(config.DB_PATH) as db:
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [user_id]
        await db.execute(f"UPDATE users SET {set_clause} WHERE id = ?", values)  # nosec B608
        await db.commit()
        return True


async def set_user_active(user_id: int, is_active: bool) -> bool:
    """Enable or disable a user account."""
    return await update_user(user_id, is_active=int(is_active))


async def update_last_login(user_id: int):
    """Update the last_login timestamp."""
    await update_user(user_id, last_login=_now_iso())


# ── Sessions ─────────────────────────────────────────────────────


async def create_session(token: str, user_id: int, expires_at: str) -> None:
    """Store a new session."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        now = _now_iso()
        await db.execute(
            "INSERT INTO sessions (token, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
            (token, user_id, expires_at, now),
        )
        await db.commit()


async def get_session(token: str) -> dict | None:
    """Get a session by token, returns None if expired or missing."""
    if not token:
        return None
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        now = _now_iso()
        cursor = await db.execute(
            "SELECT * FROM sessions WHERE token = ? AND expires_at > ?",
            (token, now),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def delete_session(token: str) -> None:
    """Remove a session."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("DELETE FROM sessions WHERE token = ?", (token,))
        await db.commit()


async def delete_sessions_for_user(user_id: int) -> int:
    """Remove all sessions for a user. Returns count deleted."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        await db.commit()
        return cursor.rowcount


async def cleanup_expired_sessions() -> int:
    """Remove all expired sessions. Returns count deleted."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        now = _now_iso()
        cursor = await db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
        await db.commit()
        return cursor.rowcount
