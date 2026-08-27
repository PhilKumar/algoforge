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
    "withdrawal": ["withdrawal amount", "withdrawal", "debit", "dr amount"],
    "deposit": ["deposit amount", "deposit", "credit", "cr amount"],
    "balance": ["balance", "closing balance", "running balance"],
    "serial": ["s no", "s.no", "sr no", "sl no", "sl. no", "sr.no"],
    # Some banks (Kotak) print one Amount column and say which way it went
    # in a Dr / Cr flag beside it. These sit last so the classic columns
    # claim their headers first; the flag after Amount wins over the one
    # after Balance because assignment is first-strongest.
    "amount": ["amount"],
    "drcr": ["dr / cr", "dr/cr"],
}

_DATE_FORMATS = ("%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%b-%Y", "%d %b %Y", "%Y-%m-%d")
_ACCT_RE = re.compile(r"\b(\d{9,18})\b")
_LINKED_ACCT_RE = re.compile(r"\b(\d{9,18})\s*:\s*rev sweep", re.IGNORECASE)
# ICICI's INF narration is an internal transfer and names the other account:
# "INF/<reference>/ <account>/<holder name>".
_INF_ACCT_RE = re.compile(r"\binf/\d+/\s*(\d{9,18})\b", re.IGNORECASE)
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
    {"match": "spln", "category": "Kotak loan"},
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
    # His brokers: ICICI's BRK/ scheme rows, and the IMPS names the broker
    # prints when money comes back. Both directions are Investments — the
    # month view treats the category as saving, not spending.
    {"match": "brk/", "category": "Investments"},
    {"match": "raisesecur", "category": "Investments"},
    {"match": "moneylicio", "category": "Investments"},
    # NEFTCC- is a card bill paid by NEFT; PayZapp is his own wallet.
    {"match": "neftcc-", "category": "Credit card bill"},
    {"match": "payzapp", "category": "Self transfer"},
    # RTGS from "HDFC BANK LTD RA OPS" is a loan being disbursed — the
    # retail-assets desk, not a purchase. Angel One's CMS payout is the
    # broker returning money. PHILIPRANJ is his own account at Axis, the
    # spelling his bank's narration actually prints.
    {"match": "hdfc bank ltd ra", "category": "HDFC loan"},
    {"match": "angel one", "category": "Investments"},
    {"match": "philipranj", "category": "Self transfer"},
    {"match": "salary", "category": "Salary"},
    # His pay arrives named by the employer, never by the word "salary" —
    # "NEFT-...-KYNDRYL SOLUTIONS-..." is what a month's pay looks like.
    {"match": "kyndryl", "category": "Salary"},
    {"match": "ibm india", "category": "Salary"},
    {"match": "interest", "category": "Interest"},
    {"match": "dividend", "category": "Dividend"},
    {"match": "refund", "category": "Refund"},
    {"match": "cashback", "category": "Refund"},
    # Money that only moved between the user's own accounts. The category is
    # load-bearing: the ledger view leaves it out of the month's spending.
    {"match": "sweep to od", "category": "Self transfer"},
    {"match": "sweep from od", "category": "Self transfer"},
    {"match": "rev sweep", "category": "Self transfer"},
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
    for candidate in (text, text.split(" ")[0]):
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(candidate, fmt)
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
    money = {"withdrawal", "deposit"} & set(mapping) or {"amount", "drcr"} <= set(mapping)
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
        if withdrawal <= 0 and deposit <= 0:
            flagged = _parse_money(cell("amount")) or 0.0
            flag = cell("drcr").strip().lower()
            if flagged > 0 and flag in ("dr", "debit"):
                withdrawal = flagged
            elif flagged > 0 and flag in ("cr", "credit"):
                deposit = flagged
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
    linked_kinds: dict[str, str] = {}
    for row in rows:
        m = _LINKED_ACCT_RE.search(row["note"])
        if m:
            linked_kinds[m.group(1)] = "Sweep-linked overdraft (OD)"
        m = _INF_ACCT_RE.search(row["note"])
        if m:
            linked_kinds.setdefault(m.group(1), "Linked account (internal transfer)")
    linked = [{"number": n, "kind": k} for n, k in sorted(linked_kinds.items())]
    return {
        "status": "ok",
        "filename": filename,
        "account": account,
        "linked_accounts": linked,
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


# ── Loan discovery: the statements remember every EMI he ever paid ─

_LOANISH_RE = re.compile(
    r"ach|nach|ecs|emi|loan|instal|prime|canfin|can fin|finserv|fin serv|bajaj|finance",
    re.IGNORECASE,
)
_NOT_A_LOAN_RE = re.compile(r"zerodha|clearing corp|mutual fund|cra-nsdl|nps|groww|sip[/ ]", re.IGNORECASE)
_LOAN_NUM_RE = re.compile(r"(\d{6,})")


_SCHEME_TOKENS = {"ach", "nach", "ecs", "decs", "decs dr", "dr", "upi", "bil", "onl", "mmt", "imps", "tp"}


def _lender_key(note: str) -> str:
    for part in note.split("/"):
        cleaned = re.sub(r"[^a-z ]", "", part.strip().lower()).strip()
        if len(cleaned) >= 4 and cleaned not in _SCHEME_TOKENS:
            return cleaned[:40]
    return payee_key(note)


def discover_loans(rows: list[dict], today: "datetime.date") -> list[dict]:
    """Find EMI-shaped streams in outflow rows: regular near-monthly debits
    of a steady amount to one loan account. Keyed on the LAST long number in
    the narration — the loan account — because a bank's narration format
    changes mid-life while the loan number survives it (Can Fin's 42 debits
    arrived under two formats; the trailing number says they are one loan).
    A stream silent for 75+ days reads as closed.
    """
    import statistics
    from datetime import datetime as _dt

    # Two passes. A narration carries several numbers: the loan account and
    # per-debit references. The reference is unique to its row; the loan
    # account repeats every month — so each row keys on whichever of its
    # numbers is seen most often across the whole tape.
    loanish = []
    seen_numbers: dict[str, int] = {}
    for row in rows:
        note = row["note"]
        if not _LOANISH_RE.search(note) or _NOT_A_LOAN_RE.search(note):
            continue
        numbers = _LOAN_NUM_RE.findall(note)
        loanish.append((row, numbers))
        for number in set(numbers):
            seen_numbers[number] = seen_numbers.get(number, 0) + 1
    groups: dict[str, list] = {}
    for row, numbers in loanish:
        best = max(numbers, key=lambda n: (seen_numbers[n], len(n))) if numbers else None
        # A number that never repeats is a per-debit reference, not a loan
        # account (the old Can Fin era stamped a fresh reference on all 68
        # debits) — those rows key on the lender's name instead.
        if best is not None and seen_numbers[best] >= 2:
            key = best
        else:
            key = _lender_key(row["note"])
        groups.setdefault(key, []).append(row)
    # The same lender's name arrives long and short across narration formats
    # ("TP CAN FIN" / "TP CAN FIN HOMES LTD") — fold the shorter into the
    # longer when one begins with the other.
    for short in sorted([k for k in groups if not k.isdigit()], key=len):
        for long in sorted([k for k in groups if not k.isdigit() and k != short], key=len, reverse=True):
            if len(short) >= 6 and long.startswith(short):
                groups[long] += groups.pop(short)
                break

    candidates = []
    for key, items in groups.items():
        if len(items) < 4:
            continue
        items.sort(key=lambda r: r["entry_date"])
        # One mandate can carry several truths at once: two loans running in
        # parallel at different EMIs, a bulk closure payment, a refund. So
        # the debits cluster by AMOUNT LEVEL first — each steady level is a
        # candidate stream of its own — and levels too small to be a loan
        # (the one-off bulk payment) fall away.
        levels: list[list[dict]] = []
        for row in sorted(items, key=lambda r: r["amount"]):
            placed = next(
                (
                    lvl
                    for lvl in levels
                    if abs(row["amount"] - statistics.median(x["amount"] for x in lvl))
                    <= 0.35 * max(statistics.median(x["amount"] for x in lvl), 1)
                ),
                None,
            )
            (placed.append(row) if placed else levels.append([row]))
        level_cands = []
        for level in levels:
            if len(level) < 4:
                continue
            level.sort(key=lambda r: r["entry_date"])
            med = statistics.median(r["amount"] for r in level)
            if med < 500:
                continue
            dates = [_dt.strptime(r["entry_date"], "%Y-%m-%d").date() for r in level]
            gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
            if not gaps or not 20 <= statistics.median(gaps) <= 45:
                continue
            level_cands.append({"items": level, "emi": med, "first": dates[0], "last": dates[-1]})
        # Two levels that never overlap in time and sit within 90 days of
        # each other are one loan whose EMI was rewritten (a restructure, a
        # moratorium reset). Levels that overlap ran side by side: two loans.
        level_cands.sort(key=lambda c: c["first"])
        joined: list[dict] = []
        for cand in level_cands:
            prev = next(
                (j for j in joined if 0 <= (cand["first"] - j["last"]).days <= 90),
                None,
            )
            if prev:
                prev["items"] += cand["items"]
                prev["emi"] = cand["emi"]
                prev["last"] = cand["last"]
            else:
                joined.append(cand)
        for cand in joined:
            items = sorted(cand["items"], key=lambda r: r["entry_date"])
            med = cand["emi"]
            _emit(candidates, key, items, med, today)
    return _merge_and_sort(candidates)


def _emit(candidates: list, key: str, items: list[dict], med: float, today) -> None:
    from datetime import datetime as _dt

    dates = [_dt.strptime(r["entry_date"], "%Y-%m-%d").date() for r in items]
    lender = ""
    for part in items[-1]["note"].split("/"):
        cleaned = part.strip()
        if (
            cleaned
            and not cleaned.isdigit()
            and cleaned.upper() not in ("ACH", "NACH", "ECS", "TP", "UPI", "BIL", "ONL")
        ):
            lender = cleaned[:60]
            break
    candidates.append(
        {
            "key": key,
            "lender": lender,
            "count": len(items),
            "emi": round(med, 2),
            "first": items[0]["entry_date"],
            "last": items[-1]["entry_date"],
            "closed": (today - dates[-1]).days > 75,
            "debits": [{"due_date": r["entry_date"], "amount": r["amount"]} for r in items],
            "sample": items[-1]["note"][:120],
        }
    )


def _merge_and_sort(candidates: list[dict]) -> list[dict]:
    import statistics  # noqa: F401 - parallel to discover_loans' imports
    from datetime import datetime as _dt

    # One loan, two narration eras: same money, spans that touch — merge.
    candidates.sort(key=lambda c: c["first"])
    merged: list[dict] = []
    for cand in candidates:
        prev = next(
            (
                m
                for m in merged
                if abs(m["emi"] - cand["emi"]) <= 0.03 * max(m["emi"], 1)
                and 0
                <= (_dt.strptime(cand["first"], "%Y-%m-%d").date() - _dt.strptime(m["last"], "%Y-%m-%d").date()).days
                <= 90
            ),
            None,
        )
        if prev:
            prev["count"] += cand["count"]
            prev["last"] = cand["last"]
            prev["closed"] = cand["closed"]
            prev["debits"] += cand["debits"]
            prev["sample"] = cand["sample"]
        else:
            merged.append(cand)
    merged.sort(key=lambda c: (-c["closed"], c["last"]), reverse=True)
    return merged


# ── A card's own loan schedule ───────────────────────────────────
#
# A credit card that lends against itself issues a schedule of its own:
# HDFC's "LINKED LOANS" table names the loan, what it cost, what is still
# owed, and every instalment to the last one. Read straight, it puts a debt
# on the shelf with its real dates instead of a stream guessed from debits.

_CARD_LOAN_HEAD_RE = re.compile(
    r"(\d{10,25})\s+(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})\s+([A-Za-z]+)\s+"
    r"([\d,]+\.\d{2})\s+([\d.]+)\s+(\d+)\s+([\d,]+\.\d{2})"
)
_CARD_LOAN_EMI_RE = re.compile(r"^([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})\s*$")
_CARD_TAIL_RE = re.compile(r"(?:^|[_\s-])(\d{4})(?:[_\s-]|$)")


def _card_loan_date(text: str) -> str:
    return datetime.strptime(re.sub(r"\s+", " ", text.strip()), "%d %b %Y").date().isoformat()


def parse_card_loan_schedule(text: str, filename: str = "") -> dict | None:
    """Read a card's linked-loan table into a debt and its instalments.

    Returns None unless the header AND at least one instalment are found —
    a half-read schedule would put a wrong debt on the shelf, which is
    worse than leaving him to type it.
    """
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    head = None
    for line in lines:
        head = _CARD_LOAN_HEAD_RE.search(line)
        if head:
            break
    if not head:
        return None

    emis = []
    for line in lines:
        row = _CARD_LOAN_EMI_RE.match(line)
        if not row:
            continue
        principal = _parse_money(row.group(1)) or 0.0
        interest = _parse_money(row.group(2)) or 0.0
        emis.append(
            {
                "due_date": _card_loan_date(row.group(3)),
                "amount": round(principal + interest, 2),
                "principal": principal,
                "interest": interest,
            }
        )
    if not emis:
        return None
    emis.sort(key=lambda e: e["due_date"])

    # The table prints no running balance, but it does print each
    # instalment's principal — so the balance is computed: what was
    # borrowed, less every principal rupee paid through that row. The
    # loan card reads its OUTSTANDING from exactly this column.
    principal_total = _parse_money(head.group(4)) or 0.0
    remaining = principal_total
    for emi in emis:
        remaining = max(0.0, round(remaining - emi["principal"], 2))
        emi["outstanding"] = remaining

    # The card is named by the file the bank hands over ("LINKED LOANS_1234_…"),
    # never inside the table itself.
    stem = (filename or "").rsplit("/", 1)[-1]
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", stem)
    tail = ""
    for part in re.split(r"[_\s]+", stem):
        if re.fullmatch(r"\d{4}", part):
            tail = part
            break

    return {
        "loan_number": head.group(1).lstrip("0") or head.group(1),
        "booked": _card_loan_date(head.group(2)),
        "kind": head.group(3).title(),
        "principal": _parse_money(head.group(4)),
        "rate": float(head.group(5)),
        "tenure": int(head.group(6)),
        "outstanding": _parse_money(head.group(7)),
        "card_tail": tail,
        "emi": emis[0]["amount"],
        "first": emis[0]["due_date"],
        "last": emis[-1]["due_date"],
        "emis": emis,
    }


# ── Accounts named inside a narration ────────────────────────────
#
# An RTGS or NEFT narration names the other side in full: the reference, the
# holder, then the account and the IFSC that closes it. The IFSC says which
# bank, and the digits before it are the account — so an account can be
# corroborated before a single statement from it has been imported.

_BANK_BY_IFSC = {
    "HDFC": "HDFC",
    "ICIC": "ICICI",
    "KKBK": "Kotak",
    "UTIB": "Axis",
    "SBIN": "SBI",
    "PUNB": "PNB",
    "IDIB": "Indian Bank",
    "IOBA": "Indian Overseas Bank",
    "CNRB": "Canara",
    "YESB": "Yes Bank",
    "INDB": "IndusInd",
    "FDRL": "Federal",
    "RATN": "RBL",
    "DEUT": "Deutsche",
}
_ACCT_WITH_IFSC_RE = re.compile(r"(\d{9,18})\s*-\s*([A-Z]{4}0[A-Z0-9]{6})\b", re.IGNORECASE)


def counterparty_accounts(note: str) -> list[dict]:
    """Every account a narration names in full, with the bank its IFSC says.

    Only a number sitting immediately before an IFSC counts — a transfer
    reference is a long digit run too, and mistaking one for an account
    would invent accounts he does not hold.
    """
    found = []
    for match in _ACCT_WITH_IFSC_RE.finditer(note or ""):
        ifsc = match.group(2).upper()
        found.append(
            {
                "number": match.group(1),
                "ifsc": ifsc,
                "bank": _BANK_BY_IFSC.get(ifsc[:4], ifsc[:4].title()),
            }
        )
    return found
