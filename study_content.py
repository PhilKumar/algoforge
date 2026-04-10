from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm"}
DECK_EXTS = {".pdf", ".ppt", ".pptx", ".key"}
AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".aac", ".ogg"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

TYPE_META = {
    "video": {"label": "Video Overview", "accent": "video"},
    "deck": {"label": "Slide Deck", "accent": "deck"},
    "audio": {"label": "Audio Brief", "accent": "audio"},
    "image": {"label": "Reference Board", "accent": "image"},
    "file": {"label": "Study Asset", "accent": "file"},
}

_STUDY_SANITIZER_VERSION = 1
_STUDY_SANITIZER_INDEX = ".study-sanitize-index.json"
_PNG_METADATA_CHUNKS = {b"iCCP", b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"time"}


def _title_from_slug(slug: str) -> str:
    slug = re.sub(r"[\s_-]*\(\d+\)$", "", slug).strip()
    slug = re.sub(r"[\s_-]+alt$", "", slug, flags=re.IGNORECASE).strip()
    bits = [part for part in slug.replace("_", " ").replace("-", " ").split() if part]
    return " ".join(word.capitalize() for word in bits) or "Untitled Asset"


def _dedupe_key(title: str, kind: str, category: str) -> tuple[str, str, str]:
    normalized_title = re.sub(r"\s+", " ", title.strip()).lower()
    return kind, category.strip().lower(), normalized_title


def _dedupe_penalty(filename: str) -> int:
    lower = filename.lower()
    penalty = 0
    if re.search(r"\(\d+\)", lower):
        penalty += 3
    if re.search(r"(^|[-_ ])alt($|[-_ .])", lower):
        penalty += 2
    if "copy" in lower:
        penalty += 2
    return penalty


def _should_replace(existing: dict[str, Any], candidate: dict[str, Any]) -> bool:
    existing_rank = (
        _dedupe_penalty(existing["filename"]),
        -existing["size_bytes"],
        -existing["modified_ts"],
        len(existing["filename"]),
    )
    candidate_rank = (
        _dedupe_penalty(candidate["filename"]),
        -candidate["size_bytes"],
        -candidate["modified_ts"],
        len(candidate["filename"]),
    )
    return candidate_rank < existing_rank


def _category_from_parts(parts: list[str]) -> str:
    if not parts:
        return "General"
    return " / ".join(_title_from_slug(part) for part in parts if part)


def _guess_type(ext: str) -> str:
    ext = ext.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in DECK_EXTS:
        return "deck"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in IMAGE_EXTS:
        return "image"
    return "file"


def _format_size(num_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    size = float(max(num_bytes, 0))
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)}{unit}"
            return f"{size:.1f}{unit}"
        size /= 1024
    return "0B"


def _description_for_item(title: str, kind: str, category: str) -> str:
    if kind == "video":
        return f"A video overview for {category.lower()} sessions around {title.lower()}."
    if kind == "deck":
        return f"A slide deck for a short market reset, focused on {title.lower()} and {category.lower()}."
    if kind == "audio":
        return f"An audio brief for slower review, built around {title.lower()} in {category.lower()}."
    if kind == "image":
        return f"A reference board for {category.lower()} focused on {title.lower()}."
    return f"A study asset for {category.lower()} built around {title.lower()}."


def _study_base_dir(static_root: str) -> str:
    return os.path.join(static_root, "notebooklm")


def _study_index_path(base_dir: str) -> str:
    return os.path.join(base_dir, _STUDY_SANITIZER_INDEX)


def _load_study_index(base_dir: str) -> dict[str, dict[str, int]]:
    index_path = _study_index_path(base_dir)
    if not os.path.isfile(index_path):
        return {}
    try:
        with open(index_path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): value for key, value in data.items() if isinstance(value, dict)}


def _save_study_index(base_dir: str, index: dict[str, dict[str, int]]) -> None:
    index_path = _study_index_path(base_dir)
    tmp_path = index_path + ".tmp"
    payload = {key: index[key] for key in sorted(index)}
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    os.replace(tmp_path, index_path)


def _study_fingerprint(path: str) -> dict[str, int]:
    stat = os.stat(path)
    return {
        "version": _STUDY_SANITIZER_VERSION,
        "size_bytes": int(stat.st_size),
        "mtime_ns": int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
    }


