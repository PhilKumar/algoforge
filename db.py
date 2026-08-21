"""
db.py — PhilForge SQLite Data Layer (Multi-Tenant)

Async SQLite via aiosqlite with WAL mode for concurrent reads.
All queries filter by user_id for data isolation.
"""

import json
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
        mfa_totp_secret TEXT    DEFAULT '',
        mfa_pending_secret TEXT DEFAULT '',
        mfa_enabled     INTEGER NOT NULL DEFAULT 0,
        mfa_enrolled_at TEXT,
        mfa_last_counter INTEGER NOT NULL DEFAULT -1,
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
    """CREATE TABLE IF NOT EXISTS action_tokens (
        token_hash   TEXT    PRIMARY KEY,
        user_id      INTEGER NOT NULL,
        session_hash TEXT    NOT NULL,
        action_class TEXT    NOT NULL,
        method       TEXT    NOT NULL,
        path         TEXT    NOT NULL,
        expires_at   TEXT    NOT NULL,
        created_at   TEXT    NOT NULL,
        consumed_at  TEXT,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_action_tokens_user_expiry ON action_tokens(user_id, expires_at)",
    # One confirmation, then a window. A token is still one-shot and still
    # bound to one exact request; what this remembers is that the person at
    # this session proved themselves for a whole CLASS of action recently, so
    # the admin console stops demanding a fresh code per user it touches. Bound
    # to the session hash, so it dies with logout and never travels.
    """CREATE TABLE IF NOT EXISTS action_grants (
        user_id      INTEGER NOT NULL,
        session_hash TEXT    NOT NULL,
        action_class TEXT    NOT NULL,
        expires_at   TEXT    NOT NULL,
        created_at   TEXT    NOT NULL,
        PRIMARY KEY (user_id, session_hash, action_class),
        FOREIGN KEY (user_id) REFERENCES users(id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_action_grants_expiry ON action_grants(expires_at)",
    # Passkeys — Face ID / fingerprint sign-in. Only PUBLIC keys live here; the
    # private key never leaves the phone's secure hardware and no biometric is
    # ever transmitted, so this table holds nothing that can impersonate anyone.
    """CREATE TABLE IF NOT EXISTS passkeys (
        credential_id TEXT    PRIMARY KEY,
        user_id       INTEGER NOT NULL,
        public_key    TEXT    NOT NULL,
        sign_count    INTEGER NOT NULL DEFAULT 0,
        label         TEXT    NOT NULL DEFAULT '',
        created_at    TEXT    NOT NULL,
        last_used_at  TEXT,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_passkeys_user ON passkeys(user_id)",
    # A challenge is single-use and short-lived. Kept in the DB rather than in
    # process memory so a restart mid-ceremony fails closed instead of
    # accepting a stale one.
    """CREATE TABLE IF NOT EXISTS webauthn_challenges (
        challenge_id TEXT    PRIMARY KEY,
        user_id      INTEGER,
        purpose      TEXT    NOT NULL,
        challenge    TEXT    NOT NULL,
        expires_at   TEXT    NOT NULL,
        created_at   TEXT    NOT NULL
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
    """CREATE TABLE IF NOT EXISTS financial_plans (
        user_id      INTEGER PRIMARY KEY,
        data         TEXT    NOT NULL DEFAULT '{}',
        updated_at   TEXT    NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )""",
    """CREATE TABLE IF NOT EXISTS app_state (
        key         TEXT    PRIMARY KEY,
        value       TEXT    NOT NULL DEFAULT '',
        updated_at  TEXT    NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS scalp_trades (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        trade_data  TEXT    NOT NULL DEFAULT '{}',
        created_at  TEXT    NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_scalp_trades_user ON scalp_trades(user_id)",
    # Every Test Bench replay is kept so a mother candle only has to be run
    # once.  `query_key` is the identity of the question asked (instrument,
    # strategy, timeframe, mother, per-level, ITM steps) and is UNIQUE per
    # user, which is what lets a repeat run be recognised instead of quietly
    # producing a second row saying the same thing.
    """CREATE TABLE IF NOT EXISTS test_bench_runs (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         INTEGER NOT NULL,
        query_key       TEXT    NOT NULL,
        instrument      TEXT    NOT NULL,
        strategy        TEXT    NOT NULL,
        timeframe       TEXT    NOT NULL,
        mother_date     TEXT    NOT NULL,
        mother_timestamp TEXT   NOT NULL,
        rung_inr        REAL    NOT NULL DEFAULT 0,
        itm_steps       INTEGER NOT NULL DEFAULT 0,
        outcome         TEXT    NOT NULL DEFAULT '',
        net_pnl         REAL,
        entry_count     INTEGER NOT NULL DEFAULT 0,
        payload         TEXT    NOT NULL DEFAULT '{}',
        created_at      TEXT    NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_test_bench_query ON test_bench_runs(user_id, query_key)",
    "CREATE INDEX IF NOT EXISTS idx_test_bench_date ON test_bench_runs(user_id, mother_date)",
    """CREATE TABLE IF NOT EXISTS fib_backtest_runs (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id          INTEGER NOT NULL,
        mother_timestamp TEXT    NOT NULL,
        side             TEXT    NOT NULL,
        timeframe        TEXT    NOT NULL,
        horizon_to       TEXT    NOT NULL,
        fully_priced     INTEGER NOT NULL DEFAULT 0,
        net_pnl          REAL,
        gap_count        INTEGER NOT NULL DEFAULT 0,
        payload          TEXT    NOT NULL DEFAULT '{}',
        created_at       TEXT    NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_fib_backtest_user_date ON fib_backtest_runs(user_id, mother_timestamp)",
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
    # Existing installations pre-date application MFA. SQLite's CREATE TABLE
    # IF NOT EXISTS does not add columns, so upgrades need explicit, idempotent
    # column migrations before any authenticated request reads a user row.
    existing_user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    user_column_migrations = {
        "mfa_totp_secret": "TEXT DEFAULT ''",
        "mfa_pending_secret": "TEXT DEFAULT ''",
        "mfa_enabled": "INTEGER NOT NULL DEFAULT 0",
        "mfa_enrolled_at": "TEXT",
        "mfa_last_counter": "INTEGER NOT NULL DEFAULT -1",
    }
    for column, definition in user_column_migrations.items():
        if column not in existing_user_columns:
            conn.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")  # nosec B608
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


def _connect_sync() -> sqlite3.Connection:
    """Open a synchronous SQLite connection for thread/off-loop helpers."""
    if not _initialized:
        _init_db_sync()
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_SENSITIVE_USER_FIELDS = frozenset(
    {
        "dhan_client_id",
        "dhan_access_token",
        "dhan_pin",
        "dhan_totp_secret",
        "mfa_totp_secret",
        "mfa_pending_secret",
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
        from auth import encrypt_value, encryption_enabled

        if not encryption_enabled():
            raise RuntimeError("ENCRYPTION_KEY must be configured before saving broker credentials")

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


def get_admin_user_sync(preferred_username: str | None = None) -> dict | None:
    """Synchronous admin lookup for thread-based startup helpers."""
    with _connect_sync() as db:
        candidates: list[str | None] = []
        if preferred_username:
            candidates.append(preferred_username)
        if (preferred_username or "").lower() != "admin":
            candidates.append("admin")
        for username in candidates:
            if not username:
                continue
            cursor = db.execute(
                "SELECT * FROM users WHERE role = 'admin' AND username = ? COLLATE NOCASE ORDER BY id LIMIT 1",
                (username,),
            )
            row = cursor.fetchone()
            if row:
                return _decrypt_user_row(row)
        cursor = db.execute("SELECT * FROM users WHERE role = 'admin' ORDER BY id LIMIT 1")
        row = cursor.fetchone()
        return _decrypt_user_row(row)


async def list_users() -> list[dict]:
    """List all users (admin use)."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, username, email, role, is_active, created_at, last_login, dhan_client_id, dhan_access_token "
            "FROM users ORDER BY id"
        )
        rows = await cursor.fetchall()
        users: list[dict] = []
        for row in rows:
            full_user = _decrypt_user_row(row) or {}
            client_id = str(full_user.get("dhan_client_id", "") or "").strip()
            access_token = str(full_user.get("dhan_access_token", "") or "").strip()
            users.append(
                {
                    "id": full_user.get("id"),
                    "username": full_user.get("username"),
                    "email": full_user.get("email"),
                    "role": full_user.get("role"),
                    "is_active": full_user.get("is_active"),
                    "created_at": full_user.get("created_at"),
                    "last_login": full_user.get("last_login"),
                    "broker_configured": bool(client_id and access_token),
                    "broker_partial": bool((client_id and not access_token) or (access_token and not client_id)),
                }
            )
        return users


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
        "mfa_totp_secret",
        "mfa_pending_secret",
        "mfa_enabled",
        "mfa_enrolled_at",
        "mfa_last_counter",
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


def update_user_sync(user_id: int, **fields) -> bool:
    """Synchronous user update helper for thread/off-loop broker callbacks."""
    if not fields:
        return False
    bad = set(fields) - _ALLOWED_USER_FIELDS
    if bad:
        raise ValueError(f"Invalid user fields: {bad}")
    fields = _encrypt_user_fields(fields)
    with _connect_sync() as db:
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [user_id]
        db.execute(f"UPDATE users SET {set_clause} WHERE id = ?", values)  # nosec B608
        db.commit()
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


async def claim_mfa_counter(user_id: int, counter: int) -> bool:
    """Atomically accept a TOTP counter once, preventing code replay."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE users SET mfa_last_counter = ? WHERE id = ? AND mfa_last_counter < ?",
            (int(counter), int(user_id), int(counter)),
        )
        await db.commit()
        return cursor.rowcount == 1


async def create_action_token(
    token_hash: str,
    user_id: int,
    session_hash: str,
    action_class: str,
    method: str,
    path: str,
    expires_at: str,
) -> None:
    """Persist one short-lived, session-bound action authorization token."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        now = _now_iso()
        await db.execute("DELETE FROM action_tokens WHERE expires_at <= ? OR consumed_at IS NOT NULL", (now,))
        await db.execute(
            """INSERT INTO action_tokens
               (token_hash, user_id, session_hash, action_class, method, path, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                token_hash,
                int(user_id),
                session_hash,
                action_class,
                method.upper(),
                path,
                expires_at,
                now,
            ),
        )
        await db.commit()


async def consume_action_token(
    token_hash: str,
    user_id: int,
    session_hash: str,
    action_class: str,
    method: str,
    path: str,
) -> bool:
    """Atomically consume an exact-match action token once."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        now = _now_iso()
        cursor = await db.execute(
            """UPDATE action_tokens
               SET consumed_at = ?
               WHERE token_hash = ? AND user_id = ? AND session_hash = ?
                 AND action_class = ? AND method = ? AND path = ?
                 AND expires_at > ? AND consumed_at IS NULL""",
            (
                now,
                token_hash,
                int(user_id),
                session_hash,
                action_class,
                method.upper(),
                path,
                now,
            ),
        )
        await db.commit()
        return cursor.rowcount == 1


async def grant_action_class(user_id: int, session_hash: str, action_class: str, expires_at: str) -> None:
    """Remember that this session proved itself for a class of action."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        now = _now_iso()
        await db.execute("DELETE FROM action_grants WHERE expires_at <= ?", (now,))
        await db.execute(
            """INSERT INTO action_grants (user_id, session_hash, action_class, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id, session_hash, action_class)
               DO UPDATE SET expires_at = excluded.expires_at, created_at = excluded.created_at""",
            (int(user_id), session_hash, action_class, expires_at, now),
        )
        await db.commit()


async def has_action_grant(user_id: int, session_hash: str, action_class: str) -> bool:
    """True while this session's confirmation for that class is still good."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            """SELECT 1 FROM action_grants
               WHERE user_id = ? AND session_hash = ? AND action_class = ? AND expires_at > ?
               LIMIT 1""",
            (int(user_id), session_hash, action_class, _now_iso()),
        )
        return await cursor.fetchone() is not None


async def delete_action_grants_for_user(user_id: int) -> None:
    """Drop every remembered confirmation for a user (password reset, delete)."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("DELETE FROM action_grants WHERE user_id = ?", (int(user_id),))
        await db.commit()


async def delete_action_grants_for_session(session_hash: str) -> None:
    """Close the re-ask window for one session (logout)."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("DELETE FROM action_grants WHERE session_hash = ?", (session_hash,))
        await db.commit()


# Every table that keys rows to a user id. Deleting an account has to clear all
# of them or the next account to be handed the same id inherits the leftovers.
_USER_OWNED_TABLES: tuple[str, ...] = (
    "sessions",
    "action_tokens",
    "action_grants",
    "passkeys",
    "webauthn_challenges",
    "strategies",
    "runs",
    "trade_history",
    "journals",
    "financial_plans",
    "scalp_trades",
    "test_bench_runs",
    "fib_backtest_runs",
)


async def delete_user_and_data(user_id: int) -> dict[str, int]:
    """Delete a user and every row that belongs to them, in one transaction.

    Returns the row count removed per table, so the caller can say what it
    actually destroyed rather than claiming success blind. The user's broker
    credentials and MFA secrets are columns on the users row, so they go with
    it; their charts live on disk and are the caller's to remove.
    """
    removed: dict[str, int] = {}
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("BEGIN")
        try:
            for table in _USER_OWNED_TABLES:
                # nosec B608 - the table name is one of the module-level literals
                # in _USER_OWNED_TABLES; nothing from a request reaches this string,
                # and the only value is bound as a parameter.
                cursor = await db.execute(f"DELETE FROM {table} WHERE user_id = ?", (int(user_id),))  # nosec B608
                if cursor.rowcount > 0:
                    removed[table] = cursor.rowcount
            cursor = await db.execute("DELETE FROM users WHERE id = ?", (int(user_id),))
            removed["users"] = cursor.rowcount
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return removed


async def get_app_state(key: str) -> str | None:
    """Fetch one app-state value by key."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT value FROM app_state WHERE key = ? LIMIT 1", (str(key),))
        row = await cursor.fetchone()
        if not row:
            return None
        return str(row["value"])


async def delete_app_state(key: str) -> bool:
    """Forget one app-state value. True when a row was actually removed."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute("DELETE FROM app_state WHERE key = ?", (str(key),))
        await db.commit()
        return bool(cursor.rowcount)


async def set_app_state(key: str, value: str) -> None:
    """Insert or update one app-state value."""
    state_key = str(key)
    state_value = str(value)
    now = _now_iso()
    async with aiosqlite.connect(config.DB_PATH) as db:
        # ONE statement, not SELECT-then-INSERT. Two savers of the same key
        # (a Start route's forced save and the paper loop's first tick) both
        # saw no row and both inserted; the second died on the UNIQUE index.
        await db.execute(
            "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (state_key, state_value, now),
        )
        await db.commit()


def _json_loads(blob: str | None, default):
    """Parse JSON columns defensively."""
    if not blob:
        return default
    try:
        return json.loads(blob)
    except Exception:
        return default


def _json_dumps(value) -> str:
    """Serialize JSON payloads with datetime-safe fallback."""
    return json.dumps(value, default=str)


def _strategy_row_to_dict(row: aiosqlite.Row | sqlite3.Row | dict | None) -> dict | None:
    if not row:
        return None
    item = dict(row)
    config_data = _json_loads(item.get("config"), {})
    if not isinstance(config_data, dict):
        config_data = {}
    versions = _json_loads(item.get("versions"), [])
    if not isinstance(versions, list):
        versions = []

    strategy = dict(config_data)
    name = item.get("name") or strategy.get("run_name") or strategy.get("name") or "Untitled Strategy"
    strategy["id"] = item["id"]
    strategy["run_name"] = strategy.get("run_name") or name
    strategy["name"] = strategy.get("name") or name
    strategy["folder"] = item.get("folder", "") or strategy.get("folder", "")
    strategy["version"] = int(item.get("version") or strategy.get("version") or 1)
    strategy["versions"] = versions
    strategy["created_at"] = item.get("created_at") or strategy.get("created_at") or _now_iso()
    strategy["updated_at"] = item.get("updated_at") or strategy.get("updated_at") or strategy["created_at"]
    return strategy


def _strategy_to_record(strategy: dict) -> dict:
    payload = dict(strategy or {})
    payload.pop("id", None)
    payload.pop("user_id", None)

    versions = payload.pop("versions", []) or []
    version = int(payload.pop("version", 1) or 1)
    created_at = str(payload.pop("created_at", _now_iso()) or _now_iso())
    updated_at = str(payload.pop("updated_at", created_at) or created_at)
    folder = str(payload.get("folder", "") or "")
    name = str(payload.get("run_name") or payload.get("name") or "Untitled Strategy")

    return {
        "name": name,
        "folder": folder,
        "config": _json_dumps(payload),
        "version": version,
        "versions": _json_dumps(versions),
        "created_at": created_at,
        "updated_at": updated_at,
    }


async def list_strategies(user_id: int) -> list[dict]:
    """Return all strategies for a user in save order."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM strategies WHERE user_id = ? ORDER BY id", (user_id,))
        rows = await cursor.fetchall()
        return [_strategy_row_to_dict(row) for row in rows]


async def get_strategy(user_id: int, strategy_id: int) -> dict | None:
    """Fetch one strategy belonging to a user."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM strategies WHERE user_id = ? AND id = ?", (user_id, strategy_id))
        row = await cursor.fetchone()
        return _strategy_row_to_dict(row)


async def create_strategy_record(user_id: int, strategy: dict) -> dict:
    """Insert a strategy and return the stored record."""
    record = _strategy_to_record(strategy)
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO strategies (user_id, name, folder, config, version, versions, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                record["name"],
                record["folder"],
                record["config"],
                record["version"],
                record["versions"],
                record["created_at"],
                record["updated_at"],
            ),
        )
        await db.commit()
        strategy_id = cursor.lastrowid
    return await get_strategy(user_id, strategy_id)


async def replace_strategy_record(user_id: int, strategy_id: int, strategy: dict) -> dict | None:
    """Replace a strategy record with the provided payload."""
    record = _strategy_to_record(strategy)
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            """
            UPDATE strategies
            SET name = ?, folder = ?, config = ?, version = ?, versions = ?, created_at = ?, updated_at = ?
            WHERE user_id = ? AND id = ?
            """,
            (
                record["name"],
                record["folder"],
                record["config"],
                record["version"],
                record["versions"],
                record["created_at"],
                record["updated_at"],
                user_id,
                strategy_id,
            ),
        )
        await db.commit()
        if cursor.rowcount <= 0:
            return None
    return await get_strategy(user_id, strategy_id)


async def delete_strategy_record(user_id: int, strategy_id: int) -> bool:
    """Delete one strategy owned by the user."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute("DELETE FROM strategies WHERE user_id = ? AND id = ?", (user_id, strategy_id))
        await db.commit()
        return cursor.rowcount > 0


def _run_row_to_dict(row: aiosqlite.Row | sqlite3.Row | dict | None) -> dict | None:
    if not row:
        return None
    item = dict(row)
    config_data = _json_loads(item.get("config"), {})
    if not isinstance(config_data, dict):
        config_data = {}
    trades = _json_loads(item.get("trades"), [])
    if not isinstance(trades, list):
        trades = []
    summary = _json_loads(item.get("summary"), {})
    if not isinstance(summary, dict):
        summary = {}

    run = dict(config_data)
    strategy_name = item.get("strategy_name") or run.get("run_name") or run.get("strategy_name") or f"Run #{item['id']}"
    if "stats" not in run and summary:
        run["stats"] = summary
    run["id"] = item["id"]
    run["mode"] = item.get("mode") or run.get("mode") or "backtest"
    run["run_name"] = run.get("run_name") or strategy_name
    run["strategy_name"] = run.get("strategy_name") or strategy_name
    run["trade_count"] = int(item.get("trade_count") or run.get("trade_count") or len(trades))
    run["total_pnl"] = float(item.get("total_pnl") if item.get("total_pnl") is not None else run.get("total_pnl", 0))
    run["created_at"] = item.get("created_at") or run.get("created_at") or _now_iso()
    run["trades"] = trades
    return run


def _run_to_record(run: dict) -> dict:
    payload = dict(run or {})
    payload.pop("id", None)
    payload.pop("user_id", None)

    trades = payload.pop("trades", []) or []
    mode = str(payload.get("mode", "backtest") or "backtest")
    created_at = str(payload.get("created_at", _now_iso()) or _now_iso())
    trade_count = int(payload.get("trade_count", len(trades)) or 0)
    total_pnl = float(payload.get("total_pnl", 0) or 0)
    strategy_name = str(payload.get("run_name") or payload.get("strategy_name") or payload.get("name") or "")
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        summary = payload.get("stats", {}) if isinstance(payload.get("stats"), dict) else {}

    return {
        "mode": mode,
        "strategy_name": strategy_name,
        "config": _json_dumps(payload),
        "trades": _json_dumps(trades),
        "summary": _json_dumps(summary),
        "trade_count": trade_count,
        "total_pnl": total_pnl,
        "created_at": created_at,
    }


async def list_runs(user_id: int) -> list[dict]:
    """Return all saved runs for a user."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM runs WHERE user_id = ? ORDER BY id", (user_id,))
        rows = await cursor.fetchall()
        return [_run_row_to_dict(row) for row in rows]


async def get_run(user_id: int, run_id: int) -> dict | None:
    """Fetch one saved run for a user."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM runs WHERE user_id = ? AND id = ?", (user_id, run_id))
        row = await cursor.fetchone()
        return _run_row_to_dict(row)


async def create_run_record(user_id: int, run: dict) -> dict:
    """Insert a run and return the stored record."""
    record = _run_to_record(run)
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO runs (user_id, mode, strategy_name, config, trades, summary, trade_count, total_pnl, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                record["mode"],
                record["strategy_name"],
                record["config"],
                record["trades"],
                record["summary"],
                record["trade_count"],
                record["total_pnl"],
                record["created_at"],
            ),
        )
        await db.commit()
        run_id = cursor.lastrowid
    return await get_run(user_id, run_id)


