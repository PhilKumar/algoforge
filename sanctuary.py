"""The Sanctuary — the owner's private journal and household ledger.

A standalone page at /sanctuary, deliberately outside the trading terminal:
its API prefix is not on the viewer shared-read list, every endpoint
requires the admin role (anyone else gets a 404, as if the page does not
exist), and the content sits behind a second password that only ever
lives here as a bcrypt hash.  Unlocking opens a sliding 45-minute grant
keyed to the login session, so logging out locks the sanctuary too.

Monthly automation is lazy: recurring savings and expenses (NPS, PF, MF
SIP, school fees…) are posted for any elapsed months whenever the finance
view loads, which survives deploys without a background task.
"""

from __future__ import annotations

import asyncio
import html
import os
import re
import secrets
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

import auth
import config
import db as core_db
import sanctuary_content
import sanctuary_db
import sanctuary_emi
import sanctuary_statements
from image_uploads import ImageValidationError, sanitize_image

router = APIRouter()

IST = ZoneInfo("Asia/Kolkata")
UNLOCK_TTL_SECONDS = 45 * 60
ACTION_CLASS = "sanctuary"
ALERT_WINDOW_DAYS = 7
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PHOTO_PATH_RE = re.compile(r"^\d{4}/\d{2}/[A-Za-z0-9_\-]+\.(jpg|png|webp)$")

ENTRY_KINDS = ("note", "event", "blog", "emotion", "family", "friends", "achievement", "photo")

# Unlock attempts: in-memory failure tally, cleared on success.  Eight wrong
# tries buys a fifteen-minute wait — gentle for one owner, hostile to a guess.
_unlock_failures: dict[int, list[float]] = {}
_FAILURE_LIMIT = 8
_FAILURE_WINDOW = 15 * 60

DEFAULT_CATEGORIES = [
    {"name": "Milk", "emoji": "🥛", "kind": "expense", "quick": True},
    {"name": "Autorickshaw", "emoji": "🛺", "kind": "expense", "quick": True},
    {"name": "EB Bill", "emoji": "⚡", "kind": "expense", "quick": True},
    {"name": "Groceries", "emoji": "🧺", "kind": "expense", "quick": True},
    {"name": "Eatables", "emoji": "🍪", "kind": "expense", "quick": True},
    {"name": "Food & Dining", "emoji": "🍛", "kind": "expense", "quick": True},
    {"name": "Fuel — Car", "emoji": "⛽", "kind": "expense", "quick": True},
    {"name": "Fuel — Bike", "emoji": "🏍️", "kind": "expense", "quick": True},
    {"name": "Music Class", "emoji": "🎵", "kind": "expense", "quick": False},
    {"name": "School Fees", "emoji": "🎒", "kind": "expense", "quick": False},
    {"name": "Household Repair", "emoji": "🔧", "kind": "expense", "quick": False},
    {"name": "New Items", "emoji": "🛍️", "kind": "expense", "quick": False},
    {"name": "Mobile & Internet", "emoji": "📶", "kind": "expense", "quick": False},
    {"name": "Medical", "emoji": "💊", "kind": "expense", "quick": False},
    {"name": "Giving", "emoji": "🕊️", "kind": "expense", "quick": False},
    {"name": "Travel", "emoji": "🚌", "kind": "expense", "quick": False},
    {"name": "Other", "emoji": "🌱", "kind": "expense", "quick": False},
    {"name": "NPS", "emoji": "🌳", "kind": "saving", "quick": False},
    {"name": "PF", "emoji": "🌳", "kind": "saving", "quick": False},
    {"name": "MF SIP", "emoji": "🌿", "kind": "saving", "quick": False},
    {"name": "RD / FD", "emoji": "🏦", "kind": "saving", "quick": False},
    {"name": "Gold", "emoji": "🪙", "kind": "saving", "quick": False},
]


def _today_ist() -> date:
    return datetime.now(IST).date()


def _month_key(day: date) -> str:
    return day.strftime("%Y-%m")


def _month_bounds(month: str) -> tuple[str, str]:
    start = date.fromisoformat(f"{month}-01")
    end = sanctuary_emi._add_months(start, 1) - timedelta(days=1)
    return start.isoformat(), end.isoformat()


def _months_between(start: str, end: str) -> list[str]:
    """Inclusive YYYY-MM keys from start to end."""
    try:
        cursor = date.fromisoformat(f"{start}-01")
        stop = date.fromisoformat(f"{end}-01")
    except ValueError:
        return []
    months = []
    while cursor <= stop and len(months) < 600:
        months.append(_month_key(cursor))
        cursor = sanctuary_emi._add_months(cursor, 1)
    return months


# ── Gates ────────────────────────────────────────────────────────


async def _admin_user(request: Request) -> dict:
    """The sanctuary exists only for the owner; everyone else sees a 404."""
    user = await auth.get_current_user(request)
    if str(user.get("role") or "").lower() != "admin":
        raise HTTPException(status_code=404, detail="Not found")
    return user