def _clear_extended_attributes(path: str) -> bool:
    listxattr = getattr(os, "listxattr", None)
    removexattr = getattr(os, "removexattr", None)
    if not callable(listxattr) or not callable(removexattr):
        return False
    changed = False
    try:
        attrs = listxattr(path)
    except OSError:
        return False
    for attr in attrs:
        try:
            removexattr(path, attr)
            changed = True
        except OSError:
            continue
    return changed


def _rewrite_png_without_metadata(path: str) -> bool:
    with open(path, "rb") as handle:
        data = handle.read()
    signature = b"\x89PNG\r\n\x1a\n"
    if not data.startswith(signature):
        return False
    cursor = len(signature)
    out = bytearray(signature)
    changed = False
    while cursor + 12 <= len(data):
        length = int.from_bytes(data[cursor : cursor + 4], "big")
        chunk_end = cursor + 12 + length
        if chunk_end > len(data):
            return False
        chunk_type = data[cursor + 4 : cursor + 8]
        chunk = data[cursor:chunk_end]
        if chunk_type in _PNG_METADATA_CHUNKS:
            changed = True
        else:
            out.extend(chunk)
        cursor = chunk_end
        if chunk_type == b"IEND":
            break
    if not changed:
        return False
    tmp_path = path + ".tmp"
    with open(tmp_path, "wb") as handle:
        handle.write(out)
    os.replace(tmp_path, path)
    return True


def _rewrite_jpeg_without_metadata(path: str) -> bool:
    with open(path, "rb") as handle:
        data = handle.read()
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return False
    out = bytearray(b"\xff\xd8")
    cursor = 2
    changed = False
    while cursor < len(data):
        if data[cursor] != 0xFF:
            return False
        while cursor < len(data) and data[cursor] == 0xFF:
            cursor += 1
        if cursor >= len(data):
            break
        marker = data[cursor]
        cursor += 1
        if marker == 0xD9:
            out.extend(b"\xff\xd9")
            break
        if marker == 0xDA:
            if cursor + 2 > len(data):
                return False
            seg_len = int.from_bytes(data[cursor : cursor + 2], "big")
            seg_start = cursor - 2
            seg_end = cursor + seg_len
            if seg_end > len(data):
                return False
            out.extend(data[seg_start:seg_end])
            out.extend(data[seg_end:])
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            out.extend(b"\xff" + bytes([marker]))
            continue
        if cursor + 2 > len(data):
            return False
        seg_len = int.from_bytes(data[cursor : cursor + 2], "big")
        seg_start = cursor - 2
        seg_end = cursor + seg_len
        if seg_end > len(data):
            return False
        segment = data[seg_start:seg_end]
        if 0xE0 <= marker <= 0xEF or marker == 0xFE:
            changed = True
        else:
            out.extend(segment)
        cursor = seg_end
    if not changed:
        return False
    tmp_path = path + ".tmp"
    with open(tmp_path, "wb") as handle:
        handle.write(out)
    os.replace(tmp_path, path)
    return True


def _sanitize_study_file(base_dir: str, full_path: str, index: dict[str, dict[str, int]]) -> bool:
    rel_path = os.path.relpath(full_path, base_dir)
    if rel_path.startswith("..") or os.path.basename(rel_path).startswith("."):
        return False
    fingerprint = _study_fingerprint(full_path)
    if index.get(rel_path) == fingerprint:
        return False

    original_stat = os.stat(full_path)
    changed = _clear_extended_attributes(full_path)
    ext = os.path.splitext(full_path)[1].lower()
    try:
        if ext == ".png":
            changed = _rewrite_png_without_metadata(full_path) or changed
        elif ext in {".jpg", ".jpeg"}:
            changed = _rewrite_jpeg_without_metadata(full_path) or changed
    finally:
        if changed:
            _clear_extended_attributes(full_path)
            os.utime(
                full_path,
                ns=(
                    int(getattr(original_stat, "st_atime_ns", int(original_stat.st_atime * 1_000_000_000))),
                    int(getattr(original_stat, "st_mtime_ns", int(original_stat.st_mtime * 1_000_000_000))),
                ),
            )
    index[rel_path] = _study_fingerprint(full_path)
    return True