async def replace_run_record(user_id: int, run_id: int, run: dict) -> dict | None:
    """Replace a saved run with the provided payload."""
    record = _run_to_record(run)
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            """
            UPDATE runs
            SET mode = ?, strategy_name = ?, config = ?, trades = ?, summary = ?, trade_count = ?, total_pnl = ?, created_at = ?
            WHERE user_id = ? AND id = ?
            """,
            (
                record["mode"],
                record["strategy_name"],
                record["config"],
                record["trades"],
                record["summary"],
                record["trade_count"],
                record["total_pnl"],
                record["created_at"],
                user_id,
                run_id,
            ),
        )
        await db.commit()
        if cursor.rowcount <= 0:
            return None
    return await get_run(user_id, run_id)


async def delete_run_record(user_id: int, run_id: int) -> bool:
    """Delete one run owned by the user."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute("DELETE FROM runs WHERE user_id = ? AND id = ?", (user_id, run_id))
        await db.commit()
        return cursor.rowcount > 0


async def bulk_delete_run_records(user_id: int, run_ids: list[int]) -> int:
    """Delete multiple runs for a user."""
    ids = [int(rid) for rid in run_ids]
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            f"DELETE FROM runs WHERE user_id = ? AND id IN ({placeholders})",  # nosec B608
            [user_id, *ids],
        )
        await db.commit()
        return cursor.rowcount


async def cleanup_empty_runs(user_id: int | None = None) -> int:
    """Delete empty non-backtest runs, optionally scoped to one user."""
    if user_id is None:
        sql = (
            "DELETE FROM runs WHERE mode != 'backtest' AND trade_count <= 0 "
            "AND (trades IS NULL OR trades = '' OR trades = '[]')"
        )
        params: list[object] = []
    else:
        sql = (
            "DELETE FROM runs WHERE user_id = ? AND mode != 'backtest' AND trade_count <= 0 "
            "AND (trades IS NULL OR trades = '' OR trades = '[]')"
        )
        params = [user_id]
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(sql, params)
        await db.commit()
        return cursor.rowcount


def _trade_history_data_from_row(row: aiosqlite.Row | sqlite3.Row | dict | None) -> dict | None:
    if not row:
        return None
    data = _json_loads(dict(row).get("data"), {})
    return data if isinstance(data, dict) else {}


async def list_trade_history(user_id: int) -> dict[str, dict]:
    """Return all persisted real-trade history for a user keyed by date."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT trade_date, data FROM trade_history WHERE user_id = ? ORDER BY trade_date",
            (user_id,),
        )
        rows = await cursor.fetchall()
        history: dict[str, dict] = {}
        for row in rows:
            history[str(row["trade_date"])] = _trade_history_data_from_row(row) or {}
        return history