async def _unlocked_user(request: Request) -> dict:
    user = await _admin_user(request)
    token = auth.get_session_token(request)
    session_key = auth.session_storage_key(token)
    if not await core_db.has_action_grant(int(user["id"]), session_key, ACTION_CLASS):
        raise HTTPException(status_code=423, detail="sanctuary_locked")
    # Sliding idle window: presence keeps the door open, absence closes it.
    expires_at = (datetime.now(ZoneInfo("UTC")) + timedelta(seconds=UNLOCK_TTL_SECONDS)).isoformat()
    await core_db.grant_action_class(int(user["id"]), session_key, ACTION_CLASS, expires_at)
    return user


async def _password_hash(user_id: int) -> str:
    return await sanctuary_db.get_state(user_id, "password_hash", "")


def _too_many_failures(user_id: int) -> bool:
    now = time.monotonic()
    attempts = [t for t in _unlock_failures.get(user_id, []) if now - t < _FAILURE_WINDOW]
    _unlock_failures[user_id] = attempts
    return len(attempts) >= _FAILURE_LIMIT


# ── Page ─────────────────────────────────────────────────────────


@router.get("/sanctuary", response_class=HTMLResponse)
async def sanctuary_page(request: Request):
    token = auth.get_session_token(request)
    session = await auth.validate_session(token)
    user = await core_db.get_user_by_id(session["user_id"]) if session else None
    if not user or not user.get("is_active"):
        return RedirectResponse("/app", status_code=307)
    if str(user.get("role") or "").lower() != "admin":
        return HTMLResponse("<h1>Not found</h1>", status_code=404)
    page_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sanctuary.html")
    with open(page_path, encoding="utf-8") as handle:
        return HTMLResponse(handle.read())


# ── Lock, unlock, setup ──────────────────────────────────────────


@router.get("/api/sanctuary/status")
async def sanctuary_status(request: Request, user: dict = Depends(_admin_user)):
    token = auth.get_session_token(request)
    unlocked = await core_db.has_action_grant(int(user["id"]), auth.session_storage_key(token), ACTION_CLASS)
    return {
        "setup": bool(await _password_hash(int(user["id"]))),
        "unlocked": bool(unlocked),
        "today": _today_ist().isoformat(),
    }


@router.post("/api/sanctuary/setup")
async def sanctuary_setup(request: Request, user: dict = Depends(_admin_user)):
    payload = await request.json()
    password = str(payload.get("password") or "")
    if await _password_hash(int(user["id"])):
        raise HTTPException(status_code=409, detail="Sanctuary password is already set")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Use at least 8 characters")
    await sanctuary_db.set_state(int(user["id"]), "password_hash", auth.hash_password(password))
    return await _grant_unlock(request, user)


@router.post("/api/sanctuary/unlock")
async def sanctuary_unlock(request: Request, user: dict = Depends(_admin_user)):
    user_id = int(user["id"])
    if _too_many_failures(user_id):
        raise HTTPException(status_code=429, detail="Too many tries — rest a while and come back")
    payload = await request.json()
    password = str(payload.get("password") or "")
    stored = await _password_hash(user_id)
    if not stored:
        raise HTTPException(status_code=409, detail="sanctuary_not_setup")
    if not auth.verify_password(password, stored):
        _unlock_failures.setdefault(user_id, []).append(time.monotonic())
        await asyncio.sleep(0.6)
        # 403, not 401: the login session is fine, only this door stays shut —
        # the page treats 401 as "signed out" and would bounce to /app.
        raise HTTPException(status_code=403, detail="That is not the key to this garden")
    _unlock_failures.pop(user_id, None)
    return await _grant_unlock(request, user)


async def _grant_unlock(request: Request, user: dict) -> dict:
    token = auth.get_session_token(request)
    expires_at = (datetime.now(ZoneInfo("UTC")) + timedelta(seconds=UNLOCK_TTL_SECONDS)).isoformat()
    await core_db.grant_action_class(int(user["id"]), auth.session_storage_key(token), ACTION_CLASS, expires_at)
    return {"unlocked": True, "ttl_seconds": UNLOCK_TTL_SECONDS}


@router.post("/api/sanctuary/lock")
async def sanctuary_lock(request: Request, user: dict = Depends(_admin_user)):
    token = auth.get_session_token(request)
    session_key = auth.session_storage_key(token)
    expired = datetime.now(ZoneInfo("UTC")).isoformat()
    await core_db.grant_action_class(int(user["id"]), session_key, ACTION_CLASS, expired)
    return {"unlocked": False}


@router.post("/api/sanctuary/change-password")
async def sanctuary_change_password(request: Request, user: dict = Depends(_unlocked_user)):
    payload = await request.json()
    current = str(payload.get("current") or "")
    new = str(payload.get("new") or "")
    stored = await _password_hash(int(user["id"]))
    if not auth.verify_password(current, stored):
        raise HTTPException(status_code=403, detail="Current password is wrong")
    if len(new) < 8:
        raise HTTPException(status_code=400, detail="Use at least 8 characters")
    await sanctuary_db.set_state(int(user["id"]), "password_hash", auth.hash_password(new))
    return {"ok": True}


# ── Daily verse and quote ────────────────────────────────────────


def _pick_quote(day: date) -> dict:
    text, author = sanctuary_content.QUOTES[day.toordinal() % len(sanctuary_content.QUOTES)]
    return {"text": text, "author": author}


def _fallback_verse(day: date) -> dict:
    ref, text = sanctuary_content.FALLBACK_VERSES[day.toordinal() % len(sanctuary_content.FALLBACK_VERSES)]
    return {"reference": ref, "text": text, "version": "WEB", "permalink": "", "fallback": True}


