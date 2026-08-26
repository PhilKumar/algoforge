"""Bank-statement reader for the sanctuary's ledger backfill.

Reads the transaction listing a netbanking export carries — the old-format
binary .xls the Indian banks still hand out (ICICI's JasperReports export,
HDFC's OpTransactionHistory) and plain CSV — and turns each row into a ledger
candidate: date, narration, amount, running balance, and a ref_id that makes
re-uploading the same statement, or overlapping years, post nothing twice.

Every movement becomes a ledger candidate, marked with its direction:
"out" for withdrawals, "in" for deposits. Amounts stay positive — the
direction field says which way the money went, and the month view keeps
inflows out of the spending totals.
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
    {"match": "tangedco", "category": "EB bill"},
    {"match": "indane", "category": "Gas cylinder"},
    {"match": "bharatgas", "category": "Gas cylinder"},
    {"match": "hp gas", "category": "Gas cylinder"},
    {"match": "cylinder", "category": "Gas cylinder"},
    {"match": "dmart", "category": "Groceries"},
    {"match": "reliance retail", "category": "Groceries"},
    {"match": "reliance smart", "category": "Groceries"},
    {"match": "big bazaar", "category": "Groceries"},
    {"match": "medplus", "category": "Health"},
    {"match": "medical", "category": "Health"},
    {"match": "clinic", "category": "Health"},
    {"match": "diagnostic", "category": "Health"},
    {"match": "cra-nsdl", "category": "NPS"},
    {"match": "nps trust", "category": "NPS"},
    {"match": "protean", "category": "NPS"},
    {"match": "provident fund", "category": "PF"},
    {"match": "recharge", "category": "Mobile & Internet"},
    {"match": "aavin", "category": "Milk"},
    {"match": "milk", "category": "Milk"},
    {"match": "bookmyshow", "category": "Entertainment"},
    {"match": "book my show", "category": "Entertainment"},
    {"match": "dominos", "category": "Eating out"},
    {"match": "pizza", "category": "Eating out"},
    {"match": "kfc/", "category": "Eating out"},
    {"match": "mcdonald", "category": "Eating out"},
    {"match": "hotel", "category": "Eating out"},
    {"match": "tiffin", "category": "Eating out"},
    {"match": "indian clearing corp", "category": "Investments"},
    {"match": "salary", "category": "Salary"},
    {"match": "interest", "category": "Interest"},
    {"match": "dividend", "category": "Dividend"},
    {"match": "refund", "category": "Refund"},
    {"match": "cashback", "category": "Refund"},
    # Money that only moved between the user's own accounts. The category is
    # load-bearing: the ledger view leaves it out of the month's spending.
    {"match": "sweep to od", "category": "Self transfer"},
    {"match": "sweep from od", "category": "Self transfer"},
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
            # The bank half of a handle churns across exports (okax, oki,
            # okic are one person) — the name half is the identity.
            return token.split("@", 1)[0].lower()[:40] + "@"
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
        direction = "out" if withdrawal > 0 else "in"
        amount = withdrawal if direction == "out" else deposit
        if direction == "in":
            deposits_count += 1
            deposits_total = round(deposits_total + deposit, 2)
        # The fingerprint must name the TRANSACTION, not the export: serial
        # numbers restart in every fresh export (a Dec-31 top-up file counts
        # 1..6 where the year file said 460..465), so the serial stays out.
        # Balance separates same-day twins; where even the balance repeats,
        # the occurrence counter below does — and row order is chronological
        # in every export, so the counter is stable across files too. The
        # direction is included so a matching in and out never share a ref.
        fingerprint = "|".join(
            [
                when.date().isoformat(),
                note,
                direction,
                f"{amount:.2f}",
                "" if balance is None else f"{balance:.2f}",
            ]
        )
        rows.append(
            {
                "entry_date": when.date().isoformat(),
                "note": note[:500],
                "amount": amount,
                "dir": direction,
                "balance": balance,
                "_fp": fingerprint,
            }
        )
    if not rows:
        return {"status": "error", "error": "The table parsed but held no transaction rows."}
    seen_fp: dict[str, int] = {}
    for row in rows:
        occurrence = seen_fp.get(row["_fp"], 0)
        seen_fp[row["_fp"]] = occurrence + 1
        digest = hashlib.sha1(f"{row.pop('_fp')}#{occurrence}".encode()).hexdigest()[:16]  # nosec B324 - dedupe key, not security
        row["ref_id"] = f"stmt:{acct_tail}:{digest}"
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