async def get_trade_history_entry(user_id: int, trade_date: str) -> dict | None:
    """Fetch one persisted real-trade summary for a user/date."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT data FROM trade_history WHERE user_id = ? AND trade_date = ? LIMIT 1",
            (user_id, trade_date),
        )
        row = await cursor.fetchone()
        return _trade_history_data_from_row(row)


async def upsert_trade_history_entry(user_id: int, trade_date: str, data: dict) -> None:
    """Insert or replace one trade-history date for a user."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id FROM trade_history WHERE user_id = ? AND trade_date = ? LIMIT 1",
            (user_id, trade_date),
        )
        row = await cursor.fetchone()
        payload = _json_dumps(data or {})
        if row:
            await db.execute("UPDATE trade_history SET data = ? WHERE id = ?", (payload, row["id"]))
        else:
            await db.execute(
                "INSERT INTO trade_history (user_id, trade_date, data) VALUES (?, ?, ?)",
                (user_id, trade_date, payload),
            )
        await db.commit()


def list_trade_history_sync(user_id: int) -> dict[str, dict]:
    """Synchronous trade-history loader for thread-based backfill tasks."""
    with _connect_sync() as conn:
        cursor = conn.execute(
            "SELECT trade_date, data FROM trade_history WHERE user_id = ? ORDER BY trade_date",
            (user_id,),
        )
        history: dict[str, dict] = {}
        for row in cursor.fetchall():
            history[str(row["trade_date"])] = _trade_history_data_from_row(row) or {}
        return history


