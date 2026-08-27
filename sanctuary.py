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
import hashlib
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
import sanctuary_docs
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
# Who pays him. A credit naming one of these IS the month's pay, whatever
# category an older import gave it.
_EMPLOYER_WORDS = ("kyndryl", "ibm india")

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


# ── The Vault: identity papers, encrypted at rest ────────────────

FAMILY_ACTION_CLASS = "vault-family"
_VAULT_CATEGORIES = ("Identity", "Work", "Vehicle", "Finance", "Family", "Other")
_VAULT_CONTENT_TYPES = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    # A household's papers are not only PDFs: the tax workings arrive as
    # spreadsheets, letters as documents, notes as plain text.
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/vnd.oasis.opendocument.spreadsheet": ".ods",
    "text/plain": ".txt",
    "text/csv": ".csv",
}

# A file saved without an extension arrives typed as octet-stream or as
# nothing at all; its first bytes still say what it is.
_MAGIC = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"RIFF", "image/webp"),
    (b"\xd0\xcf\x11\xe0", "application/vnd.ms-excel"),
    (b"PK\x03\x04", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
)


def _sniff_content_type(blob: bytes, declared: str) -> str:
    if declared in _VAULT_CONTENT_TYPES:
        return declared
    for magic, kind in _MAGIC:
        if blob.startswith(magic):
            return kind
    try:
        blob[:2048].decode("utf-8")
        return "text/plain"
    except UnicodeDecodeError:
        return ""


def _vault_root(user_id: int) -> str:
    return os.path.join(config.USER_DATA_ROOT, str(int(user_id)), "vault")


def _vault_file_path(user_id: int, token: str) -> str:
    root = os.path.realpath(_vault_root(user_id))
    full = os.path.realpath(os.path.join(root, f"{token}.enc"))
    if not full.startswith(root + os.sep):
        raise HTTPException(status_code=404, detail="Not found")
    return full


def _clean_document_fields(payload: dict) -> dict:
    fields: dict = {}
    if "title" in payload:
        fields["title"] = str(payload.get("title") or "").strip()[:120]
        if not fields["title"]:
            raise HTTPException(status_code=400, detail="The document needs a name")
    if "category" in payload:
        category = str(payload.get("category") or "Other").strip()
        fields["category"] = category if category in _VAULT_CATEGORIES else "Other"
    for key, limit in (("doc_number", 120), ("note", 500), ("series", 80)):
        if key in payload:
            fields[key] = str(payload.get(key) or "").strip()[:limit]
    if "doc_date" in payload:
        doc_date = str(payload.get("doc_date") or "")
        if doc_date and not _DATE_RE.match(doc_date):
            raise HTTPException(status_code=400, detail="Bad date")
        fields["doc_date"] = doc_date
    # The number is sensitive on its own — it rests encrypted like the file.
    if fields.get("doc_number"):
        fields["doc_number"] = auth.encrypt_value(fields["doc_number"])
    return fields


def _document_view(doc: dict) -> dict:
    view = {
        k: doc[k]
        for k in (
            "id",
            "title",
            "category",
            "note",
            "series",
            "doc_date",
            "filename",
            "content_type",
            "size",
            "created_at",
        )
    }
    view["doc_number"] = auth.decrypt_value(doc.get("doc_number") or "")
    return view


@router.get("/api/sanctuary/vault")
async def vault_list(user: dict = Depends(_unlocked_user)):
    docs = [_document_view(d) for d in await sanctuary_db.list_documents(int(user["id"]))]
    return {"documents": docs, "encryption_ready": auth.encryption_enabled()}


