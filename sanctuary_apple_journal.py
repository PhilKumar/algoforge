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

# The header the app prints. His phone writes "Saturday, 14 September 2024";
# an American one writes "September 14, 2024". Both have to read.
_WEEKDAY = r"(?:(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day,?\s*)?"
_DAY_FIRST = re.compile(_WEEKDAY + r"(\d{1,2})\s+([A-Z][a-z]+),?\s+(\d{4})")
_MONTH_FIRST = re.compile(_WEEKDAY + r"([A-Z][a-z]+)\s+(\d{1,2}),?\s+(\d{4})")
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


def _is_a_mac_shadow(name: str) -> bool:
    """AppleDouble leftovers: __MACOSX/… and ._whatever."""
    return name.startswith("__MACOSX/") or "/__MACOSX/" in name or posixpath.basename(name).startswith("._")


def _month(word: str) -> int | None:
    return _MONTHS.get(word.strip().lower())


def _date_from_text(text: str) -> date | None:
    """The day Journal prints at the head of an entry."""
    for pattern, day_first in ((_DAY_FIRST, True), (_MONTH_FIRST, False)):
        found = pattern.search(text)
        if not found:
            continue
        day, name = (found.group(1), found.group(2)) if day_first else (found.group(2), found.group(1))
        month = _month(name)
        if not month:
            continue
        try:
            return date(int(found.group(3)), month, int(day))
        except ValueError:
            continue
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


def _pictures(
    soup: BeautifulSoup,
    entry_name: str,
    archive: zipfile.ZipFile,
    spent: list[int],
    with_bytes: bool = True,
) -> list[dict]:
    """Every picture an entry points at, named — and read, if asked.

    A hundred and twenty six photographs off a phone are half a gigabyte
    decoded. Counting them is not the same as holding them, so the count
    comes back on its own and the bytes are fetched one at a time, when
    there is somewhere to put them.
    """
    inside = {member.filename: member for member in archive.infolist() if not member.is_dir()}
    lowered = {name.lower(): name for name in inside}
    found: list[dict] = []
    seen: set[str] = set()
    for img in _his_own_pictures(soup):
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
        picture = {
            "name": posixpath.basename(member.filename),
            "member": member.filename,
            "caption": (img.get("alt") or "").strip()[:300],
        }
        if with_bytes:
            with archive.open(member) as handle:
                blob = handle.read(MAX_PICTURE_BYTES + 1)
            if len(blob) > MAX_PICTURE_BYTES:
                continue
            spent[0] += len(blob)
            picture["data"] = blob
        found.append(picture)
    return found


# What the app calls its own parts. A real export names them; an export from
# some later iOS may not, and everything below falls back to reading the page
# the way any page would be read.
_HEADER_CLASS = "pageHeader"
_TITLE_CLASS = "title"
_CONTAINER_CLASS = "pageContainer"
_PICTURE_KINDS = ("assetType_photo", "assetType_livePhoto")
_UNTITLED = re.compile(r"^\(\d+\)$")


def _his_own_pictures(soup: BeautifulSoup) -> list:
    """The photographs, and not the app's furniture.

    A music attachment carries its cover art, an audio note a play button, a
    link its preview — all of them .heic files sitting in Resources beside
    the real pictures. Only what the export marks as a photograph is his.
    """
    grids = soup.find_all(class_="gridItem")
    if not grids:
        return soup.find_all("img")
    wanted = []
    for item in grids:
        kinds = set(item.get("class") or [])
        if kinds.intersection(_PICTURE_KINDS):
            wanted.extend(item.find_all("img", class_="asset_image") or item.find_all("img"))
    loose = [img for img in soup.find_all("img") if not img.find_parent(class_="gridItem")]
    return wanted + [img for img in loose if "asset_image" in (img.get("class") or [])]


def _the_rest_of_it(soup: BeautifulSoup) -> dict:
    """What the entry carried besides writing and photographs.

    A video cannot come over — there is nowhere to keep one — so it is
    counted and said out loud rather than dropped in silence. A link and a
    state of mind are words, and words can be kept.
    """
    links, felt, videos = [], [], 0
    for item in soup.find_all(class_="gridItem"):
        kinds = set(item.get("class") or [])
        if "assetType_video" in kinds:
            videos += 1
        if "assetType_link" in kinds:
            anchor_tag = item.find("a", href=True)
            if anchor_tag:
                links.append(anchor_tag["href"].strip())
        if "assetType_stateOfMind" in kinds:
            said = [d.get_text(" ", strip=True) for d in item.find_all(class_="gridItemOverlayText")]
            said = [word for word in said if word]
            if said:
                felt.append(" · ".join(said))
    return {"links": links, "felt": felt, "videos": videos}