def upsert_trade_history_entry_sync(user_id: int, trade_date: str, data: dict) -> None:
    """Synchronous trade-history upsert for thread-based backfill tasks."""
    with _connect_sync() as conn:
        cursor = conn.execute(
            "SELECT id FROM trade_history WHERE user_id = ? AND trade_date = ? LIMIT 1",
            (user_id, trade_date),
        )
        row = cursor.fetchone()
        payload = _json_dumps(data or {})
        if row:
            conn.execute("UPDATE trade_history SET data = ? WHERE id = ?", (payload, row["id"]))
        else:
            conn.execute(
                "INSERT INTO trade_history (user_id, trade_date, data) VALUES (?, ?, ?)",
                (user_id, trade_date, payload),
            )
        conn.commit()


def clear_trade_history_sync(user_id: int) -> int:
    """Delete all real-trade history rows for a user."""
    with _connect_sync() as conn:
        cursor = conn.execute("DELETE FROM trade_history WHERE user_id = ?", (user_id,))
        conn.commit()
        return cursor.rowcount


async def list_journal_entries(user_id: int) -> list[dict]:
    """Return journal entry summaries for the journal list view."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT entry_date, data FROM journals WHERE user_id = ? ORDER BY entry_date DESC",
            (user_id,),
        )
        rows = await cursor.fetchall()
        entries: list[dict] = []
        for row in rows:
            data = _json_loads(dict(row).get("data"), {})
            if not isinstance(data, dict):
                data = {}
            entries.append(
                {
                    "date": str(row["entry_date"]),
                    "asset": data.get("asset", ""),
                    "grade": data.get("grade", ""),
                    "strategy": data.get("strategy", ""),
                }
            )
        return entries


async def get_journal_entry(user_id: int, entry_date: str) -> dict | None:
    """Fetch one journal entry for a user/date."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT data FROM journals WHERE user_id = ? AND entry_date = ? LIMIT 1",
            (user_id, entry_date),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        data = _json_loads(dict(row).get("data"), {})
        return data if isinstance(data, dict) else {}


