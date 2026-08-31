"""Read the export the iPhone's own Journal app makes.

Apple keeps Journal entries in an encrypted store on the phone with no API
to read them — nothing can connect to it. What it does have is Export: the
app writes AppleJournalEntries.zip, holding one HTML file per entry and the
pictures they carry. This reads that zip and hands back plain entries.

The shape is Apple's, and Apple has changed it before, so nothing here
insists on one: the date is looked for in the entry's own <time>, then in
the line it prints at the top, then in the file's name, and only then in
the zip's own clock. An entry whose date cannot be found at all is still
returned — dateless, for the caller to decide about — because losing a
day's writing is worse than filing it in the wrong place.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
import zipfile
from datetime import date, datetime
from io import BytesIO

from bs4 import BeautifulSoup

# A phone's journal is a few hundred entries and a few hundred megabytes of
# pictures; these are the walls, not expectations.
MAX_ENTRIES = 5000
MAX_UNPACKED_BYTES = 600 * 1024 * 1024
MAX_PICTURE_BYTES = 40 * 1024 * 1024

_DATE_IN_TEXT = re.compile(
    r"(?:(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day,\s*)?" r"([A-Z][a-z]+)\s+(\d{1,2}),?\s+(\d{4})"
)
_DATE_IN_NAME = re.compile(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})")
_MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ],
        start=1,
    )
}
_PICTURE_SUFFIXES = (".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".tif", ".tiff", ".avif", ".bmp")


def _month(word: str) -> int | None:
    return _MONTHS.get(word.strip().lower())


def _date_from_text(text: str) -> date | None:
    """The day Journal prints at the head of an entry: "Saturday, 14 …"."""
    found = _DATE_IN_TEXT.search(text)
    if not found:
        return None
    month = _month(found.group(1))
    if not month:
        return None
    try:
        return date(int(found.group(3)), month, int(found.group(2)))
    except ValueError:
        return None


def _date_from_name(name: str) -> date | None:
    found = _DATE_IN_NAME.search(posixpath.basename(name))
    if not found:
        return None
    try:
        return date(int(found.group(1)), int(found.group(2)), int(found.group(3)))
    except ValueError:
        return None


def _date_from_html(soup: BeautifulSoup) -> date | None:
    for tag in soup.find_all("time"):
        stamp = (tag.get("datetime") or tag.get_text(" ", strip=True) or "").strip()
        if not stamp:
            continue
        try:
            return datetime.fromisoformat(stamp.replace("Z", "+00:00")).date()
        except ValueError:
            pass
        found = _date_from_text(stamp)
        if found:
            return found
    return None


def _readable(soup: BeautifulSoup) -> str:
    """The writing, with its paragraphs still separate."""
    for dead in soup(["script", "style", "head"]):
        dead.decompose()
    lines: list[str] = []
    for block in soup.find_all(["p", "div", "li", "h1", "h2", "h3", "blockquote", "figcaption"]):
        if block.find(["p", "div", "li"]):
            continue  # a wrapper; its children carry the words
        said = block.get_text(" ", strip=True)
        if said:
            lines.append(said)
    if not lines:
        whole = soup.get_text("\n", strip=True)
        lines = [line for line in whole.splitlines() if line.strip()]
    # Journal repeats the day and the title in the body; a repeated line
    # right after itself is the export's doing, not his.
    kept: list[str] = []
    for line in lines:
        if not kept or kept[-1] != line:
            kept.append(line)
    return "\n".join(kept)


def _pictures(soup: BeautifulSoup, entry_name: str, archive: zipfile.ZipFile, spent: list[int]) -> list[dict]:
    """Every picture an entry points at, fetched out of the same zip."""
    inside = {member.filename: member for member in archive.infolist() if not member.is_dir()}
    lowered = {name.lower(): name for name in inside}
    found: list[dict] = []
    seen: set[str] = set()
    for img in soup.find_all("img"):
        src = (img.get("src") or "").split("?")[0].strip()
        if not src or src.startswith("data:"):
            continue
        from urllib.parse import unquote

        wanted = posixpath.normpath(posixpath.join(posixpath.dirname(entry_name), unquote(src)))
        member = inside.get(wanted) or lowered.get(wanted.lower())
        if member is None:
            # Journal has moved its pictures between folders across versions;
            # the file's own name is the part that stays put.
            tail = posixpath.basename(wanted).lower()
            member = next((inside[n] for n in inside if n.lower().endswith("/" + tail) or n.lower() == tail), None)
        if member is None or member.filename in seen:
            continue
        if member.file_size > MAX_PICTURE_BYTES or spent[0] + member.file_size > MAX_UNPACKED_BYTES:
            continue
        seen.add(member.filename)
        with archive.open(member) as handle:
            blob = handle.read(MAX_PICTURE_BYTES + 1)
        if len(blob) > MAX_PICTURE_BYTES:
            continue
        spent[0] += len(blob)
        found.append(
            {
                "name": posixpath.basename(member.filename),
                "data": blob,
                "caption": (img.get("alt") or "").strip()[:300],
            }
        )
    return found


def _fingerprint(entry: dict) -> str:
    """What makes this entry itself, so a second import knows it again.

    Not the file's name: Journal renames on every export. The day it
    happened and the words on it do not change.
    """
    said = f"{entry['entry_date'] or 'undated'}\n{entry['title']}\n{entry['body']}\n"
    said += "\n".join(sorted(p["name"] for p in entry["photos"]))
    return hashlib.sha1(said.encode("utf-8"), usedforsecurity=False).hexdigest()


def read_export(blob: bytes) -> list[dict]:
    """Every entry in an AppleJournalEntries.zip, oldest first."""
    try:
        archive = zipfile.ZipFile(BytesIO(blob))
    except zipfile.BadZipFile as exc:
        raise ValueError("That is not a Journal export — it wants the zip the phone made.") from exc

    with archive:
        pages = [
            member
            for member in archive.infolist()
            if not member.is_dir()
            and member.filename.lower().endswith((".html", ".htm"))
            and posixpath.basename(member.filename).lower() not in ("index.html", "index.htm")
        ]
        if not pages:
            raise ValueError("No journal entries inside that zip.")
        pages.sort(key=lambda m: m.filename)
        spent = [0]
        entries: list[dict] = []
        for member in pages[:MAX_ENTRIES]:
            if spent[0] > MAX_UNPACKED_BYTES:
                break
            with archive.open(member) as handle:
                page = handle.read(4 * 1024 * 1024)
            spent[0] += len(page)
            soup = BeautifulSoup(page.decode("utf-8", errors="replace"), "html.parser")
            body = _readable(soup)
            when = _date_from_html(soup) or _date_from_text(body[:400]) or _date_from_name(member.filename)
            if when is None and member.date_time[0] >= 1980:
                when = date(*member.date_time[:3])
            heading = soup.find(["h1", "h2"])
            title = heading.get_text(" ", strip=True)[:300] if heading else ""
            # The head of the entry is the day and the title; the writing is
            # what is left once those have been said.
            lines = body.split("\n")
            while lines and (lines[0] == title or _date_from_text(lines[0]) is not None):
                lines.pop(0)
            if title and _date_from_text(title) is not None:
                title = ""
            entry = {
                "entry_date": when.isoformat() if when else "",
                "title": title,
                "body": "\n".join(lines).strip()[:20000],
                "photos": _pictures(soup, member.filename, archive, spent),
                "source": posixpath.basename(member.filename),
            }
            if not entry["body"] and not entry["title"] and not entry["photos"]:
                continue
            entry["fingerprint"] = _fingerprint(entry)
            entries.append(entry)

    entries.sort(key=lambda e: (e["entry_date"] or "9999", e["source"]))
    return entries


def picture_suffixes() -> tuple[str, ...]:
    return _PICTURE_SUFFIXES
