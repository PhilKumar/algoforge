"""Reading a broker's holdings statement — what he OWNS, not what he owes.

The sanctuary has always counted debts. A holdings export is the other
side of the ledger: units held, what they cost, and what they are worth
this morning. Zerodha's Console export is the shape read here — a workbook
whose sheets are Equity, Mutual Funds and Combined, each opening with a
summary block and then a table of instruments.

Nothing is fetched live. A holding is worth what the statement said on the
day it was exported, and the page says which day that was, because a
number presented as today's when it is three months old is worse than no
number at all.
"""

from __future__ import annotations

import re
import zipfile

from sanctuary_emi import _safe_xml_root, _xlsx_cell_value

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_AS_ON_RE = re.compile(r"as on\s+(\d{4}-\d{2}-\d{2})", re.IGNORECASE)
# "Mutual Funds Holdings Statement", "Equity Holdings Statement"
_KIND_RE = re.compile(r"(mutual funds?|equity|combined)\s+holdings", re.IGNORECASE)
_SUMMARY_KEYS = {
    "invested value": "invested",
    "present value": "present",
    "unrealized p&l": "gain",
}


def _sheet_rows(archive: zipfile.ZipFile, path: str, shared: list[str]) -> list[list[str]]:
    root = _safe_xml_root(archive.read(path))
    rows = []
    for row in root.iter(f"{_NS}row"):
        cells = []
        for cell in row.iter(f"{_NS}c"):
            value = _xlsx_cell_value(cell, shared)
            cells.append("" if value is None else str(value))
        rows.append(cells)
    return rows


def _number(text: str) -> float | None:
    try:
        return round(float(str(text).replace(",", "").strip()), 4)
    except (TypeError, ValueError):
        return None


def parse_holdings(blob: bytes) -> dict | None:
    """Read a holdings workbook into its sheets, each with its own summary.

    Returns None unless at least one sheet carries a holdings title AND a
    summary — a workbook that half-reads would put invented money on the
    page, which is worse than asking him to try another export.
    """
    try:
        archive = zipfile.ZipFile(__import__("io").BytesIO(blob))
    except (zipfile.BadZipFile, ValueError):
        return None
    shared: list[str] = []
    if "xl/sharedStrings.xml" in archive.namelist():
        root = _safe_xml_root(archive.read("xl/sharedStrings.xml"))
        shared = ["".join(t.text or "" for t in si.iter(f"{_NS}t")) for si in root]

    sheets = []
    as_on = ""
    for path in sorted(n for n in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)):
        rows = _sheet_rows(archive, path, shared)
        flat = [" ".join(c for c in r if c) for r in rows]
        kind_hit = next((_KIND_RE.search(line) for line in flat if _KIND_RE.search(line)), None)
        if not kind_hit:
            continue
        kind = kind_hit.group(1).title().replace("Funds", "Funds")
        for line in flat:
            found = _AS_ON_RE.search(line)
            if found:
                as_on = found.group(1)
                break

        summary: dict[str, float] = {}
        header_at = None
        for index, row in enumerate(rows):
            cells = [c.strip() for c in row if c.strip()]
            if len(cells) >= 2:
                key = _SUMMARY_KEYS.get(cells[0].lower())
                if key and _number(cells[1]) is not None:
                    summary[key] = _number(cells[1])
            if cells and cells[0].lower() == "symbol":
                header_at = index
                break
        if not summary:
            continue

        holdings = []
        if header_at is not None:
            head = [c.strip().lower() for c in rows[header_at]]

            def col(*names, _head=head):
                for name in names:
                    if name in _head:
                        return _head.index(name)
                return None

            c_sym, c_qty = col("symbol"), col("quantity available")
            c_avg, c_ltp = col("average price"), col("previous closing price")
            for row in rows[header_at + 1 :]:
                if not row or c_sym is None or c_sym >= len(row) or not row[c_sym].strip():
                    continue
                qty = _number(row[c_qty]) if c_qty is not None and c_qty < len(row) else None
                avg = _number(row[c_avg]) if c_avg is not None and c_avg < len(row) else None
                ltp = _number(row[c_ltp]) if c_ltp is not None and c_ltp < len(row) else None
                if qty is None:
                    continue
                holdings.append(
                    {
                        "name": row[c_sym].strip()[:120],
                        "units": qty,
                        "avg_price": avg,
                        "price": ltp,
                        "invested": round(qty * avg, 2) if avg is not None else None,
                        "value": round(qty * ltp, 2) if ltp is not None else None,
                    }
                )
        sheets.append(
            {
                "kind": kind,
                "invested": summary.get("invested", 0.0),
                "present": summary.get("present", 0.0),
                "gain": summary.get("gain", 0.0),
                "holdings": holdings,
            }
        )

    # An empty sheet (his Equity side holds nothing) is noise, not news.
    sheets = [s for s in sheets if s["invested"] or s["present"] or s["holdings"]]
    if not sheets:
        return None
    # "Combined" repeats the others; it is a total, not a holding.
    detail = [s for s in sheets if not s["kind"].lower().startswith("combined")] or sheets
    return {
        "as_on": as_on,
        "sheets": detail,
        "invested": round(sum(s["invested"] for s in detail), 2),
        "present": round(sum(s["present"] for s in detail), 2),
        "gain": round(sum(s["gain"] for s in detail), 2),
    }