@router.post("/api/sanctuary/vault")
async def vault_upload(file: UploadFile, request: Request, user: dict = Depends(_unlocked_user)):
    if not auth.encryption_enabled():
        raise HTTPException(
            status_code=503,
            detail="The vault refuses to store documents unencrypted — ENCRYPTION_KEY is not configured.",
        )
    declared = (file.content_type or "").split(";")[0].strip().lower()
    blob = await _read_upload(file)
    if not blob:
        raise HTTPException(status_code=400, detail="Empty file")
    content_type = _sniff_content_type(blob, declared)
    if not content_type:
        raise HTTPException(status_code=400, detail="Papers and pictures only — that file is something else")
    # The same paper offered twice — inside one folder drop, or a folder
    # re-picked after a deploy — is acknowledged, not stored again.
    content_sha = hashlib.sha1(blob, usedforsecurity=False).hexdigest()
    existing = await sanctuary_db.find_document_by_sha(int(user["id"]), content_sha)
    if not existing:
        # Rows stored before fingerprints existed: a matching name and size
        # is worth opening to compare, and either way learns its fingerprint.
        for old in await sanctuary_db.documents_without_sha(
            int(user["id"]), (file.filename or "document")[:160], len(blob)
        ):
            stored = _read_document_blob(old, int(user["id"]))
            if stored is not None:
                await sanctuary_db.set_document_sha(
                    int(user["id"]),
                    old["id"],
                    hashlib.sha1(stored, usedforsecurity=False).hexdigest(),
                )
                if stored == blob:
                    existing = old
                    break
    if existing:
        return {"id": existing["id"], "duplicate": True, "title": existing.get("title") or ""}
    encrypted = auth.encrypt_bytes(blob)
    if encrypted is None:
        raise HTTPException(status_code=503, detail="Encryption unavailable")
    form = await request.form()
    if str(form.get("auto") or "") == "1":
        # A whole folder at once: the filename and its parent already say
        # what each paper is, so nothing needs typing 150 times.
        read = sanctuary_docs.classify_document(file.filename or "", str(form.get("folder") or ""))
        payload = dict(read)
        payload["note"] = str(form.get("folder") or "")
    else:
        payload = {
            "title": form.get("title") or (file.filename or "Document"),
            "category": form.get("category"),
            "doc_number": form.get("doc_number"),
            "note": form.get("note"),
            "series": form.get("series"),
            "doc_date": form.get("doc_date"),
        }
    fields = _clean_document_fields(payload)
    token = secrets.token_hex(16)
    directory = _vault_root(int(user["id"]))
    os.makedirs(directory, exist_ok=True)
    fd = os.open(os.path.join(directory, f"{token}.enc"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(encrypted)
    fields.update(
        {
            "filename": (file.filename or "document")[:160],
            "content_type": content_type,
            "size": len(blob),
            "file_token": token,
            "content_sha": content_sha,
        }
    )
    doc_id = await sanctuary_db.create_document(int(user["id"]), fields)
    salary = None
    if content_type == "application/pdf":
        salary = await _salary_from_payslip(int(user["id"]), blob)
    return {"id": doc_id, "salary": salary}


def _previous_month(month: str) -> str:
    year, mon = int(month[:4]), int(month[5:7])
    return f"{year - 1:04d}-12" if mon == 1 else f"{year:04d}-{mon - 1:02d}"


async def _month_leftover(
    user_id: int, month: str, saving_names: set, months_state: dict, default_salary: float
) -> float:
    """What a month had left when it ended — the money that carries forward.

    His pay lands on the last days of a month, so what funds August is
    whatever July still held on the 31st. This reckons one month only, never
    a chain back to 2015: a running balance would compound every mis-filed
    row into a figure nobody could check.
    """
    ledger = await sanctuary_db.list_ledger(user_id, month)
    entry = months_state.get(month) or {}
    salary = float(entry.get("salary", default_salary))
    if "salary" not in entry:
        salary = _salary_from_ledger(ledger) or salary
    excluded = saving_names | {"Self transfer"}
    outgo = [r for r in ledger if r["source"] != "statement-in"]
    spent = sum(r["amount"] for r in outgo if r["category"] not in excluded)
    saved = sum(r["amount"] for r in outgo if r["category"] in saving_names)
    start, end = _month_bounds(month)
    emi_total = sum(e["amount"] for e in await sanctuary_db.list_emis(user_id, start, end) if not e.get("paid_on"))
    return round(salary - spent - emi_total - saved, 2)


def _salary_from_ledger(ledger: list[dict]) -> float:
    """What the bank saw arrive from his employer this month.

    Pay can land in two credits (arrears, a shift allowance paid apart), so
    the month's pay credits are summed rather than picking the largest. Only
    money coming IN counts: a debit to an employer is not pay. Rows imported
    before the employer was a known payer still read as Uncategorised, so
    the narration is consulted too and nothing needs re-importing.
    """
    return round(
        sum(
            row["amount"]
            for row in ledger
            if row.get("source") == "statement-in"
            and (
                row.get("category") == "Salary"
                or any(word in (row.get("note") or "").lower() for word in _EMPLOYER_WORDS)
            )
        ),
        2,
    )


async def _salary_from_payslip(user_id: int, blob: bytes) -> dict | None:
    """A payslip knows the month it belongs to and what actually arrived.

    Whatever it says fills that month's salary, so the tiles stop waiting to
    be told what the paper already knows. A month the owner set by hand is
    left alone — his correction outranks the reader.
    """
    try:
        read = await asyncio.to_thread(_read_payslip_pdf, blob)
    except Exception:  # noqa: BLE001 - an unreadable upload is not an error
        return None
    if not read:
        return None
    months = await sanctuary_db.get_json_state(user_id, "months", {})
    entry = dict(months.get(read["month"]) or {})
    if entry.get("salary_source") == "manual":
        return None
    entry["salary"] = read["net"]
    entry["salary_source"] = "payslip"
    if read.get("employer"):
        entry["salary_note"] = read["employer"]
    months[read["month"]] = entry
    await sanctuary_db.set_json_state(user_id, "months", months)
    return {"month": read["month"], "net": read["net"], "employer": read.get("employer", "")}


def _read_payslip_pdf(blob: bytes) -> dict | None:
    import io

    import pdfplumber

    with pdfplumber.open(io.BytesIO(blob)) as pdf:
        text = pdf.pages[0].extract_text() or ""
    return sanctuary_docs.read_payslip(text)


def _read_document_blob(doc: dict, user_id: int) -> bytes | None:
    """The stored paper, decrypted — or None if it is gone or unreadable."""
    try:
        full = _vault_file_path(user_id, doc.get("file_token") or "missing")
    except HTTPException:
        return None
    if not os.path.isfile(full):
        return None
    with open(full, "rb") as handle:
        return auth.decrypt_bytes(handle.read())


def _serve_document(doc: dict, user_id: int):
    blob = _read_document_blob(doc, user_id)
    if blob is None:
        raise HTTPException(status_code=404, detail="The file is gone from disk or cannot be decrypted")
    from fastapi.responses import Response

    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", doc.get("filename") or "document")
    return Response(
        content=blob,
        media_type=doc.get("content_type") or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{safe_name}"', "Cache-Control": "no-store"},
    )


@router.get("/api/sanctuary/vault/{doc_id}/file")
async def vault_file(doc_id: int, user: dict = Depends(_unlocked_user)):
    doc = await sanctuary_db.get_document(int(user["id"]), doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="No such document")
    return _serve_document(doc, int(user["id"]))


# One refile at a time per user; the page polls this while it runs. Opening
# and reading a few hundred encrypted papers takes minutes on the small box —
# done inside the request, nginx gave up at 60s and the button looked dead.
_REFILE_JOBS: dict[int, dict] = {}


async def _refile_job(user_id: int) -> None:
    state = _REFILE_JOBS[user_id]
    try:
        # ── fingerprints first, so twins can be seen ──
        docs = await sanctuary_db.list_documents(user_id)
        state["total"] = len(docs)
        by_sha: dict[str, dict] = {}
        for doc in docs:
            state["scanned"] += 1
            sha = doc.get("content_sha") or ""
            if not sha:
                blob = await asyncio.to_thread(_read_document_blob, doc, user_id)
                if blob is None:
                    continue
                sha = hashlib.sha1(blob, usedforsecurity=False).hexdigest()
                await sanctuary_db.set_document_sha(user_id, doc["id"], sha)
                doc["content_sha"] = sha
                # A payslip stored before the salary reader existed gets its
                # month filled now, while its pages are open anyway. Only a
                # payslip is worth the PDF reader's time.
                if (doc.get("content_type") or "") == "application/pdf" and doc.get("series") == "Payslips":
                    await _salary_from_payslip(user_id, blob)
            keeper = by_sha.get(sha)
            if keeper is None:
                by_sha[sha] = doc
                continue
            # Two rows, one paper: keep the better-described of the pair.
            loser = doc
            if keeper["category"] == "Other" and doc["category"] != "Other":
                by_sha[sha], loser = doc, keeper
            gone = await sanctuary_db.delete_document(user_id, loser["id"])
            if gone:
                state["removed"] += 1
                try:
                    os.unlink(_vault_file_path(user_id, gone.get("file_token") or "missing"))
                except OSError:
                    pass
        for doc in await sanctuary_db.list_documents(user_id):
            folder = doc.get("note") or ""
            if folder.count("/") >= 0 and folder.startswith("payslips"):
                folder = "/".join(folder.split("/")[1:])
            read = sanctuary_docs.classify_document(doc.get("filename") or "", folder)
            changed = {}
            if read["category"] != doc["category"]:
                changed["category"] = read["category"]
            if read["series"] != doc["series"]:
                changed["series"] = read["series"]
            if read["doc_date"] and not doc["doc_date"]:
                changed["doc_date"] = read["doc_date"]
            if folder != (doc.get("note") or ""):
                changed["note"] = folder
            if changed:
                await sanctuary_db.update_document(user_id, doc["id"], changed)
                state["moved"] += 1
    finally:
        state["running"] = False


@router.post("/api/sanctuary/vault/refile")
async def vault_refile(user: dict = Depends(_unlocked_user)):
    """Read every document's name again, for when the reading improves.

    Each row keeps the folder it arrived from in its note, so nothing needs
    re-uploading. A document whose title he has edited himself is left alone.
    While every file is open anyway, each learns its content fingerprint, and
    a paper stored twice under one fingerprint is kept only once — the copy
    that knows what it is, or failing that the oldest. The work runs in the
    background and the page watches /refile/status until it settles.
    """
    user_id = int(user["id"])
    state = _REFILE_JOBS.get(user_id)
    if state and state.get("running"):
        return {"started": False, **state}
    _REFILE_JOBS[user_id] = {"running": True, "scanned": 0, "total": 0, "moved": 0, "removed": 0}
    asyncio.create_task(_refile_job(user_id))
    return {"started": True, **_REFILE_JOBS[user_id]}


@router.get("/api/sanctuary/vault/refile/status")
async def vault_refile_status(user: dict = Depends(_unlocked_user)):
    return _REFILE_JOBS.get(int(user["id"])) or {"running": False, "scanned": 0, "total": 0, "moved": 0, "removed": 0}


@router.put("/api/sanctuary/vault/{doc_id}")
async def vault_update(doc_id: int, request: Request, user: dict = Depends(_unlocked_user)):
    fields = _clean_document_fields(await request.json())
    if not fields or not await sanctuary_db.update_document(int(user["id"]), doc_id, fields):
        raise HTTPException(status_code=404, detail="No such document")
    return {"ok": True}


@router.delete("/api/sanctuary/vault/{doc_id}")
async def vault_delete(doc_id: int, user: dict = Depends(_unlocked_user)):
    doc = await sanctuary_db.delete_document(int(user["id"]), doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="No such document")
    try:
        os.unlink(_vault_file_path(int(user["id"]), doc.get("file_token") or "missing"))
    except OSError:
        pass
    return {"ok": True}


# ── Family access: the vault outlives its keeper ─────────────────
#
# Phil's ask, 2026-08-27: "I want my family to access these docs here
# after me." The sanctuary itself stays his alone — this is a separate,
# read-only door. A family member signs in with their own PhilForge
# account (any role), opens /vault, and gives the family passcode he
# set. Wrong passcode is 403 with the same slow-down as the sanctuary
# gate; no passcode set means the door does not exist.


async def _vault_owner() -> dict | None:
    return await core_db.get_admin_user()


async def _family_user(request: Request) -> dict:
    token = auth.get_session_token(request)
    session = await auth.validate_session(token)
    user = await core_db.get_user_by_id(session["user_id"]) if session else None
    if not user or not user.get("is_active"):
        raise HTTPException(status_code=401, detail="Sign in first")
    granted = await core_db.has_action_grant(int(user["id"]), auth.session_storage_key(token), FAMILY_ACTION_CLASS)
    if not granted:
        raise HTTPException(status_code=423, detail="vault_locked")
    return user


@router.post("/api/sanctuary/vault/family/passcode")
async def vault_family_passcode(request: Request, user: dict = Depends(_unlocked_user)):
    """The owner sets (or clears) the family passcode."""
    payload = await request.json()
    passcode = str(payload.get("passcode") or "")
    if not passcode:
        await sanctuary_db.set_state(int(user["id"]), "vault_family_hash", "")
        return {"family_access": False}
    if len(passcode) < 8:
        raise HTTPException(status_code=400, detail="Use at least 8 characters")
    await sanctuary_db.set_state(int(user["id"]), "vault_family_hash", auth.hash_password(passcode))
    return {"family_access": True}


@router.get("/api/sanctuary/vault/family/status")
async def vault_family_status(user: dict = Depends(_unlocked_user)):
    stored = await sanctuary_db.get_state(int(user["id"]), "vault_family_hash", "")
    return {"family_access": bool(stored)}


@router.post("/api/vault/unlock")
async def vault_family_unlock(request: Request):
    token = auth.get_session_token(request)
    session = await auth.validate_session(token)
    user = await core_db.get_user_by_id(session["user_id"]) if session else None
    if not user or not user.get("is_active"):
        raise HTTPException(status_code=401, detail="Sign in first")
    user_id = int(user["id"])
    if _too_many_failures(user_id):
        raise HTTPException(status_code=429, detail="Too many tries — rest a while and come back")
    owner = await _vault_owner()
    stored = await sanctuary_db.get_state(int(owner["id"]), "vault_family_hash", "") if owner else ""
    payload = await request.json()
    passcode = str(payload.get("passcode") or "")
    if not stored or not auth.verify_password(passcode, stored):
        _unlock_failures.setdefault(user_id, []).append(time.monotonic())
        await asyncio.sleep(0.6)
        raise HTTPException(status_code=403, detail="That is not the family key")
    _unlock_failures.pop(user_id, None)
    expires_at = (datetime.now(ZoneInfo("UTC")) + timedelta(seconds=UNLOCK_TTL_SECONDS)).isoformat()
    await core_db.grant_action_class(user_id, auth.session_storage_key(token), FAMILY_ACTION_CLASS, expires_at)
    return {"unlocked": True, "ttl_seconds": UNLOCK_TTL_SECONDS}


@router.get("/api/vault/documents")
async def vault_family_list(user: dict = Depends(_family_user)):
    owner = await _vault_owner()
    if not owner:
        return {"documents": []}
    docs = [_document_view(d) for d in await sanctuary_db.list_documents(int(owner["id"]))]
    return {"documents": docs, "owner": owner.get("username") or ""}


@router.get("/api/vault/documents/{doc_id}/file")
async def vault_family_file(doc_id: int, user: dict = Depends(_family_user)):
    owner = await _vault_owner()
    doc = await sanctuary_db.get_document(int(owner["id"]), doc_id) if owner else None
    if not doc:
        raise HTTPException(status_code=404, detail="No such document")
    return _serve_document(doc, int(owner["id"]))


@router.get("/vault", response_class=HTMLResponse)
async def vault_page(request: Request):
    token = auth.get_session_token(request)
    session = await auth.validate_session(token)
    user = await core_db.get_user_by_id(session["user_id"]) if session else None
    if not user or not user.get("is_active"):
        return RedirectResponse("/app", status_code=307)
    page_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vault.html")
    with open(page_path, encoding="utf-8") as handle:
        return HTMLResponse(handle.read())


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
        # Marked as his, so a payslip uploaded later cannot overwrite what he
        # deliberately typed.
        entry["salary_source"] = "manual"
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


@router.put("/api/sanctuary/finance/entry/{row_id}")
async def ledger_recategorise_row(row_id: int, request: Request, user: dict = Depends(_unlocked_user)):
    """One row, one correction — a rule is for a payee, this is for an oddity."""
    payload = await request.json()
    category = str(payload.get("category") or "").strip()[:60]
    if not category:
        raise HTTPException(status_code=400, detail="Name the category")
    if not await sanctuary_db.set_ledger_category(int(user["id"]), row_id, category):
        raise HTTPException(status_code=404, detail="No such entry")
    return {"ok": True}


@router.get("/api/sanctuary/finance/standing")
async def finance_standing(user: dict = Depends(_unlocked_user)):
    """Where he stands: every debt still owed, the monthly load, and what
    the last year's real cashflow says about the road out."""
    import statistics

    user_id = int(user["id"])
    loans = await sanctuary_db.list_loans(user_id)
    debts = []
    unknown = 0
    for loan in loans:
        if not loan.get("active"):
            continue
        emis = await sanctuary_db.emis_for_loan(user_id, loan["id"])
        remaining = sum(e["amount"] for e in emis if not e["paid_on"])
        if remaining <= 0:
            # A schedule built from statements only knows the PAST — a
            # running loan with every known EMI paid still owes its future.
            # The card's drawn/outstanding figure fills in; without one the
            # debt is honestly unknown, never zero.
            remaining = float(loan.get("drawn_amount") or 0)
            if remaining <= 0 and float(loan.get("emi_amount") or 0) > 0:
                unknown += 1
                continue
        if remaining > 0:
            debts.append(
                {
                    "name": loan["name"],
                    "remaining": round(remaining, 2),
                    "emi": float(loan.get("emi_amount") or 0),
                }
            )
    total_debt = round(sum(d["remaining"] for d in debts), 2)
    monthly_emi = round(sum(d["emi"] for d in debts), 2)

    flows = await sanctuary_db.monthly_flows(user_id, 13)
    current = _month_key(_today_ist())
    complete = [f for f in flows if f["month"] != current][-12:]
    surpluses = [f["inflow"] - f["outflow"] for f in complete if f["inflow"] > 0]
    surplus = round(statistics.median(surpluses), 2) if len(surpluses) >= 3 else None

    months_out = None
    if surplus and surplus > 0 and total_debt > 0:
        months_out = round(total_debt / surplus)
    return {
        "debts": debts,
        "debt_count": len(debts),
        "unknown_count": unknown,
        "total_debt": total_debt,
        "monthly_emi": monthly_emi,
        "flows": complete,
        "surplus_median": surplus,
        "months_to_clear": months_out,
    }


@router.get("/api/sanctuary/finance/duplicates")
async def finance_duplicates(user: dict = Depends(_unlocked_user)):
    """Hand-entered rows the statements later told again. Each hand row is
    paired with its closest bank row by amount and date; pairs the owner
    ruled 'truly separate' stay dismissed."""
    from datetime import datetime as _dt

    user_id = int(user["id"])
    hand = await sanctuary_db.ledger_rows_by_sources(user_id, ("manual", "recurring"))
    bank = await sanctuary_db.ledger_rows_by_sources(user_id, ("statement",))
    dismissed = set(await sanctuary_db.get_json_state(user_id, "dup_dismissed", []))

    def day(row):
        return _dt.strptime(row["entry_date"], "%Y-%m-%d").date()

    taken: set[int] = set()
    pairs = []
    for m in hand:
        best = None
        for b in bank:
            if b["id"] in taken or abs(b["amount"] - m["amount"]) >= 0.01:
                continue
            distance = abs((day(b) - day(m)).days)
            if distance <= 3 and (best is None or distance < best[0]):
                best = (distance, b)
        if not best:
            continue
        b = best[1]
        if f"{m['id']}:{b['id']}" in dismissed:
            continue
        taken.add(b["id"])
        pairs.append({"hand": m, "bank": b})
    return {"pairs": pairs}


@router.post("/api/sanctuary/finance/duplicates/resolve")
async def finance_duplicates_resolve(request: Request, user: dict = Depends(_unlocked_user)):
    user_id = int(user["id"])
    payload = await request.json()
    try:
        hand_id, bank_id = int(payload.get("hand_id")), int(payload.get("bank_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Which pair?") from None
    action = str(payload.get("action") or "")
    if action == "merge":
        if not await sanctuary_db.merge_duplicate_pair(user_id, hand_id, bank_id):
            raise HTTPException(status_code=404, detail="That pair is gone")
        return {"ok": True}
    if action == "keep":
        dismissed = await sanctuary_db.get_json_state(user_id, "dup_dismissed", [])
        mark = f"{hand_id}:{bank_id}"
        if mark not in dismissed:
            dismissed.append(mark)
        await sanctuary_db.set_json_state(user_id, "dup_dismissed", dismissed[:1000])
        return {"ok": True}
    raise HTTPException(status_code=400, detail="merge or keep")


@router.get("/api/sanctuary/finance/search")
async def ledger_search(q: str = "", user: dict = Depends(_unlocked_user)):
    query = q.strip()
    if len(query) < 2:
        raise HTTPException(status_code=400, detail="Give the search two characters or more")
    rows = await sanctuary_db.search_ledger(int(user["id"]), query)
    return {"rows": rows, "capped": len(rows) >= 400}


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
                "source": "statement-in" if row.get("dir") == "in" else "statement",
                "ref_id": ref_id[:120],
            }
        )
    added = await sanctuary_db.add_ledger_many(user_id, cleaned)
    account = re.sub(r"\D", "", str(payload.get("account") or ""))
    if added and 9 <= len(account) <= 18:
        await _remember_account_number(user_id, account, str(payload.get("bank") or ""))
    linked = payload.get("linked_accounts")
    if added and isinstance(linked, list):
        for raw in linked[:10]:
            entry = raw if isinstance(raw, dict) else {"number": raw}
            number = re.sub(r"\D", "", str(entry.get("number") or ""))
            kind = str(entry.get("kind") or "Linked account")[:80]
            if 9 <= len(number) <= 18 and number != account:
                await _remember_account_number(user_id, number, kind)
    return {"added": added, "skipped": len(cleaned) - added}


async def _remember_account_number(user_id: int, account: str, bank: str) -> None:
    """File the statement's account number under Important info, once."""
    notes = await sanctuary_db.get_json_state(user_id, "notes", [])
    title = f"Bank account ··{account[-4:]}"
    if any(n.get("title") == title or account in str(n.get("body", "")) for n in notes):
        return
    if len(notes) >= 200:
        return
    body = f"Account number: {account}"
    if bank.strip():
        body += f"\nBank: {bank.strip()[:80]}"
    body += f"\nFiled from a statement upload on {_today_ist().isoformat()}."
    notes.append(
        {
            "id": secrets.token_hex(4),
            "title": title,
            "body": body,
            "updated_at": _today_ist().isoformat(),
        }
    )
    await sanctuary_db.set_json_state(user_id, "notes", notes)


@router.get("/api/sanctuary/statements/review")
async def statement_review(user: dict = Depends(_unlocked_user)):
    """The unsorted rows, grouped by who was paid — the review row's feed."""
    rows = await sanctuary_db.uncategorised_ledger(int(user["id"]))
    groups: dict[str, dict] = {}
    for row in rows:
        key = sanctuary_statements.payee_key(row["note"])
        group = groups.setdefault(
            key,
            {"match": key, "count": 0, "total": 0.0, "sample": row["note"], "in_total": 0.0, "out_total": 0.0},
        )
        group["count"] += 1
        group["total"] = round(group["total"] + row["amount"], 2)
        # Which way the money went. A payee can be both — money sent to the
        # broker and money coming back — so both sides are kept, and the
        # page can say so instead of showing one ambiguous figure.
        side = "in_total" if row["source"] == "statement-in" else "out_total"
        group[side] = round(group[side] + row["amount"], 2)
    ordered = sorted(groups.values(), key=lambda g: -g["total"])
    return {"uncategorised": len(rows), "groups": ordered[:120]}


@router.post("/api/sanctuary/statements/resort")
async def statement_resort(user: dict = Depends(_unlocked_user)):
    """Run every rule over the unsorted pile again.

    A rule only ever touched rows at import time or when it was taught —
    one added later (Kyndryl as salary, the brokers as investments) never
    reached rows already sitting in the pile, so the pile could only grow.
    This walks the unsorted rows through the full rulebook, his taught
    rules first, and files every row the book now knows. A category a rule
    names into being is created on the way — Investments as a saving, so
    the broker money leaves 'spent' rather than swelling it.
    """
    user_id = int(user["id"])
    user_rules = await sanctuary_db.get_json_state(user_id, "stmt_rules", [])
    moved: dict[str, int] = {}
    for row in await sanctuary_db.uncategorised_ledger(user_id):
        category = sanctuary_statements.categorise(row["note"], user_rules)
        if category == sanctuary_statements.UNCATEGORISED:
            continue
        await sanctuary_db.set_ledger_category(user_id, row["id"], category)
        moved[category] = moved.get(category, 0) + 1
    if moved:
        categories = await _get_categories(user_id)
        have = {c["name"] for c in categories}
        savings = {"Investments"}
        for name in sorted(moved):
            if name not in have and len(categories) < 100:
                categories.append(
                    {
                        "name": name,
                        "emoji": "📈" if name in savings else "🏷️",
                        "kind": "saving" if name in savings else "expense",
                        "quick": False,
                    }
                )
        await sanctuary_db.set_json_state(user_id, "categories", categories)
    return {"moved": sum(moved.values()), "by_category": moved}


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
    from_category = str(payload.get("from_category") or "Uncategorised").strip()[:60]
    moved = await sanctuary_db.recategorise_matching(user_id, match, category, from_category)
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
    if "drawn_amount" in payload:
        try:
            fields["drawn_amount"] = max(0.0, round(float(payload.get("drawn_amount") or 0), 2))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Bad drawn amount") from None
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


async def _loan_candidates(user_id: int) -> list[dict]:
    rows = await sanctuary_db.statement_outflow_rows(user_id)
    candidates = await asyncio.to_thread(sanctuary_statements.discover_loans, rows, _today_ist())
    ignored = set(await sanctuary_db.get_json_state(user_id, "loan_ignored", []))
    # A carded stream is recognised by its SCHEDULE, not its key: keys can
    # be textual (a lender whose references never repeat stores no number on
    # the card) and cards get renamed and edited. If a loan's EMI dates cover
    # most of a candidate's debit dates at the same EMI, that stream is
    # already on the shelf — and deleting the card brings the offer back.
    carded = []
    for loan in await sanctuary_db.list_loans(user_id):
        emis = await sanctuary_db.emis_for_loan(user_id, loan["id"])
        carded.append(
            {
                "loan": loan,
                "dates": {e["due_date"] for e in emis},
                "emi": float(loan.get("emi_amount") or 0),
                "acct": str(loan.get("account_no") or ""),
            }
        )

    def match(c):
        dates = {d["due_date"] for d in c["debits"]}
        for entry in carded:
            if abs(entry["emi"] - c["emi"]) > 0.03 * max(c["emi"], 1):
                continue
            if entry["acct"] and entry["acct"] == c["key"]:
                return entry
            if dates and entry["dates"] and len(dates & entry["dates"]) >= 0.6 * len(dates):
                return entry
        return None

    fresh = []
    for c in candidates:
        if c["key"] in ignored or f"{c['key']}#{c['emi']:.0f}" in ignored:
            continue
        entry = match(c)
        if entry is None:
            fresh.append(c)
            continue
        # Already on the shelf — but a card he typed himself carries no loan
        # number, and the statements know it. Fill that gap silently: having
        # the number at hand is the whole reason he asked for it stored.
        loan = entry["loan"]
        if c["key"].isdigit() and not loan.get("account_no"):
            detail = f"Mandate found in the statements: {c['sample']}"
            existing = loan.get("details") or ""
            await sanctuary_db.update_loan(
                user_id,
                loan["id"],
                {
                    "account_no": c["key"],
                    "details": f"{existing}\n\n{detail}" if existing else detail,
                },
            )
            entry["acct"] = c["key"]
            loan["account_no"] = c["key"]
    return fresh


@router.get("/api/sanctuary/loans/discover")
async def loans_discover(user: dict = Depends(_unlocked_user)):
    """EMI-shaped streams the statements remember, not yet on the shelf."""
    candidates = await _loan_candidates(int(user["id"]))
    for cand in candidates:
        cand.pop("debits", None)
    return {"candidates": candidates}


def _match_candidate(candidates: list[dict], key: str, emi) -> dict | None:
    """Two parallel loans share a mandate key — the EMI tells them apart."""
    matches = [c for c in candidates if c["key"] == key]
    if emi is not None:
        matches = [c for c in matches if abs(c["emi"] - float(emi)) <= 0.03 * max(c["emi"], 1)]
    return matches[0] if matches else None


@router.post("/api/sanctuary/loans/adopt")
async def loans_adopt(request: Request, user: dict = Depends(_unlocked_user)):
    """One tap: the stream becomes a loan card wearing its real history."""
    user_id = int(user["id"])
    payload = await request.json()
    key = str(payload.get("key") or "")
    try:
        emi = float(payload["emi"]) if payload.get("emi") is not None else None
    except (TypeError, ValueError):
        emi = None
    cand = _match_candidate(await _loan_candidates(user_id), key, emi)
    if not cand:
        raise HTTPException(status_code=404, detail="That stream is gone or already carded")
    # The offer list filters what is already carded, but a page open since
    # before the last adoption still shows the old list — two taps three
    # minutes apart put the Kotak car loan on the shelf three times. The
    # schedule is checked again HERE, against the shelf as it stands now.
    wanted = {d["due_date"] for d in cand["debits"]}
    for loan in await sanctuary_db.list_loans(user_id):
        if abs(float(loan.get("emi_amount") or 0) - cand["emi"]) > 0.03 * max(cand["emi"], 1):
            continue
        held = {e["due_date"] for e in await sanctuary_db.emis_for_loan(user_id, loan["id"])}
        if wanted and held and len(wanted & held) >= 0.6 * len(wanted):
            raise HTTPException(
                status_code=409,
                detail=f"“{loan['name']}” is already this loan — same instalments, same dates.",
            )
    # The schedule holds one EMI per day, and a bank can debit twice on one
    # date (the CRED mandate did) — same-day debits fold into one row.
    by_date: dict[str, float] = {}
    for debit in cand["debits"]:
        by_date[debit["due_date"]] = round(by_date.get(debit["due_date"], 0) + debit["amount"], 2)
    cand["debits"] = [{"due_date": d, "amount": a} for d, a in sorted(by_date.items())]
    years = cand["first"][:4] + ("–" + cand["last"][:4] if cand["last"][:4] != cand["first"][:4] else "")
    name = str(payload.get("name") or "").strip()[:80] or f"{cand['lender'] or 'Loan'} · {years}"
    loan_id = await sanctuary_db.create_loan(
        user_id,
        {
            "name": name,
            "lender": cand["lender"],
            "emi_amount": cand["emi"],
            "due_day": min(max(int(cand["last"][8:10]), 1), 28),
            "start_date": cand["first"],
            "account_no": cand["key"] if cand["key"].isdigit() else "",
            "note": "found in the statements",
            "details": f"Auto-found from the bank statements: {cand['count']} debits of ~₹{cand['emi']:,.0f}, {cand['first']} to {cand['last']}. Last narration: {cand['sample']}",
            "active": not cand["closed"],
        },
    )
    try:
        await sanctuary_db.replace_schedule(user_id, loan_id, cand["debits"])
        await sanctuary_db.settle_past_emis(user_id, loan_id, cand["last"])
    except Exception:
        # A card without its history is worse than no card — undo and say so.
        await sanctuary_db.delete_loan(user_id, loan_id)
        raise HTTPException(status_code=500, detail="The schedule would not take — nothing was added") from None
    return {"id": loan_id, "name": name, "paid": len(cand["debits"]), "closed": cand["closed"]}


@router.post("/api/sanctuary/loans/schedule")
async def loans_schedule_import(file: UploadFile, user: dict = Depends(_unlocked_user)):
    """A lender's own schedule, read straight onto the shelf.

    A loan drawn on a credit card never appears as a discoverable stream
    until its debits have run for months, and even then the dates are
    guessed. The card issues the truth — every instalment to the last one,
    the rate, and what is still owed — so this reads it whole. Importing
    the same schedule twice updates that loan rather than adding another.
    """
    user_id = int(user["id"])
    blob = await _read_upload(file)
    if not blob:
        raise HTTPException(status_code=400, detail="Empty file")
    try:
        read = await asyncio.to_thread(_read_card_schedule_pdf, blob, file.filename or "")
    except Exception:  # noqa: BLE001 - an unreadable upload is not a crash
        read = None
    if not read:
        raise HTTPException(
            status_code=400,
            detail="No loan schedule found in that file — it wants the lender's own EMI table.",
        )

    number = read["loan_number"]
    card = f"card ••{read['card_tail']}" if read["card_tail"] else "a card"
    name = f"{read['kind']} on {card} · {read['booked'][:4]}"
    details = (
        f"From the lender's own schedule: ₹{read['principal']:,.0f} over {read['tenure']} months "
        f"at {read['rate']}%, booked {read['booked']}. Outstanding ₹{read['outstanding']:,.0f}. "
        f"Loan number {number}."
    )
    fields = {
        "name": name,
        "lender": read["kind"],
        "emi_amount": read["emi"],
        "due_day": min(max(int(read["first"][8:10]), 1), 28),
        "start_date": read["first"],
        "account_no": number,
        "note": f"drawn on {card}",
        "details": details,
        "active": read["outstanding"] > 0,
    }
    existing = next(
        (loan for loan in await sanctuary_db.list_loans(user_id) if str(loan.get("account_no") or "") == number),
        None,
    )
    if existing:
        await sanctuary_db.update_loan(user_id, existing["id"], fields)
        loan_id = existing["id"]
    else:
        loan_id = await sanctuary_db.create_loan(user_id, fields)
    try:
        await sanctuary_db.replace_schedule(user_id, loan_id, read["emis"])
        await sanctuary_db.settle_past_emis(user_id, loan_id, _today_ist().isoformat())
    except Exception:
        if not existing:
            await sanctuary_db.delete_loan(user_id, loan_id)
        raise HTTPException(status_code=500, detail="The schedule would not take — nothing was changed") from None
    return {
        "id": loan_id,
        "name": name,
        "updated": bool(existing),
        "instalments": len(read["emis"]),
        "emi": read["emi"],
        "outstanding": read["outstanding"],
        "card_tail": read["card_tail"],
    }


def _read_card_schedule_pdf(blob: bytes, filename: str) -> dict | None:
    import io

    import pdfplumber

    with pdfplumber.open(io.BytesIO(blob)) as pdf:
        text = "\n".join((page.extract_text() or "") for page in pdf.pages)
    return sanctuary_statements.parse_card_loan_schedule(text, filename)


@router.post("/api/sanctuary/loans/discover/ignore")
async def loans_discover_ignore(request: Request, user: dict = Depends(_unlocked_user)):
    user_id = int(user["id"])
    payload = await request.json()
    key = str(payload.get("key") or "")[:80]
    if not key:
        raise HTTPException(status_code=400, detail="Which one?")
    try:
        mark = f"{key}#{float(payload['emi']):.0f}" if payload.get("emi") is not None else key
    except (TypeError, ValueError):
        mark = key
    ignored = await sanctuary_db.get_json_state(user_id, "loan_ignored", [])
    if mark not in ignored:
        ignored.append(mark)
    await sanctuary_db.set_json_state(user_id, "loan_ignored", ignored[:200])
    return {"ok": True}


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


# The banks and cards he holds. A statement proves an account exists but
# never says whose bank it is, and a card he has never spent from leaves no
# trace at all — so these are his to state, and the number rests encrypted
# like everything else the family may one day need.
_ACCOUNT_KINDS = ("bank", "card")


def _account_view(item: dict) -> dict:
    number = auth.decrypt_value(item.get("number") or "")
    return {
        "id": item.get("id") or "",
        "kind": item.get("kind") or "bank",
        "bank": item.get("bank") or "",
        "label": item.get("label") or "",
        "number": number,
        "tail": number[-6:] if number else "",
        "note": item.get("note") or "",
    }


@router.get("/api/sanctuary/accounts")
async def accounts_get(user: dict = Depends(_unlocked_user)):
    stored = await sanctuary_db.get_json_state(int(user["id"]), "accounts", [])
    return {"accounts": [_account_view(a) for a in stored]}


@router.put("/api/sanctuary/accounts")
async def accounts_put(request: Request, user: dict = Depends(_unlocked_user)):
    payload = await request.json()
    items = payload.get("accounts")
    if not isinstance(items, list) or len(items) > 60:
        raise HTTPException(status_code=400, detail="Bad accounts list")
    cleaned = []
    for item in items:
        item = item or {}
        number = str(item.get("number") or "").strip()[:40]
        kind = str(item.get("kind") or "bank")
        cleaned.append(
            {
                "id": str(item.get("id") or secrets.token_hex(4)),
                "kind": kind if kind in _ACCOUNT_KINDS else "bank",
                "bank": str(item.get("bank") or "").strip()[:60],
                "label": str(item.get("label") or "").strip()[:80],
                "number": auth.encrypt_value(number) if number else "",
                "note": str(item.get("note") or "").strip()[:300],
            }
        )
    await sanctuary_db.set_json_state(int(user["id"]), "accounts", cleaned)
    return {"ok": True}


@router.get("/api/sanctuary/known")
async def known_get(user: dict = Depends(_unlocked_user)):
    """The accounts and lenders the sanctuary already knows about.

    Important info was a page he had to type himself, so a bank he had
    never written down looked missing even when its loan was on the shelf.
    This says what the statements and cards already prove — every account
    a statement was imported from, and every lender with a card — so the
    family can find them without him having written a word.
    """
    user_id = int(user["id"])
    accounts: dict[str, dict] = {}
    for row in await sanctuary_db.statement_account_summary(user_id):
        tail = row["tail"]
        if tail == "unknown":
            continue
        accounts[tail] = {
            "tail": tail,
            "entries": row["entries"],
            "first": row["first"],
            "last": row["last"],
            "kind": "bank",
            "bank": "",
            "label": "",
            "id": "",
        }
    # An account he has stated but never imported a statement from can still
    # be corroborated: his own RTGS and NEFT narrations name the other side
    # in full. The transfers only CONFIRM what he says — they never invent an
    # account, because most counterparties in a narration are employers and
    # payees, not his.
    stated_accounts = [_account_view(a) for a in await sanctuary_db.get_json_state(user_id, "accounts", [])]
    wanted = {a["number"] for a in stated_accounts if a["number"] and a["kind"] == "bank"}
    corroboration: dict[str, dict] = {}
    if wanted:
        for row in await sanctuary_db.transfer_narrations(user_id):
            for seen in sanctuary_statements.counterparty_accounts(row["note"]):
                match = next((w for w in wanted if w in seen["number"] or seen["number"] in w), None)
                if not match:
                    continue
                entry = corroboration.setdefault(match, {"transfers": 0, "last": "", "bank": seen["bank"]})
                entry["transfers"] += 1
                entry["last"] = max(entry["last"], row["entry_date"])

    # What he has told us marries into what the statements prove: an account
    # he has named gains its bank, and one he holds but has never imported
    # appears anyway. A card is his word alone — it leaves no statement here.
    cards = []
    for stated in stated_accounts:
        if stated["kind"] == "card":
            cards.append(stated)
            continue
        proof = corroboration.get(stated["number"]) or {}
        extra = {
            "bank": stated["bank"] or proof.get("bank", ""),
            "label": stated["label"],
            "id": stated["id"],
            "transfers": proof.get("transfers", 0),
            "transfer_last": proof.get("last", ""),
        }
        seen = accounts.get(stated["tail"])
        if seen:
            seen.update(extra)
        else:
            accounts[stated["tail"] or stated["id"]] = {**stated, **extra, "entries": 0, "first": "", "last": ""}
    # The loans are NOT repeated here. They have their own panel, in full,
    # and listing twelve of them turned this card into a wall to scroll
    # past. Only the count travels, as a pointer to where they live.
    loans = await sanctuary_db.list_loans(user_id)
    return {
        "accounts": sorted(accounts.values(), key=lambda a: -a["entries"]),
        "cards": cards,
        "loans_open": sum(1 for loan in loans if loan.get("active")),
        "loans_settled": sum(1 for loan in loans if not loan.get("active")),
    }


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
    entry = months_state.get(month) or {}
    salary = float(entry.get("salary", default_salary))
    salary_source = str(entry.get("salary_source") or "")
    # No payslip for this month, but the bank saw the pay arrive: the
    # employer's credit is the salary. His own figure and a payslip both
    # outrank it, and it is never written down — the statement stays the
    # source, so a corrected import corrects the tile.
    if "salary" not in entry:
        from_bank = _salary_from_ledger(ledger)
        if from_bank:
            salary, salary_source = from_bank, "statement"

    excluded = saving_names | {"Self transfer"}
    outgo = [r for r in ledger if r["source"] != "statement-in"]
    spent = sum(r["amount"] for r in outgo if r["category"] not in excluded)
    saved = sum(r["amount"] for r in outgo if r["category"] in saving_names)
    inflow = sum(r["amount"] for r in ledger if r["source"] == "statement-in")
    # The pay that arrives on the 30th is not spent that day — it is what
    # the NEXT month lives on. So each month opens with whatever the last
    # one still held, carried in as funds rather than as a second salary.
    carried_in = await _month_leftover(user_id, _previous_month(month), saving_names, months_state, default_salary)

    # An EMI the bank statement already told is not spent AGAIN by the
    # schedule that planned it. A schedule row whose amount appears as a
    # statement debit within five days of its due date is marked in_ledger:
    # it stays visible on its loan, but the money counts once — in the
    # ledger row, where it actually moved.
    stmt_days = {}
    for row in outgo:
        if str(row.get("ref_id") or "").startswith("stmt:"):
            stmt_days.setdefault(round(row["amount"], 2), []).append(row["entry_date"])
    for emi in emis:
        due = date.fromisoformat(emi["due_date"])
        emi["in_ledger"] = any(
            abs((date.fromisoformat(d) - due).days) <= 5 for d in stmt_days.get(round(emi["amount"], 2), [])
        )
        # An instalment that has not come due is not money spent. A future
        # month must read empty, not pre-charged with what it will owe.
        emi["due_yet"] = bool(emi["paid_on"]) or due <= today
    emi_total = sum(e["amount"] for e in emis if not e["in_ledger"] and e["due_yet"])

    by_category: dict[str, float] = {}
    for row in outgo:
        if row["category"] == "Self transfer":
            continue  # his own money moving is not a spending bar
        by_category[row["category"]] = by_category.get(row["category"], 0) + row["amount"]
    for emi in emis:
        if emi["in_ledger"] or not emi["due_yet"]:
            continue
        label = f"EMI · {emi['loan_name']}"
        by_category[label] = by_category.get(label, 0) + emi["amount"]

    # Which months hold anything at all — the jump picker rings them, so
    # eight years of history is reachable without a hundred taps.
    known = await sanctuary_db.months_with_anything(user_id)

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
        "salary_source": salary_source,
        "salary_is_default": salary_source != "statement"
        and (month not in months_state or "salary" not in (months_state.get(month) or {})),
        "note": (months_state.get(month) or {}).get("note", ""),
        "totals": {
            "inflow": round(inflow, 2),
            "spent": round(spent + emi_total, 2),
            "expenses": round(spent, 2),
            "emis": round(emi_total, 2),
            "saved": round(saved, 2),
            "carried_in": carried_in,
            "left": round(carried_in + salary - spent - emi_total - saved, 2),
        },
        "carried_from": _previous_month(month),
        "months_known": known,
        "salary_months": {m: 1 for m, v in months_state.items() if (v or {}).get("salary")},
        "by_category": {k: round(v, 2) for k, v in sorted(by_category.items(), key=lambda kv: -kv[1])},
        "ledger": ledger,
        "emis": emis,
        "alerts": alerts,
        "categories": categories,
        "recurring": await sanctuary_db.get_json_state(user_id, "recurring", []),
        "trend": trend,
    }