async def upsert_journal_entry(user_id: int, entry_date: str, data: dict) -> None:
    """Insert or replace one journal entry for a user/date."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id FROM journals WHERE user_id = ? AND entry_date = ? LIMIT 1",
            (user_id, entry_date),
        )
        row = await cursor.fetchone()
        payload = _json_dumps(data or {})
        if row:
            await db.execute("UPDATE journals SET data = ? WHERE id = ?", (payload, row["id"]))
        else:
            await db.execute(
                "INSERT INTO journals (user_id, entry_date, data) VALUES (?, ?, ?)",
                (user_id, entry_date, payload),
            )
        await db.commit()


async def delete_journal_entry(user_id: int, entry_date: str) -> bool:
    """Delete one journal entry for a user/date."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM journals WHERE user_id = ? AND entry_date = ?",
            (user_id, entry_date),
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_financial_plan(user_id: int) -> dict:
    """Fetch saved financial plan for a user."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT data, updated_at FROM financial_plans WHERE user_id = ? LIMIT 1",
            (user_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return {}
        data = _json_loads(dict(row).get("data"), {})
        if not isinstance(data, dict):
            data = {}
        data["updated_at"] = row["updated_at"]
        return data


async def upsert_financial_plan(user_id: int, data: dict) -> None:
    """Insert or update one user's financial plan."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT user_id FROM financial_plans WHERE user_id = ? LIMIT 1",
            (user_id,),
        )
        row = await cursor.fetchone()
        payload = _json_dumps(data or {})
        now = _now_iso()
        if row:
            await db.execute(
                "UPDATE financial_plans SET data = ?, updated_at = ? WHERE user_id = ?",
                (payload, now, user_id),
            )
        else:
            await db.execute(
                "INSERT INTO financial_plans (user_id, data, updated_at) VALUES (?, ?, ?)",
                (user_id, payload, now),
            )
        await db.commit()