def sanitize_study_library(static_root: str) -> None:
    base_dir = _study_base_dir(static_root)
    if not os.path.isdir(base_dir):
        return
    index = _load_study_index(base_dir)
    changed = False
    seen: set[str] = set()
    for root, _, files in os.walk(base_dir):
        for name in sorted(files):
            if name.startswith("."):
                continue
            full_path = os.path.join(root, name)
            rel_path = os.path.relpath(full_path, base_dir)
            seen.add(rel_path)
            try:
                changed = _sanitize_study_file(base_dir, full_path, index) or changed
            except OSError:
                continue
    stale = [rel_path for rel_path in index if rel_path not in seen]
    for rel_path in stale:
        index.pop(rel_path, None)
        changed = True
    if changed:
        try:
            _save_study_index(base_dir, index)
        except OSError:
            pass


def sanitize_study_asset(static_root: str, full_path: str) -> None:
    base_dir = _study_base_dir(static_root)
    if not os.path.isdir(base_dir) or not os.path.isfile(full_path):
        return
    base_dir_abs = os.path.abspath(base_dir)
    full_path_abs = os.path.abspath(full_path)
    if not full_path_abs.startswith(base_dir_abs + os.sep):
        return
    index = _load_study_index(base_dir)
    changed = _sanitize_study_file(base_dir, full_path_abs, index)
    if changed:
        try:
            _save_study_index(base_dir, index)
        except OSError:
            pass


def get_study_library(static_root: str) -> dict[str, Any]:
    sanitize_study_library(static_root)
    base_dir = _study_base_dir(static_root)
    items_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}

    if os.path.isdir(base_dir):
        for root, _, files in os.walk(base_dir):
            rel_root = os.path.relpath(root, base_dir)
            rel_parts = [] if rel_root == "." else rel_root.split(os.sep)
            for name in sorted(files):
                if name.startswith("."):
                    continue
                ext = os.path.splitext(name)[1].lower()
                kind = _guess_type(ext)
                rel_path = os.path.join(rel_root, name) if rel_root != "." else name
                rel_url = "/study-assets/" + rel_path.replace(os.sep, "/")
                full_path = os.path.join(root, name)
                stat = os.stat(full_path)
                slug = os.path.splitext(name)[0]
                title = _title_from_slug(slug)
                category = _category_from_parts(
                    rel_parts[1:] if rel_parts[:1] in (["videos"], ["decks"], ["audio"], ["images"]) else rel_parts
                )
                modified = datetime.fromtimestamp(stat.st_mtime)
                item = {
                    "id": rel_path.replace(os.sep, "__").replace(".", "_"),
                    "title": title,
                    "slug": slug,
                    "kind": kind,
                    "kind_label": TYPE_META[kind]["label"],
                    "accent": TYPE_META[kind]["accent"],
                    "category": category,
                    "url": rel_url,
                    "preview_url": rel_url,
                    "download_url": rel_url,
                    "filename": name,
                    "size_bytes": stat.st_size,
                    "size_label": _format_size(stat.st_size),
                    "modified_ts": stat.st_mtime,
                    "modified_label": modified.strftime("%d %b %Y"),
                    "description": _description_for_item(title, kind, category),
                    "is_previewable": kind in {"video", "deck", "audio", "image"},
                }
                dedupe_key = _dedupe_key(title, kind, category)
                existing = items_by_key.get(dedupe_key)
                if existing is None or _should_replace(existing, item):
                    items_by_key[dedupe_key] = item

    items = list(items_by_key.values())
    items.sort(key=lambda item: item["modified_ts"], reverse=True)
    categories: dict[str, int] = {}
    for item in items:
        categories[item["category"]] = categories.get(item["category"], 0) + 1

    featured = items[0] if items else None
    stats = {
        "total_items": len(items),
        "videos": sum(1 for item in items if item["kind"] == "video"),
        "decks": sum(1 for item in items if item["kind"] == "deck"),
        "audio": sum(1 for item in items if item["kind"] == "audio"),
        "categories": len(categories),
    }

    return {
        "status": "ok",
        "featured": featured,
        "items": items,
        "categories": [{"name": name, "count": count} for name, count in sorted(categories.items())],
        "stats": stats,
    }
