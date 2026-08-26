"""Bank-statement reader for the sanctuary's ledger backfill.

Reads the transaction listing a netbanking export carries — the old-format
binary .xls the Indian banks still hand out (ICICI's JasperReports export,
HDFC's OpTransactionHistory) and plain CSV — and turns each row into a ledger
candidate: date, narration, amount, running balance, and a ref_id that makes
re-uploading the same statement, or overlapping years, post nothing twice.

Only withdrawals become ledger candidates. The ledger is a spending book —
every amount in it is positive money out — so deposits are counted and
reported in the parse result for the preview, never posted.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import datetime

# ── Header recognition ───────────────────────────────────────────
# A statement's table announces itself with a header row. Column names vary
# by bank, so each field carries its synonyms; a row that matches at least
# date + narration + one money column is a header.
_HEADER_NAMES = {
    "date": ["transaction date", "txn date", "value date", "date"],
    "note": ["transaction remarks", "narration", "description", "particulars", "remarks"],
    "withdrawal": ["withdrawal amount", "withdrawal", "debit", "dr amount", "dr"],
    "deposit": ["deposit amount", "deposit", "credit", "cr amount", "cr"],
    "balance": ["balance", "closing balance", "running balance"],
    "serial": ["s no", "s.no", "sr no", "sl no", "sr.no"],
}

_DATE_FORMATS = ("%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%b-%Y", "%d %b %Y", "%Y-%m-%d")
_ACCT_RE = re.compile(r"\b(\d{9,18})\b")
_MONEY_RE = re.compile(r"^-?[\d,]+\.?\d*$")

# ── Categorisation ───────────────────────────────────────────────
# First match wins, user-taught rules are checked before these. The match is
# a case-insensitive substring of the narration. These defaults cover what a
# narration says on its own; everything else lands in Uncategorised for the
# review row, where a correction becomes a taught rule.
DEFAULT_RULES = [
    {"match": "apollo phar", "category": "Health"},
    {"match": "pharmacy", "category": "Health"},
    {"match": "hospital", "category": "Health"},
    {"match": "tithe", "category": "Tithe"},
    {"match": "yeshu", "category": "Tithe"},
    {"match": "church", "category": "Tithe"},
    {"match": "jio", "category": "Mobile & Internet"},
    {"match": "airtel", "category": "Mobile & Internet"},
    {"match": "bsnl", "category": "Mobile & Internet"},
    {"match": "amazon", "category": "Shopping"},
    {"match": "flipkart", "category": "Shopping"},
    {"match": "myntra", "category": "Shopping"},
    {"match": "swiggy", "category": "Eating out"},
    {"match": "zomato", "category": "Eating out"},
    {"match": "hdfc bank limited", "category": "HDFC loan"},
    {"match": "kotakmahprime", "category": "Kotak loan"},
    {"match": "bajaj fin", "category": "Bajaj loan"},
    {"match": "lic of india", "category": "Insurance"},
    {"match": "lic prem", "category": "Insurance"},
    {"match": "policybazaar", "category": "Insurance"},
    {"match": "family", "category": "Family transfer"},
    {"match": "petrol", "category": "Fuel"},
    {"match": "fuel", "category": "Fuel"},
    {"match": "indianoil", "category": "Fuel"},
    {"match": "bharat petro", "category": "Fuel"},
    {"match": "hind petro", "category": "Fuel"},
    {"match": "irctc", "category": "Travel"},
    {"match": "redbus", "category": "Travel"},
    {"match": "makemytrip", "category": "Travel"},
    {"match": "ola", "category": "Auto & Cab"},
    {"match": "uber", "category": "Auto & Cab"},
    {"match": "rapido", "category": "Auto & Cab"},
    {"match": "tneb", "category": "EB bill"},
    {"match": "electricity", "category": "EB bill"},
    {"match": "school", "category": "School fees"},
    {"match": "netflix", "category": "Subscriptions"},
    {"match": "spotify", "category": "Subscriptions"},
    {"match": "hotstar", "category": "Subscriptions"},
    {"match": "atm", "category": "Cash withdrawal"},
    {"match": "atw-", "category": "Cash withdrawal"},
    {"match": "nwd-", "category": "Cash withdrawal"},
    {"match": "cash wdl", "category": "Cash withdrawal"},
    {"match": "cred.club", "category": "Credit card bill"},
    {"match": "credpay", "category": "Credit card bill"},
    {"match": "cred@", "category": "Credit card bill"},
    {"match": "via cred", "category": "Credit card bill"},
    {"match": "payment on cred", "category": "Credit card bill"},
    {"match": "zerodha", "category": "Investments"},
    {"match": "groww", "category": "Investments"},
    {"match": "canfinhomes", "category": "Home loan"},
    {"match": "can fin homes", "category": "Home loan"},
    # Money that only moved between the user's own accounts. The category is
    # load-bearing: the ledger view leaves it out of the month's spending.
    {"match": "sweep to od", "category": "Self transfer"},
]


UNCATEGORISED = "Uncategorised"


def payee_key(note: str) -> str:
    """A stable token for grouping narrations by who was paid.

    A UPI narration names the payee by handle (someone@bank); that token
    survives across every payment to the same person while the reference
    number and remark change. Without a handle, fall back to the first
    slash-segment that isn't a scheme word or a number — for BIL/NFS/ACH
    rows that's the biller's name.
    """
    text = (note or "").strip()
    for token in text.replace("/", " ").split():
        if "@" in token:
            return token.lower()[:40]
    scheme_words = {"upi", "bil", "onl", "nfs", "mmt", "imps", "ach", "mps", "neft", "rtgs", "inft"}
    for part in text.split("/"):
        cleaned = part.strip().lower()
        if cleaned and cleaned not in scheme_words and not re.fullmatch(r"[\d-]+", cleaned):
            return cleaned[:40]
    return text.lower()[:40]


def categorise(note: str, user_rules: list[dict] | None = None) -> str:
    text = (note or "").lower()
    for rule in list(user_rules or []) + DEFAULT_RULES:
        match = str(rule.get("match") or "").lower().strip()
        if match and match in text:
            return str(rule.get("category") or UNCATEGORISED)[:60]
    return UNCATEGORISED


# ── Parsing ──────────────────────────────────────────────────────


def _parse_date(value: str) -> datetime | None:
    text = str(value or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _parse_money(value: str) -> float | None:
    text = str(value or "").strip().replace(",", "")
    if not text or not _MONEY_RE.match(text.replace(",", "")):
        return None
    try:
        return round(float(text), 2)
    except ValueError:
        return None


def _match_header(cells: list[str]) -> dict[str, int] | None:
    """Map field -> column index if this row looks like the table header."""
    lowered = [re.sub(r"\s+", " ", c).strip().lower() for c in cells]
    mapping: dict[str, int] = {}
    for field, names in _HEADER_NAMES.items():
        best = (0, -1)
        for idx, cell in enumerate(lowered):
            if idx in mapping.values() or not cell:
                continue
            for name in names:
                if cell == name:
                    score = 1000 + len(name)
                elif name in cell:
                    score = len(name)
                else:
                    continue
                if score > best[0]:
                    best = (score, idx)
        if best[1] >= 0:
            mapping[field] = best[1]
    required = {"date", "note"} <= set(mapping)
    money = {"withdrawal", "deposit"} & set(mapping)
    return mapping if required and money else None


def _rows_to_result(filename: str, grid: list[list[str]]) -> dict:
    account = ""
    header: dict[str, int] | None = None
    header_at = -1
    for r, cells in enumerate(grid):
        joined = " ".join(cells)
        if not account and "account" in joined.lower():
            found = _ACCT_RE.search(joined)
            if found:
                account = found.group(1)
        candidate = _match_header(cells)
        if candidate and header is None:
            header, header_at = candidate, r
    if header is None:
        return {"status": "error", "error": "No transaction table found in this file."}

    acct_tail = account[-6:] if account else "unknown"
    rows: list[dict] = []
    deposits_count = 0
    deposits_total = 0.0
    for cells in grid[header_at + 1 :]:

        def cell(field: str) -> str:
            idx = header.get(field, -1)
            return cells[idx].strip() if 0 <= idx < len(cells) else ""

        when = _parse_date(cell("date"))
        if when is None:
            continue
        withdrawal = _parse_money(cell("withdrawal")) or 0.0
        deposit = _parse_money(cell("deposit")) or 0.0
        balance = _parse_money(cell("balance"))
        note = re.sub(r"\s+", " ", cell("note")).strip()
        if withdrawal <= 0 and deposit <= 0:
            continue
        if deposit > 0 and withdrawal <= 0:
            deposits_count += 1
            deposits_total = round(deposits_total + deposit, 2)
            continue
        # The serial and balance keep two same-amount same-day payments to the
        # same payee distinct, and keep the SAME row identical across the
        # year-file overlap a bank puts at the boundary.
        fingerprint = "|".join(
            [
                when.date().isoformat(),
                cell("serial"),
                note,
                f"{withdrawal:.2f}",
                "" if balance is None else f"{balance:.2f}",
            ]
        )
        digest = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:16]  # nosec B324 - dedupe key, not security
        rows.append(
            {
                "entry_date": when.date().isoformat(),
                "note": note[:500],
                "amount": withdrawal,
                "balance": balance,
                "ref_id": f"stmt:{acct_tail}:{digest}",
            }
        )
    if not rows:
        return {"status": "error", "error": "The table parsed but held no withdrawal rows."}
    rows.sort(key=lambda r: r["entry_date"])
    return {
        "status": "ok",
        "filename": filename,
        "account": account,
        "account_tail": acct_tail,
        "date_from": rows[0]["entry_date"],
        "date_to": rows[-1]["entry_date"],
        "rows": rows,
        "deposits_count": deposits_count,
        "deposits_total": deposits_total,
    }


def _grid_from_xls(blob: bytes) -> list[list[str]]:
    import xlrd

    book = xlrd.open_workbook(file_contents=blob)
    sheet = book.sheet_by_index(0)
    grid = []
    for r in range(sheet.nrows):
        row = []
        for c in range(sheet.ncols):
            value = sheet.cell_value(r, c)
            if isinstance(value, float) and value == int(value):
                value = int(value)
            row.append(str(value))
        grid.append(row)
    return grid


def _grid_from_csv(blob: bytes) -> list[list[str]]:
    text = blob.decode("utf-8-sig", errors="replace")
    return [list(row) for row in csv.reader(io.StringIO(text))]


def parse_statement(filename: str, blob: bytes) -> dict:
    """Parse one uploaded statement into ledger candidates."""
    name = (filename or "").lower()
    try:
        if name.endswith(".csv"):
            grid = _grid_from_csv(blob)
        elif name.endswith(".xls"):
            grid = _grid_from_xls(blob)
        elif name.endswith(".xlsx"):
            return {
                "status": "error",
                "error": "Modern .xlsx isn't supported yet — export the .xls or CSV form.",
            }
        else:
            return {"status": "error", "error": "Drop a .xls or .csv statement export."}
    except Exception as exc:  # noqa: BLE001 - the upload is untrusted input
        return {"status": "error", "error": f"Could not read the file: {exc}"}
    return _rows_to_result(filename, grid)