def _trade_id_from_scalp_payload(payload: dict) -> int:
    try:
        return int(payload.get("trade_id") or 0)
    except (TypeError, ValueError):
        return 0


async def list_scalp_trades(user_id: int) -> list[dict]:
    """Return persisted closed scalp trades for a user."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT trade_data FROM scalp_trades WHERE user_id = ? ORDER BY id",
            (user_id,),
        )
        rows = await cursor.fetchall()
        trades: list[dict] = []
        for row in rows:
            payload = _json_loads(dict(row).get("trade_data"), {})
            if isinstance(payload, dict):
                trades.append(payload)
        return trades


async def create_scalp_trade(user_id: int, trade: dict) -> None:
    """Persist one closed scalp trade for a user."""
    payload = dict(trade or {})
    created_at = str(
        payload.get("closed_at")
        or payload.get("exit_time")
        or payload.get("created_at")
        or payload.get("entry_time")
        or _now_iso()
    )
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT INTO scalp_trades (user_id, trade_data, created_at) VALUES (?, ?, ?)",
            (user_id, _json_dumps(payload), created_at),
        )
        await db.commit()


async def bulk_delete_scalp_trades(user_id: int, trade_ids: list[int]) -> int:
    """Delete persisted scalp trades for a user by nested trade_id."""
    ids = {int(tid) for tid in trade_ids if str(tid).strip()}
    if not ids:
        return 0
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, trade_data FROM scalp_trades WHERE user_id = ? ORDER BY id",
            (user_id,),
        )
        rows = await cursor.fetchall()
        row_ids = []
        for row in rows:
            payload = _json_loads(dict(row).get("trade_data"), {})
            if isinstance(payload, dict) and _trade_id_from_scalp_payload(payload) in ids:
                row_ids.append(int(row["id"]))
        if not row_ids:
            return 0
        placeholders = ",".join("?" for _ in row_ids)
        delete_cursor = await db.execute(
            f"DELETE FROM scalp_trades WHERE user_id = ? AND id IN ({placeholders})",  # nosec B608
            [user_id, *row_ids],
        )
        await db.commit()
        return delete_cursor.rowcount


async def delete_scalp_trade(user_id: int, trade_id: int) -> bool:
    """Delete one persisted scalp trade for a user by nested trade_id."""
    return (await bulk_delete_scalp_trades(user_id, [trade_id])) > 0


def get_max_scalp_trade_id_sync(user_id: int) -> int:
    """Return the max persisted scalp trade_id for a user."""
    max_trade_id = 0
    with _connect_sync() as conn:
        cursor = conn.execute("SELECT trade_data FROM scalp_trades WHERE user_id = ?", (user_id,))
        for row in cursor.fetchall():
            payload = _json_loads(dict(row).get("trade_data"), {})
            if isinstance(payload, dict):
                max_trade_id = max(max_trade_id, _trade_id_from_scalp_payload(payload))
    return max_trade_id


# ── Test Bench: one row per replayed mother candle ────────────────
def test_bench_query_key(
    *,
    instrument: str,
    strategy: str,
    timeframe: str,
    mother_timestamp: str,
    rung_inr: float,
    itm_steps: int,
) -> str:
    """The identity of a Test Bench question.

    Two runs with the same key would give the same answer, so the second one
    is a repeat rather than a new result.  Per-level rupees and ITM steps are
    part of it because they change the money, not just the geometry.
    """
    return "|".join(
        [
            str(instrument).upper(),
            str(strategy).lower(),
            str(timeframe).lower(),
            str(mother_timestamp),
            f"{float(rung_inr):.2f}",
            str(int(itm_steps)),
        ]
    )


async def find_test_bench_run(user_id: int, query_key: str) -> dict | None:
    """The stored run for this exact question, or None."""
    db = await get_db()
    try:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM test_bench_runs WHERE user_id = ? AND query_key = ?",
            (int(user_id), query_key),
        ) as cursor:
            row = await cursor.fetchone()
        return _test_bench_row(row) if row else None
    finally:
        await db.close()


async def save_test_bench_run(user_id: int, query_key: str, summary: dict, payload: dict) -> int:
    """Insert or refresh the stored run for this question, returning its id.

    A repeat run overwrites rather than duplicating: the query key is what
    makes two runs the same, and the newer replay is the better copy (it may
    have been priced when the older one had gaps).
    """
    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO test_bench_runs
                   (user_id, query_key, instrument, strategy, timeframe, mother_date,
                    mother_timestamp, rung_inr, itm_steps, outcome, net_pnl,
                    entry_count, payload, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(user_id, query_key) DO UPDATE SET
                   outcome = excluded.outcome,
                   net_pnl = excluded.net_pnl,
                   entry_count = excluded.entry_count,
                   payload = excluded.payload,
                   created_at = excluded.created_at""",
            (
                int(user_id),
                query_key,
                str(summary.get("instrument") or ""),
                str(summary.get("strategy") or ""),
                str(summary.get("timeframe") or ""),
                str(summary.get("mother_timestamp") or "")[:10],
                str(summary.get("mother_timestamp") or ""),
                float(summary.get("rung_inr") or 0),
                int(summary.get("itm_steps") or 0),
                str(summary.get("outcome") or ""),
                summary.get("net_pnl"),
                int(summary.get("entry_count") or 0),
                _json_dumps(payload),
                _now_iso(),
            ),
        )
        await db.commit()
        async with db.execute(
            "SELECT id FROM test_bench_runs WHERE user_id = ? AND query_key = ?",
            (int(user_id), query_key),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else 0
    finally:
        await db.close()


async def list_test_bench_runs(user_id: int, *, search: str = "", page: int = 1, per_page: int = 10) -> dict:
    """One page of stored runs, most recently RUN first.

    Ordered by created_at, not the mother's own date: replaying an old mother
    refreshes its row (save_test_bench_run bumps created_at), so the run you
    just made is always the top row — Phil's reading of "newest first".

    ``search`` matches the mother date/time, the instrument, the strategy or
    the timeframe, so typing a date finds that day's runs directly.
    """
    page = max(1, int(page))
    per_page = max(1, min(int(per_page), 100))
    term = f"%{str(search).strip().lower()}%"
    where = "WHERE user_id = ?"
    params: list = [int(user_id)]
    if str(search).strip():
        where += (
            " AND (LOWER(mother_timestamp) LIKE ? OR LOWER(instrument) LIKE ?"
            " OR LOWER(strategy) LIKE ? OR LOWER(timeframe) LIKE ? OR LOWER(outcome) LIKE ?)"
        )
        params.extend([term] * 5)
    db = await get_db()
    try:
        db.row_factory = aiosqlite.Row
        # `where` is assembled from literals only; the search term itself is a
        # bound parameter, never interpolated.
        async with db.execute(f"SELECT COUNT(*) FROM test_bench_runs {where}", params) as cursor:  # nosec B608
            total = int((await cursor.fetchone())[0])
        async with db.execute(
            f"""SELECT id, instrument, strategy, timeframe, mother_timestamp, rung_inr,
                       itm_steps, outcome, net_pnl, entry_count, created_at
                FROM test_bench_runs {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?""",  # nosec B608
            [*params, per_page, (page - 1) * per_page],
        ) as cursor:
            rows = await cursor.fetchall()
        return {
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": max(1, (total + per_page - 1) // per_page),
            "rows": [_test_bench_row(row, with_payload=False) for row in rows],
        }
    finally:
        await db.close()


async def get_test_bench_run(user_id: int, run_id: int) -> dict | None:
    """One stored run in full, payload included."""
    db = await get_db()
    try:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM test_bench_runs WHERE user_id = ? AND id = ?",
            (int(user_id), int(run_id)),
        ) as cursor:
            row = await cursor.fetchone()
        return _test_bench_row(row) if row else None
    finally:
        await db.close()