def _fingerprint(entry: dict) -> str:
    """What makes this entry itself, so a second import knows it again.

    Not the file's name: Journal renames on every export. The day it
    happened and the words on it do not change.
    """
    said = f"{entry['entry_date'] or 'undated'}\n{entry['title']}\n{entry['body']}\n"
    said += "\n".join(sorted(p["name"] for p in entry["photos"]))
    return hashlib.sha1(said.encode("utf-8"), usedforsecurity=False).hexdigest()


def read_export(source, with_pictures: bool = True) -> list[dict]:
    """Every entry in an AppleJournalEntries.zip, oldest first.

    `source` is the export's bytes or the path to it. A phone's journal is
    bigger than the machine this runs on has to spare, so the path is the
    way in that matters: the zip stays on disk and is read from there.
    """
    try:
        archive = zipfile.ZipFile(BytesIO(source) if isinstance(source, (bytes, bytearray)) else source)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError("That is not a Journal export — it wants the zip the phone made.") from exc

    with archive:
        pages = [
            member
            for member in archive.infolist()
            if not member.is_dir()
            and member.filename.lower().endswith((".html", ".htm"))
            and posixpath.basename(member.filename).lower() not in ("index.html", "index.htm")
            # A zip made on a Mac carries a shadow copy of every file under
            # __MACOSX; read as entries they would double his whole journal.
            and not _is_a_mac_shadow(member.filename)
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
            photos = _pictures(soup, member.filename, archive, spent, with_pictures)
            carried = _the_rest_of_it(soup)

            header = soup.find(class_=_HEADER_CLASS)
            said_date = header.get_text(" ", strip=True) if header else ""
            # The name of the file is the day and the title Apple gave it:
            # 2024-09-14_Sedentary_lifestyle.html. Both are worth having.
            stem = posixpath.splitext(posixpath.basename(member.filename))[0]
            named = stem.split("_", 1)
            from_name = named[1].replace("_", " ").strip() if len(named) > 1 else ""
            if _UNTITLED.match(from_name):
                from_name = ""  # (1), (2) — a second entry on one day, not a title

            when = _date_from_text(said_date) or _date_from_html(soup) or _date_from_name(member.filename)
            # The container holds the day, the pictures, and two empty boxes
            # where Apple's own title and body would be; the writing itself
            # sits outside it, in the spans the exporter wrapped it in.
            marked = soup.find(class_=_TITLE_CLASS)
            titled = marked.get_text(" ", strip=True) if marked else ""
            if not titled:
                heading = soup.find(["h1", "h2"])
                titled = heading.get_text(" ", strip=True) if heading else ""
            container = soup.find(class_=_CONTAINER_CLASS)
            if container:
                container.decompose()
            body = _readable(soup)
            if when is None:
                when = _date_from_text(body[:400])
            if when is None and member.date_time[0] >= 1980:
                when = date(*member.date_time[:3])

            lines = [line for line in body.split("\n") if line.strip()]
            title = titled or from_name
            # Only a page that names none of its parts is read by guesswork.
            # Where the export marks them, an entry with no title has none —
            # borrowing its first sentence puts words in his mouth.
            if not title and header is None and len(lines) > 1 and len(lines[0]) <= 80:
                title = lines[0]
            while lines and (lines[0] == title or _date_from_text(lines[0]) is not None):
                lines.pop(0)
            if title and _date_from_text(title) is not None:
                title = ""
            for felt in carried["felt"]:
                lines.append(f"Felt: {felt}")
            lines.extend(carried["links"])
            entry = {
                "entry_date": when.isoformat() if when else "",
                "title": title[:300],
                "body": "\n".join(lines).strip()[:20000],
                "photos": photos,
                "videos": carried["videos"],
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


def picture_bytes(path: str, member: str) -> bytes:
    """One picture out of the export, read on its own.

    The importer asks for these one at a time and lets go of each before the
    next, so a journal of any size costs one photograph of memory.
    """
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo(member)
        if info.file_size > MAX_PICTURE_BYTES:
            raise ValueError("picture too large")
        with archive.open(info) as handle:
            return handle.read(MAX_PICTURE_BYTES + 1)[:MAX_PICTURE_BYTES]
