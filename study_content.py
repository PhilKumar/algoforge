from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm"}
DECK_EXTS = {".pdf", ".ppt", ".pptx", ".key"}
AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".aac", ".ogg"}

TYPE_META = {
    "video": {"label": "Video Overview", "accent": "video"},
    "deck": {"label": "Slide Deck", "accent": "deck"},
    "audio": {"label": "Audio Brief", "accent": "audio"},
    "file": {"label": "Study Asset", "accent": "file"},
}


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
        return f"A NotebookLM video overview for {category.lower()} sessions around {title.lower()}."
    if kind == "deck":
        return f"A slide deck for a short market reset, focused on {title.lower()} and {category.lower()}."
    if kind == "audio":
        return f"An audio brief for slower review, built around {title.lower()} in {category.lower()}."
    return f"A study asset for {category.lower()} built around {title.lower()}."


def get_study_library(static_root: str) -> dict[str, Any]:
    base_dir = os.path.join(static_root, "notebooklm")
    items_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}

    if os.path.isdir(base_dir):
        for root, _, files in os.walk(base_dir):
            rel_root = os.path.relpath(root, base_dir)
            rel_parts = [] if rel_root == "." else rel_root.split(os.sep)
            for name in sorted(files):
                ext = os.path.splitext(name)[1].lower()
                kind = _guess_type(ext)
                rel_path = os.path.join(rel_root, name) if rel_root != "." else name
                rel_url = "/static/notebooklm/" + rel_path.replace(os.sep, "/")
                full_path = os.path.join(root, name)
                stat = os.stat(full_path)
                slug = os.path.splitext(name)[0]
                title = _title_from_slug(slug)
                category = _category_from_parts(
                    rel_parts[1:] if rel_parts[:1] in (["videos"], ["decks"], ["audio"]) else rel_parts
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
                    "is_previewable": kind in {"video", "deck", "audio"},
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