async def delete_test_bench_run(user_id: int, run_id: int) -> bool:
    db = await get_db()
    try:
        cursor = await db.execute(
            "DELETE FROM test_bench_runs WHERE user_id = ? AND id = ?", (int(user_id), int(run_id))
        )
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


def _test_bench_row(row, *, with_payload: bool = True) -> dict:
    data = dict(row)
    if with_payload:
        data["payload"] = _json_loads(data.get("payload"), {})
    else:
        data.pop("payload", None)
    return data


# ── Fib Boundary backtests: durable, user-owned replay packages ──
async def save_fib_backtest_run(user_id: int, payload: dict) -> int:
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    db = await get_db()
    try:
        cursor = await db.execute(
            """INSERT INTO fib_backtest_runs
                   (user_id, mother_timestamp, side, timeframe, horizon_to,
                    fully_priced, net_pnl, gap_count, payload, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                int(user_id),
                str((payload.get("mother") or {}).get("timestamp") or ""),
                str(payload.get("side") or ""),
                str(payload.get("timeframe") or ""),
                str(payload.get("horizon_to") or ""),
                int(bool(result.get("fully_priced"))),
                result.get("net_pnl"),
                len(result.get("data_gaps") or []),
                _json_dumps(payload),
                _now_iso(),
            ),
        )
        await db.commit()
        return int(cursor.lastrowid or 0)
    finally:
        await db.close()


async def list_fib_backtest_runs(user_id: int, limit: int = 50) -> list[dict]:
    db = await get_db()
    try:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, mother_timestamp, side, timeframe, horizon_to,
                      fully_priced, net_pnl, gap_count, created_at
               FROM fib_backtest_runs WHERE user_id = ?
               ORDER BY id DESC LIMIT ?""",
            (int(user_id), max(1, min(int(limit), 200))),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]
    finally:
        await db.close()