async def _fetch_votd() -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            response = await client.get(sanctuary_content.VOTD_URL)
            response.raise_for_status()
            votd = response.json().get("votd") or {}
        text = html.unescape(re.sub(r"<[^>]+>", "", votd.get("text") or "")).strip()
        reference = html.unescape(votd.get("display_ref") or votd.get("reference") or "").strip()
        if not text or not reference:
            return None
        return {
            "reference": reference,
            "text": text,
            "version": "NIV",
            "permalink": html.unescape(votd.get("permalink") or ""),
            "fallback": False,
        }
    except Exception:
        return None


@router.get("/api/sanctuary/daily")
async def sanctuary_daily(user: dict = Depends(_unlocked_user)):
    user_id = int(user["id"])
    today = _today_ist()
    cache_key = f"daily:{today.isoformat()}"
    cached = await sanctuary_db.get_json_state(user_id, cache_key, None)
    if cached:
        return cached
    verse = await _fetch_votd()
    payload = {
        "date": today.isoformat(),
        "verse": verse or _fallback_verse(today),
        "quote": _pick_quote(today),
    }
    if verse:  # only cache the real feed; a fallback day retries on next load
        await sanctuary_db.set_json_state(user_id, cache_key, payload)
    return payload


# ── Journal ──────────────────────────────────────────────────────


def _clean_entry_fields(payload: dict, partial: bool = False) -> dict:
    fields: dict = {}
    if "entry_date" in payload or not partial:
        entry_date = str(payload.get("entry_date") or _today_ist().isoformat())
        if not _DATE_RE.match(entry_date):
            raise HTTPException(status_code=400, detail="Bad entry_date")
        fields["entry_date"] = entry_date
    if "kind" in payload or not partial:
        kind = str(payload.get("kind") or "note")
        if kind not in ENTRY_KINDS:
            raise HTTPException(status_code=400, detail="Bad kind")
        fields["kind"] = kind
    for key, limit in (("title", 300), ("body", 20000), ("music", 500)):
        if key in payload or not partial:
            fields[key] = str(payload.get(key) or "")[:limit]
    if "mood" in payload:
        mood = payload.get("mood")
        fields["mood"] = int(mood) if mood is not None and str(mood).isdigit() else None
        if fields["mood"] is not None and not 1 <= fields["mood"] <= 5:
            raise HTTPException(status_code=400, detail="Mood is 1–5")
    if "photos" in payload:
        photos = payload.get("photos") or []
        if not isinstance(photos, list) or len(photos) > 20:
            raise HTTPException(status_code=400, detail="Bad photos list")
        cleaned = []
        for photo in photos:
            file_id = str((photo or {}).get("file") or "")
            if not _PHOTO_PATH_RE.match(file_id):
                raise HTTPException(status_code=400, detail="Bad photo reference")
            cleaned.append({"file": file_id, "caption": str((photo or {}).get("caption") or "")[:300]})
        fields["photos"] = cleaned
    return fields


@router.get("/api/sanctuary/journal")
async def journal_list(
    month: str | None = None,
    kind: str | None = None,
    q: str | None = None,
    user: dict = Depends(_unlocked_user),
):
    if month and not _MONTH_RE.match(month):
        raise HTTPException(status_code=400, detail="Bad month")
    if kind and kind not in ENTRY_KINDS:
        raise HTTPException(status_code=400, detail="Bad kind")
    entries = await sanctuary_db.list_entries(int(user["id"]), month=month, kind=kind, query=q)
    return {"entries": entries}


@router.post("/api/sanctuary/journal")
async def journal_create(request: Request, user: dict = Depends(_unlocked_user)):
    payload = await request.json()
    fields = _clean_entry_fields(payload)
    entry_id = await sanctuary_db.create_entry(int(user["id"]), fields)
    if fields.get("mood"):
        await sanctuary_db.upsert_mood(int(user["id"]), fields["entry_date"], fields["mood"])
    return {"id": entry_id}


@router.put("/api/sanctuary/journal/{entry_id}")
async def journal_update(entry_id: int, request: Request, user: dict = Depends(_unlocked_user)):
    payload = await request.json()
    fields = _clean_entry_fields(payload, partial=True)
    if not await sanctuary_db.update_entry(int(user["id"]), entry_id, fields):
        raise HTTPException(status_code=404, detail="No such entry")
    return {"ok": True}


@router.delete("/api/sanctuary/journal/{entry_id}")
async def journal_delete(entry_id: int, user: dict = Depends(_unlocked_user)):
    entry = await sanctuary_db.delete_entry(int(user["id"]), entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="No such entry")
    for photo in entry.get("photos", []):
        _remove_photo_file(int(user["id"]), photo.get("file", ""))
    return {"ok": True}


@router.post("/api/sanctuary/mood")
async def mood_set(request: Request, user: dict = Depends(_unlocked_user)):
    payload = await request.json()
    mood_date = str(payload.get("date") or _today_ist().isoformat())
    mood = payload.get("mood")
    if not _DATE_RE.match(mood_date) or not isinstance(mood, int) or not 1 <= mood <= 5:
        raise HTTPException(status_code=400, detail="Bad mood payload")
    await sanctuary_db.upsert_mood(int(user["id"]), mood_date, mood, str(payload.get("note") or "")[:300])
    return {"ok": True}