async def get_fib_backtest_run(user_id: int, run_id: int) -> dict | None:
    db = await get_db()
    try:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM fib_backtest_runs WHERE user_id = ? AND id = ?",
            (int(user_id), int(run_id)),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        result = dict(row)
        result["payload"] = _json_loads(result.get("payload"), {})
        return result
    finally:
        await db.close()


# ── Passkeys (Face ID / fingerprint) ─────────────────────────────
async def add_passkey(credential_id: str, user_id: int, public_key: str, sign_count: int, label: str) -> None:
    """Store one registered passkey. Public key only — never a biometric."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO passkeys (credential_id, user_id, public_key, sign_count, label, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (credential_id, int(user_id), public_key, int(sign_count), label, _now_iso()),
        )
        await db.commit()


async def get_passkey(credential_id: str) -> dict | None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM passkeys WHERE credential_id = ?", (credential_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def list_passkeys(user_id: int) -> list[dict]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT credential_id, label, created_at, last_used_at FROM passkeys WHERE user_id = ? ORDER BY created_at",
            (int(user_id),),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def touch_passkey(credential_id: str, sign_count: int) -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "UPDATE passkeys SET sign_count = ?, last_used_at = ? WHERE credential_id = ?",
            (int(sign_count), _now_iso(), credential_id),
        )
        await db.commit()


async def delete_passkey(credential_id: str, user_id: int) -> bool:
    """Remove one passkey. Scoped to the owner so an id alone is not enough."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM passkeys WHERE credential_id = ? AND user_id = ?", (credential_id, int(user_id))
        )
        await db.commit()
        return cursor.rowcount > 0


async def store_webauthn_challenge(
    challenge_id: str, user_id: int | None, purpose: str, challenge: str, expires_at: str
) -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        # Sweep expired rows on the way in; this table has no other reader.
        await db.execute("DELETE FROM webauthn_challenges WHERE expires_at < ?", (_now_iso(),))
        await db.execute(
            "INSERT OR REPLACE INTO webauthn_challenges"
            " (challenge_id, user_id, purpose, challenge, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (challenge_id, user_id, purpose, challenge, expires_at, _now_iso()),
        )
        await db.commit()


async def consume_webauthn_challenge(challenge_id: str, purpose: str) -> dict | None:
    """Take a challenge and delete it in one step, so it can never be replayed."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM webauthn_challenges WHERE challenge_id = ? AND purpose = ? AND expires_at >= ?",
            (challenge_id, purpose, _now_iso()),
        )
        row = await cursor.fetchone()
        await db.execute("DELETE FROM webauthn_challenges WHERE challenge_id = ?", (challenge_id,))
        await db.commit()
        return dict(row) if row else None