@router.get("/api/sanctuary/moods")
async def moods_get(month: str, user: dict = Depends(_unlocked_user)):
    if not _MONTH_RE.match(month):
        raise HTTPException(status_code=400, detail="Bad month")
    start, end = _month_bounds(month)
    return {"moods": await sanctuary_db.moods_for_range(int(user["id"]), start, end)}


@router.get("/api/sanctuary/songs")
async def songs_get(user: dict = Depends(_unlocked_user)):
    return {"songs": await sanctuary_db.get_json_state(int(user["id"]), "songs", [])}


@router.put("/api/sanctuary/songs")
async def songs_put(request: Request, user: dict = Depends(_unlocked_user)):
    payload = await request.json()
    songs = payload.get("songs")
    if not isinstance(songs, list) or len(songs) > 500:
        raise HTTPException(status_code=400, detail="Bad songs list")
    cleaned = [
        {
            "title": str((song or {}).get("title") or "")[:200],
            "artist": str((song or {}).get("artist") or "")[:200],
            "link": str((song or {}).get("link") or "")[:500],
            "note": str((song or {}).get("note") or "")[:300],
        }
        for song in songs
    ]
    await sanctuary_db.set_json_state(int(user["id"]), "songs", cleaned)
    return {"ok": True}


# ── Photos ───────────────────────────────────────────────────────


def _photo_root(user_id: int) -> str:
    return os.path.join(config.USER_DATA_ROOT, str(int(user_id)), "sanctuary")


def _remove_photo_file(user_id: int, file_id: str) -> None:
    if not _PHOTO_PATH_RE.match(file_id or ""):
        return
    path = os.path.join(_photo_root(user_id), file_id)
    try:
        os.remove(path)
    except OSError:
        pass


async def _read_upload(file: UploadFile) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="File too large (max 10 MB)")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/api/sanctuary/photos")
async def photo_upload(file: UploadFile, user: dict = Depends(_unlocked_user)):
    data = await _read_upload(file)
    try:
        cleaned = await asyncio.to_thread(sanitize_image, data, file.content_type or "")
    except ImageValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    today = _today_ist()
    rel_dir = today.strftime("%Y/%m")
    directory = os.path.join(_photo_root(int(user["id"])), rel_dir)
    os.makedirs(directory, exist_ok=True)
    name = f"{today.strftime('%d')}-{secrets.token_hex(8)}{cleaned.extension}"
    fd = os.open(os.path.join(directory, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(cleaned.data)
    return {"file": f"{rel_dir}/{name}"}


@router.get("/api/sanctuary/photos/{file_path:path}")
async def photo_serve(file_path: str, user: dict = Depends(_unlocked_user)):
    if not _PHOTO_PATH_RE.match(file_path):
        raise HTTPException(status_code=404, detail="Not found")
    root = os.path.realpath(_photo_root(int(user["id"])))
    full = os.path.realpath(os.path.join(root, file_path))
    if not full.startswith(root + os.sep) or not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(full)


# ── Finance: categories, months, ledger ──────────────────────────


async def _get_categories(user_id: int) -> list[dict]:
    categories = await sanctuary_db.get_json_state(user_id, "categories", None)
    if categories is None:
        categories = DEFAULT_CATEGORIES
        await sanctuary_db.set_json_state(user_id, "categories", categories)
    return categories


@router.put("/api/sanctuary/finance/categories")
async def categories_put(request: Request, user: dict = Depends(_unlocked_user)):
    payload = await request.json()
    categories = payload.get("categories")
    if not isinstance(categories, list) or not 1 <= len(categories) <= 100:
        raise HTTPException(status_code=400, detail="Bad categories list")
    cleaned = []
    for category in categories:
        name = str((category or {}).get("name") or "").strip()[:60]
        if not name:
            continue
        cleaned.append(
            {
                "name": name,
                "emoji": str((category or {}).get("emoji") or "🌱")[:8],
                "kind": "saving" if (category or {}).get("kind") == "saving" else "expense",
                "quick": bool((category or {}).get("quick")),
            }
        )
    if not cleaned:
        raise HTTPException(status_code=400, detail="Bad categories list")
    await sanctuary_db.set_json_state(int(user["id"]), "categories", cleaned)
    return {"categories": cleaned}


@router.put("/api/sanctuary/finance/month")
async def month_put(request: Request, user: dict = Depends(_unlocked_user)):
    payload = await request.json()
    month = str(payload.get("month") or "")
    if not _MONTH_RE.match(month):
        raise HTTPException(status_code=400, detail="Bad month")
    months = await sanctuary_db.get_json_state(int(user["id"]), "months", {})
    entry = months.get(month) or {}
    if "salary" in payload:
        entry["salary"] = max(0.0, float(payload.get("salary") or 0))
    if "note" in payload:
        entry["note"] = str(payload.get("note") or "")[:1000]
    months[month] = entry
    await sanctuary_db.set_json_state(int(user["id"]), "months", months)
    if payload.get("make_default") and "salary" in entry:
        await sanctuary_db.set_state(int(user["id"]), "salary_default", str(entry["salary"]))
    return {"ok": True}


@router.post("/api/sanctuary/finance/entry")
async def ledger_add(request: Request, user: dict = Depends(_unlocked_user)):
    payload = await request.json()
    entry_date = str(payload.get("entry_date") or _today_ist().isoformat())
    if not _DATE_RE.match(entry_date):
        raise HTTPException(status_code=400, detail="Bad date")
    try:
        amount = round(float(payload.get("amount")), 2)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Bad amount") from None
    if not 0 < amount <= 100_000_000:
        raise HTTPException(status_code=400, detail="Bad amount")
    row_id = await sanctuary_db.add_ledger(
        int(user["id"]),
        {
            "entry_date": entry_date,
            "category": str(payload.get("category") or "Other")[:60],
            "amount": amount,
            "note": str(payload.get("note") or "")[:500],
            "source": "manual",
        },
    )
    return {"id": row_id}


@router.delete("/api/sanctuary/finance/entry/{row_id}")
async def ledger_delete(row_id: int, user: dict = Depends(_unlocked_user)):
    if not await sanctuary_db.delete_ledger_row(int(user["id"]), row_id):
        raise HTTPException(status_code=404, detail="No such entry")
    return {"ok": True}


# ── Bank statements (the ledger's backfill and its monthly feed) ─


@router.post("/api/sanctuary/statements/parse")
async def statement_parse(file: UploadFile, user: dict = Depends(_unlocked_user)):
    """Parse an uploaded statement into a preview. Nothing posts here."""
    user_id = int(user["id"])
    blob = await _read_upload(file)
    result = await asyncio.to_thread(sanctuary_statements.parse_statement, file.filename or "", blob)
    if result.get("status") != "ok":
        return result
    user_rules = await sanctuary_db.get_json_state(user_id, "stmt_rules", [])
    for row in result["rows"]:
        row["category"] = sanctuary_statements.categorise(row["note"], user_rules)
    existing = await sanctuary_db.existing_ledger_refs(user_id, [r["ref_id"] for r in result["rows"]])
    result["already_posted"] = len(existing)
    result["new_count"] = len(result["rows"]) - len(existing)
    for row in result["rows"]:
        row["posted"] = row["ref_id"] in existing
    return result


@router.post("/api/sanctuary/statements/commit")
async def statement_commit(request: Request, user: dict = Depends(_unlocked_user)):
    user_id = int(user["id"])
    payload = await request.json()
    rows = payload.get("rows")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 5000:
        raise HTTPException(status_code=400, detail="Bad statement rows")
    cleaned = []
    for row in rows:
        row = row or {}
        entry_date = str(row.get("entry_date") or "")
        ref_id = str(row.get("ref_id") or "")
        try:
            amount = round(float(row.get("amount")), 2)
        except (TypeError, ValueError):
            continue
        if not _DATE_RE.match(entry_date) or not ref_id.startswith("stmt:"):
            continue
        if not 0 < amount <= 100_000_000:
            continue
        cleaned.append(
            {
                "entry_date": entry_date,
                "category": str(row.get("category") or "Uncategorised")[:60],
                "amount": amount,
                "note": str(row.get("note") or "")[:500],
                "source": "statement",
                "ref_id": ref_id[:120],
            }
        )
    added = await sanctuary_db.add_ledger_many(user_id, cleaned)
    return {"added": added, "skipped": len(cleaned) - added}


@router.get("/api/sanctuary/statements/review")
async def statement_review(user: dict = Depends(_unlocked_user)):
    """The unsorted rows, grouped by who was paid — the review row's feed."""
    rows = await sanctuary_db.uncategorised_ledger(int(user["id"]))
    groups: dict[str, dict] = {}
    for row in rows:
        key = sanctuary_statements.payee_key(row["note"])
        group = groups.setdefault(key, {"match": key, "count": 0, "total": 0.0, "sample": row["note"]})
        group["count"] += 1
        group["total"] = round(group["total"] + row["amount"], 2)
    ordered = sorted(groups.values(), key=lambda g: -g["total"])
    return {"uncategorised": len(rows), "groups": ordered[:120]}


@router.post("/api/sanctuary/statements/rule")
async def statement_rule(request: Request, user: dict = Depends(_unlocked_user)):
    """Teach a rule: this match means this category, now and from now on."""
    user_id = int(user["id"])
    payload = await request.json()
    match = str(payload.get("match") or "").strip()[:80]
    # A handle-shaped match ("hepzibah08@") is stored by its name stem, so
    # the rule also catches the truncated forms a narration prints.
    if match.endswith("@"):
        match = match[:-1]
    category = str(payload.get("category") or "").strip()[:60]
    if len(match) < 2 or not category:
        raise HTTPException(status_code=400, detail="Need a match and a category")
    rules = await sanctuary_db.get_json_state(user_id, "stmt_rules", [])
    rules = [r for r in rules if str(r.get("match", "")).lower() != match.lower()]
    rules.insert(0, {"match": match, "category": category})
    await sanctuary_db.set_json_state(user_id, "stmt_rules", rules[:400])
    categories = await _get_categories(user_id)
    if category not in {c["name"] for c in categories} and len(categories) < 100:
        categories.append({"name": category, "emoji": "🏷️", "kind": "expense", "quick": False})
        await sanctuary_db.set_json_state(user_id, "categories", categories)
    moved = await sanctuary_db.recategorise_uncategorised(user_id, match, category)
    return {"moved": moved, "rules": len(rules)}


# ── Recurring items (NPS, PF, MF SIP, fees…) ─────────────────────


async def _materialize_recurring(user_id: int) -> None:
    """Post every recurring item for each elapsed month, exactly once."""
    items = await sanctuary_db.get_json_state(user_id, "recurring", [])
    today = _today_ist()
    current = _month_key(today)
    for item in items:
        if not item.get("active", True):
            continue
        start = item.get("start_month") or current
        for month in _months_between(start, current):
            day = min(max(int(item.get("day", 1)), 1), 28)
            if month == current and today.day < day:
                continue
            ref = f"rec:{item.get('id')}:{month}"
            if await sanctuary_db.ledger_ref_exists(user_id, ref):
                continue
            await sanctuary_db.add_ledger(
                user_id,
                {
                    "entry_date": f"{month}-{day:02d}",
                    "category": str(item.get("category") or item.get("name") or "Other")[:60],
                    "amount": round(float(item.get("amount") or 0), 2),
                    "note": str(item.get("name") or ""),
                    "source": "recurring",
                    "ref_id": ref,
                },
            )


@router.put("/api/sanctuary/finance/recurring")
async def recurring_put(request: Request, user: dict = Depends(_unlocked_user)):
    payload = await request.json()
    items = payload.get("recurring")
    if not isinstance(items, list) or len(items) > 100:
        raise HTTPException(status_code=400, detail="Bad recurring list")
    cleaned = []
    for item in items:
        name = str((item or {}).get("name") or "").strip()[:80]
        try:
            amount = round(float((item or {}).get("amount")), 2)
        except (TypeError, ValueError):
            continue
        if not name or not 0 < amount <= 100_000_000:
            continue
        start_month = str((item or {}).get("start_month") or "")
        cleaned.append(
            {
                "id": str((item or {}).get("id") or secrets.token_hex(4)),
                "name": name,
                "amount": amount,
                "day": min(max(int((item or {}).get("day") or 1), 1), 28),
                "category": str((item or {}).get("category") or name)[:60],
                "start_month": start_month if _MONTH_RE.match(start_month) else _month_key(_today_ist()),
                "active": bool((item or {}).get("active", True)),
            }
        )
    await sanctuary_db.set_json_state(int(user["id"]), "recurring", cleaned)
    await _materialize_recurring(int(user["id"]))
    return {"recurring": cleaned}


# ── Loans and EMI schedules ──────────────────────────────────────


def _clean_loan_fields(payload: dict) -> dict:
    fields: dict = {}
    if "name" in payload:
        fields["name"] = str(payload.get("name") or "").strip()[:80]
        if not fields["name"]:
            raise HTTPException(status_code=400, detail="Loan needs a name")
    for key, limit in (("lender", 80), ("note", 500), ("account_no", 60), ("details", 4000)):
        if key in payload:
            fields[key] = str(payload.get(key) or "")[:limit]
    if "emi_amount" in payload:
        try:
            fields["emi_amount"] = max(0.0, round(float(payload.get("emi_amount") or 0), 2))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Bad EMI amount") from None
    if "due_day" in payload:
        fields["due_day"] = min(max(int(payload.get("due_day") or 5), 1), 28)
    if "start_date" in payload:
        start_date = str(payload.get("start_date") or "")
        if start_date and not _DATE_RE.match(start_date):
            raise HTTPException(status_code=400, detail="Bad start date")
        fields["start_date"] = start_date
    if "active" in payload:
        fields["active"] = 1 if payload.get("active") else 0
    return fields


@router.get("/api/sanctuary/loans")
async def loans_list(user: dict = Depends(_unlocked_user)):
    loans = await sanctuary_db.list_loans(int(user["id"]))
    today = _today_ist().isoformat()
    for loan in loans:
        emis = await sanctuary_db.emis_for_loan(int(user["id"]), loan["id"])
        remaining = [e for e in emis if not e["paid_on"] and e["due_date"] >= today]
        overdue = [e for e in emis if not e["paid_on"] and e["due_date"] < today]
        loan["schedule_count"] = len(emis)
        loan["paid_count"] = sum(1 for e in emis if e["paid_on"])
        loan["overdue_count"] = len(overdue)
        loan["remaining_amount"] = round(sum(e["amount"] for e in remaining + overdue), 2)
        upcoming = min((e["due_date"] for e in remaining), default="")
        loan["next_due"] = min((e["due_date"] for e in overdue), default="") or upcoming
        loan["outstanding"] = next(
            (e["outstanding"] for e in reversed(emis) if e["paid_on"] and e["outstanding"] is not None),
            emis[0]["outstanding"] if emis and emis[0]["outstanding"] is not None else None,
        )
    return {"loans": loans}


@router.post("/api/sanctuary/loans")
async def loan_create(request: Request, user: dict = Depends(_unlocked_user)):
    payload = await request.json()
    payload.setdefault("name", "")
    fields = _clean_loan_fields(payload)
    loan_id = await sanctuary_db.create_loan(int(user["id"]), fields)
    return {"id": loan_id}


@router.put("/api/sanctuary/loans/{loan_id}")
async def loan_update(loan_id: int, request: Request, user: dict = Depends(_unlocked_user)):
    fields = _clean_loan_fields(await request.json())
    if not fields or not await sanctuary_db.update_loan(int(user["id"]), loan_id, fields):
        raise HTTPException(status_code=404, detail="No such loan")
    return {"ok": True}


@router.delete("/api/sanctuary/loans/{loan_id}")
async def loan_delete(loan_id: int, user: dict = Depends(_unlocked_user)):
    if not await sanctuary_db.delete_loan(int(user["id"]), loan_id):
        raise HTTPException(status_code=404, detail="No such loan")
    return {"ok": True}


@router.get("/api/sanctuary/loans/{loan_id}/schedule")
async def schedule_get(loan_id: int, user: dict = Depends(_unlocked_user)):
    if not await sanctuary_db.get_loan(int(user["id"]), loan_id):
        raise HTTPException(status_code=404, detail="No such loan")
    return {"schedule": await sanctuary_db.emis_for_loan(int(user["id"]), loan_id)}


@router.post("/api/sanctuary/loans/{loan_id}/schedule/parse")
async def schedule_parse(
    loan_id: int,
    file: UploadFile,
    request: Request,
    user: dict = Depends(_unlocked_user),
):
    loan = await sanctuary_db.get_loan(int(user["id"]), loan_id)
    if not loan:
        raise HTTPException(status_code=404, detail="No such loan")
    form = await request.form()
    first_due_raw = str(form.get("first_due") or "")
    first_due = date.fromisoformat(first_due_raw) if _DATE_RE.match(first_due_raw) else None
    password = str(form.get("pdf_password") or "") or None
    blob = await _read_upload(file)
    result = await asyncio.to_thread(
        sanctuary_emi.parse_emi_document,
        file.filename or "",
        blob,
        loan.get("due_day") or 5,
        first_due,
        password,
    )
    return result


@router.post("/api/sanctuary/loans/{loan_id}/schedule/commit")
async def schedule_commit(loan_id: int, request: Request, user: dict = Depends(_unlocked_user)):
    loan = await sanctuary_db.get_loan(int(user["id"]), loan_id)
    if not loan:
        raise HTTPException(status_code=404, detail="No such loan")
    payload = await request.json()
    rows = payload.get("rows")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 600:
        raise HTTPException(status_code=400, detail="Bad schedule rows")
    cleaned = []
    for row in rows:
        due = str((row or {}).get("due_date") or "")
        try:
            amount = round(float((row or {}).get("amount")), 2)
        except (TypeError, ValueError):
            continue
        if not _DATE_RE.match(due) or not 0 < amount <= 100_000_000:
            continue

        def _optional(key: str, source=row):
            value = (source or {}).get(key)
            try:
                return round(float(value), 2) if value is not None else None
            except (TypeError, ValueError):
                return None

        cleaned.append(
            {
                "due_date": due,
                "amount": amount,
                "principal_part": _optional("principal_part"),
                "interest_part": _optional("interest_part"),
                "outstanding": _optional("outstanding"),
            }
        )
    if not cleaned:
        raise HTTPException(status_code=400, detail="No valid rows to commit")
    count = await sanctuary_db.replace_schedule(int(user["id"]), loan_id, cleaned)
    amounts = sorted(row["amount"] for row in cleaned)
    median_amount = amounts[len(amounts) // 2]
    days = sorted(int(row["due_date"][8:10]) for row in cleaned)
    await sanctuary_db.update_loan(
        int(user["id"]),
        loan_id,
        {"emi_amount": median_amount, "due_day": min(days[len(days) // 2], 28)},
    )
    return {"count": count}


@router.post("/api/sanctuary/loans/{loan_id}/schedule/generate")
async def schedule_generate(loan_id: int, request: Request, user: dict = Depends(_unlocked_user)):
    """Build a flat schedule when there is no sheet to upload."""
    loan = await sanctuary_db.get_loan(int(user["id"]), loan_id)
    if not loan:
        raise HTTPException(status_code=404, detail="No such loan")
    payload = await request.json()
    first_due_raw = str(payload.get("first_due") or "")
    if not _DATE_RE.match(first_due_raw):
        raise HTTPException(status_code=400, detail="Bad first due date")
    try:
        months = int(payload.get("months"))
        amount = round(float(payload.get("amount") or loan.get("emi_amount") or 0), 2)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Bad months or amount") from None
    if not 1 <= months <= 600 or not 0 < amount <= 100_000_000:
        raise HTTPException(status_code=400, detail="Bad months or amount")
    first_due = date.fromisoformat(first_due_raw)
    rows = [{"due_date": sanctuary_emi._add_months(first_due, i).isoformat(), "amount": amount} for i in range(months)]
    count = await sanctuary_db.replace_schedule(int(user["id"]), loan_id, rows)
    await sanctuary_db.update_loan(int(user["id"]), loan_id, {"emi_amount": amount, "due_day": min(first_due.day, 28)})
    return {"count": count}


@router.post("/api/sanctuary/loans/{loan_id}/schedule/settle-past")
async def schedule_settle_past(loan_id: int, user: dict = Depends(_unlocked_user)):
    """After uploading an old schedule: mark every past installment paid."""
    if not await sanctuary_db.get_loan(int(user["id"]), loan_id):
        raise HTTPException(status_code=404, detail="No such loan")
    count = await sanctuary_db.settle_past_emis(int(user["id"]), loan_id, _today_ist().isoformat())
    return {"count": count}


@router.post("/api/sanctuary/emis/{emi_id}/paid")
async def emi_mark_paid(emi_id: int, request: Request, user: dict = Depends(_unlocked_user)):
    payload = await request.json()
    paid_on = str(payload.get("paid_on") or _today_ist().isoformat())
    if not _DATE_RE.match(paid_on):
        raise HTTPException(status_code=400, detail="Bad date")
    if not await sanctuary_db.set_emi_paid(int(user["id"]), emi_id, paid_on):
        raise HTTPException(status_code=404, detail="No such EMI")
    return {"ok": True}


@router.post("/api/sanctuary/emis/{emi_id}/unpaid")
async def emi_mark_unpaid(emi_id: int, user: dict = Depends(_unlocked_user)):
    if not await sanctuary_db.set_emi_paid(int(user["id"]), emi_id, ""):
        raise HTTPException(status_code=404, detail="No such EMI")
    return {"ok": True}


# ── Important info notes ─────────────────────────────────────────


@router.get("/api/sanctuary/notes")
async def notes_get(user: dict = Depends(_unlocked_user)):
    return {"notes": await sanctuary_db.get_json_state(int(user["id"]), "notes", [])}


@router.put("/api/sanctuary/notes")
async def notes_put(request: Request, user: dict = Depends(_unlocked_user)):
    payload = await request.json()
    notes = payload.get("notes")
    if not isinstance(notes, list) or len(notes) > 200:
        raise HTTPException(status_code=400, detail="Bad notes list")
    cleaned = [
        {
            "id": str((note or {}).get("id") or secrets.token_hex(4)),
            "title": str((note or {}).get("title") or "")[:200],
            "body": str((note or {}).get("body") or "")[:10000],
            "updated_at": _today_ist().isoformat(),
        }
        for note in notes
    ]
    await sanctuary_db.set_json_state(int(user["id"]), "notes", cleaned)
    return {"ok": True}


# ── The month view (one call feeds the whole finance tab) ────────


@router.get("/api/sanctuary/finance")
async def finance_month(month: str | None = None, user: dict = Depends(_unlocked_user)):
    user_id = int(user["id"])
    today = _today_ist()
    month = month if month and _MONTH_RE.match(month) else _month_key(today)
    await _materialize_recurring(user_id)

    categories = await _get_categories(user_id)
    saving_names = {c["name"] for c in categories if c["kind"] == "saving"}
    ledger = await sanctuary_db.list_ledger(user_id, month)
    start, end = _month_bounds(month)
    emis = await sanctuary_db.list_emis(user_id, start, end)

    months_state = await sanctuary_db.get_json_state(user_id, "months", {})
    default_salary = float(await sanctuary_db.get_state(user_id, "salary_default", "0") or 0)
    salary = float((months_state.get(month) or {}).get("salary", default_salary))

    excluded = saving_names | {"Self transfer"}
    spent = sum(r["amount"] for r in ledger if r["category"] not in excluded)
    saved = sum(r["amount"] for r in ledger if r["category"] in saving_names)
    emi_total = sum(e["amount"] for e in emis)

    by_category: dict[str, float] = {}
    for row in ledger:
        by_category[row["category"]] = by_category.get(row["category"], 0) + row["amount"]
    for emi in emis:
        label = f"EMI · {emi['loan_name']}"
        by_category[label] = by_category.get(label, 0) + emi["amount"]

    alert_through = (today + timedelta(days=ALERT_WINDOW_DAYS)).isoformat()
    alerts = [
        {
            **emi,
            "overdue": emi["due_date"] < today.isoformat(),
        }
        for emi in await sanctuary_db.unpaid_emis_through(user_id, alert_through)
    ]

    trend_months = []
    cursor = date.fromisoformat(f"{month}-01")
    for _ in range(6):
        trend_months.append(_month_key(cursor))
        cursor = sanctuary_emi._add_months(cursor, -1)
    trend_months.reverse()
    ledger_trend = await sanctuary_db.ledger_month_trend(user_id, trend_months)
    emi_rows = await sanctuary_db.list_emis(user_id, f"{trend_months[0]}-01", _month_bounds(trend_months[-1])[1])
    trend = []
    for key in trend_months:
        emi_sum = sum(e["amount"] for e in emi_rows if e["due_date"].startswith(key))
        trend.append({"month": key, "total": round(ledger_trend.get(key, 0) + emi_sum, 2)})

    return {
        "month": month,
        "today": today.isoformat(),
        "salary": salary,
        "salary_is_default": month not in months_state or "salary" not in (months_state.get(month) or {}),
        "note": (months_state.get(month) or {}).get("note", ""),
        "totals": {
            "spent": round(spent + emi_total, 2),
            "expenses": round(spent, 2),
            "emis": round(emi_total, 2),
            "saved": round(saved, 2),
            "left": round(salary - spent - emi_total - saved, 2),
        },
        "by_category": {k: round(v, 2) for k, v in sorted(by_category.items(), key=lambda kv: -kv[1])},
        "ledger": ledger,
        "emis": emis,
        "alerts": alerts,
        "categories": categories,
        "recurring": await sanctuary_db.get_json_state(user_id, "recurring", []),
        "trend": trend,
    }
