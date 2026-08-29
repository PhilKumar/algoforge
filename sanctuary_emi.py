"""Parse an uploaded EMI / amortization schedule into schedule rows.

Accepts CSV, XLSX and PDF exports as banks actually produce them:

- HDFC-style tables whose "Period" column is an installment NUMBER, not a
  date — dates are synthesized from the loan's first-EMI date.
- Kotak-style PDFs whose table rows collapse into one cell per line — the
  line is tokenized and the (EMI, principal, interest) triple is located
  by checking principal + interest ≈ EMI, so the guess validates itself.
- Password-protected PDFs (banks lock them with PAN/DOB) — the caller can
  pass the document password.

XLSX is read with the standard library (zip + XML) because openpyxl is not
a PhilForge dependency; PDF uses pdfplumber when it is importable.

Returns {rows, warnings, needs_first_due} — when needs_first_due is true
the sheet held only period numbers and the caller must re-submit with a
first_due date to anchor them.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import date, datetime, timedelta
from xml.etree import ElementTree as ET  # nosec B405 — guarded by _safe_xml_root

HEADER_SYNONYMS = {
    # Real date columns; PERIOD_NAMES below only stand in when none match.
    "due_date": [
        "due date",
        "emi date",
        "installment date",
        "instalment date",
        "payment date",
        "date",
        "month",
        "emi month",
    ],
    "amount": [
        "instalment amt",
        "installment amt",
        "emi amount",
        "emi amt",
        "emi",
        "installment amount",
        "instalment amount",
        "installment",
        "instalment",
        "total payment",
        "total amount",
        "payment",
        "amount",
        "amt",
        "total",
    ],
    "principal": [
        "principal component",
        "principal amount",
        "principal paid",
        "principal repaid",
        "principal (p)",
        "principal",
        "prin",
    ],
    "interest": [
        "interest component",
        "interest amount",
        "interest paid",
        "interest charged",
        "interest (i)",
        "interest",
        "int",
    ],
    "outstanding": [
        "outstanding principal",
        "principal outstanding",
        "closing balance",
        "closing principal",
        "outstanding balance",
        "outstanding",
        "balance",
        "os balance",
        "principal os",
    ],
}

DATE_FORMATS = [
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%d/%m/%y",
    "%d-%m-%y",
    "%d-%b-%Y",
    "%d-%b-%y",
    "%d %b %Y",
    "%d %B %Y",
    "%d-%B-%Y",
    "%Y-%m-%d",
    "%b-%Y",
    "%b %Y",
    "%B-%Y",
    "%B %Y",
    "%b-%y",
    "%m/%Y",
]

_EXCEL_EPOCH = date(1899, 12, 30)
_MONTH_DAYS = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def _add_months(anchor: date, months: int) -> date:
    month_index = anchor.month - 1 + months
    year = anchor.year + month_index // 12
    month = month_index % 12 + 1
    limit = 29 if (month == 2 and year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else _MONTH_DAYS[month - 1]
    return date(year, month, min(anchor.day, limit))


def _clean_amount(raw) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = re.sub(r"[₹()\s]|rs\.?|inr", "", text, flags=re.IGNORECASE)
    text = text.replace(",", "")
    if not re.fullmatch(r"-?\d+(\.\d+)?", text):
        return None
    value = float(text)
    return -value if negative else value


def _clean_date(raw, default_day: int | None = None) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, date):
        return raw
    if isinstance(raw, (int, float)):
        serial = float(raw)
        if 20000 <= serial <= 80000:  # Excel serial range ≈ 1954–2118
            return _EXCEL_EPOCH + timedelta(days=int(serial))
        return None
    text = str(raw).strip()
    if not text:
        return None
    serial = _clean_amount(text)
    if serial is not None and 20000 <= serial <= 80000 and "." not in text:
        return _EXCEL_EPOCH + timedelta(days=int(serial))
    for fmt in DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        if "%d" not in fmt:
            # Month-only rows ("Mar-2026"): pin to the loan's due day.
            parsed = parsed.replace(day=min(default_day or 5, 28))
        return parsed
    return None


def _period_number(raw) -> int | None:
    value = _clean_amount(raw)
    if value is not None and value == int(value) and 1 <= value <= 600:
        return int(value)
    return None


PERIOD_NAMES = ["period", "emi no", "sr no", "installment no", "instalment no"]

# Floors for the guessing reader alone — a headed sheet says what its columns
# mean and is trusted as it stands.
_INSTALMENT_FLOOR = 100.0
_FEWEST_INSTALMENTS = 3
_SERIAL_RE = re.compile(r"\b(no\.?|nos|number|sr|srl|sl|s\.no|#)\b")


def _is_serial_header(cell: str) -> bool:
    """True for a counter column — "Instalment No.", "Sr No", "S.No"."""
    return bool(_SERIAL_RE.search(cell))


def _header_score(cell: str, names: list[str]) -> int:
    """How well a header matches a field: exact beats contained, longer beats shorter."""
    best = 0
    for name in names:
        if cell == name:
            return 1000 + len(name)
        if name in cell:
            best = max(best, len(name))
    return best


def _match_headers(row: list) -> dict[str, int] | None:
    """Assign each field the column that matches it most specifically.

    Column by column would let "Instalment No." claim the amount, because it
    contains the word "instalment" — which is how an HDFC key-fact sheet was
    read as sixty rupees of one, two, three. Scoring every pairing and taking
    the strongest first gives "Instalment Amt" the money and leaves the
    counter to be the period.
    """
    cells = [str(c).strip().lower() if c is not None else "" for c in row]
    scored: dict[tuple[str, int], int] = {}
    for field, names in HEADER_SYNONYMS.items():
        for idx, cell in enumerate(cells):
            if not cell:
                continue
            if field == "amount" and _is_serial_header(cell):
                continue  # a counter is never the money
            score = _header_score(cell, names)
            if score:
                scored[(field, idx)] = score

    mapping: dict[str, int] = {}
    taken: set[int] = set()
    for (field, idx), _ in sorted(scored.items(), key=lambda kv: -kv[1]):
        if field in mapping or idx in taken:
            continue
        mapping[field] = idx
        taken.add(idx)

    if "due_date" not in mapping:
        for idx, cell in enumerate(cells):
            if idx in taken or not cell:
                continue
            if any(cell == name or name in cell for name in PERIOD_NAMES):
                mapping["due_date"] = idx
                break
    if "due_date" in mapping and ("amount" in mapping or ("principal" in mapping and "interest" in mapping)):
        return mapping
    return None


def _finish(parsed: list[dict], warnings: list[str], needs_first_due=False) -> dict:
    parsed.sort(key=lambda r: r["due_date"])
    seen, unique = set(), []
    for row in parsed:
        if row["due_date"] in seen:
            warnings.append(f"Duplicate row for {row['due_date']} dropped.")
            continue
        seen.add(row["due_date"])
        unique.append(row)
    return {"rows": unique, "warnings": warnings, "needs_first_due": needs_first_due}


def _parse_under_header(rows: list[list], mapping: dict[str, int], default_day, first_due: date | None) -> dict:
    """Read the rows below one header, using that header's column map."""
    parsed, warnings = [], []
    period_rows = []  # (period, amounts) when the date column counts 1, 2, 3…
    for row in rows:

        def get(field, row=row, mapping=mapping):
            return row[mapping[field]] if field in mapping and mapping[field] < len(row) else None

        principal = _clean_amount(get("principal"))
        interest = _clean_amount(get("interest"))
        amount = _clean_amount(get("amount"))
        if amount is None and principal is not None and interest is not None:
            amount = round(principal + interest, 2)
        if amount is None or amount <= 0:
            continue
        values = {
            "amount": round(amount, 2),
            "principal_part": principal,
            "interest_part": interest,
            "outstanding": _clean_amount(get("outstanding")),
        }
        due = _clean_date(get("due_date"), default_day)
        if due is not None:
            parsed.append({"due_date": due.isoformat(), **values})
            continue
        period = _period_number(get("due_date"))
        if period is not None:
            period_rows.append((period, values))

    if period_rows and not parsed:
        if first_due is None:
            warnings.append(
                f"The sheet numbers its {len(period_rows)} installments (1, 2, 3…) "
                "instead of dating them — set the first EMI date to anchor them."
            )
            result = _finish([], warnings, needs_first_due=True)
            result["_candidates"] = len(period_rows)
            return result
        base = min(p for p, _ in period_rows)
        for period, values in period_rows:
            parsed.append({"due_date": _add_months(first_due, period - base).isoformat(), **values})
    result = _finish(parsed, warnings)
    result["_candidates"] = len(result["rows"])
    return result


def _rows_to_schedule(rows: list[list], default_day, first_due: date | None) -> dict:
    """Find the schedule among everything else the document contains.

    A key-fact sheet is a dozen pages of tables, and taking the first row
    that merely looks like a header hands the parse to something like
    "Installment Frequency | Monthly" — which then reads the real schedule's
    installment numbers as rupees. So every plausible header is tried and the
    one that yields the most rows wins; a header also has to map at least
    three fields across three filled cells before it is considered at all.
    """
    full, sparse = [], []
    for index, row in enumerate(rows):
        mapping = _match_headers(row)
        if not mapping:
            continue
        filled = sum(1 for cell in row if cell not in (None, ""))
        # A header naming three or more of the columns is trusted over a bare
        # pair, because "Installment Frequency | Monthly" is shaped exactly
        # like a legitimate two-column "Due Date | EMI" and would otherwise
        # win on row count alone. Pairs are still read when nothing richer
        # exists, which is what a minimal two-column sheet is.
        (full if len(mapping) >= 3 and filled >= 3 else sparse).append((index, mapping))

    for tier in (full, sparse):
        best = None
        for index, mapping in tier:
            result = _parse_under_header(rows[index + 1 :], mapping, default_day, first_due)
            if best is None or result["_candidates"] > best["_candidates"]:
                best = result
        if best is not None:
            best.pop("_candidates", None)
            if best["rows"] or best["needs_first_due"]:
                return best
    return _parse_lines_fallback(rows, [])


def _parse_lines_fallback(rows: list[list], warnings: list[str]) -> dict:
    """Rows whose whole line landed in one cell: tokenize and self-validate.

    A schedule line looks like ``02 Jun 2022 Installment 17.0 6,576
    3,851.49 2,724.51 2,24,063.51``.  The leading date is found by trying
    1–3 token prefixes; among the remaining numeric tokens the (EMI,
    principal, interest) triple is the consecutive run with p + i ≈ EMI,
    and the outstanding balance is the value that follows it.
    """
    parsed = []
    for row in rows:
        cells = [c for c in row if c not in (None, "")]
        for cell in cells:
            tokens = str(cell).split()
            if len(tokens) < 2:
                continue
            due = None
            rest: list[str] = []
            # The date is usually first, but a line lifted from a paginated
            # table often starts with the installment number — so the window
            # walks the line instead of only trying its head.
            for start in range(min(len(tokens), 4)):
                for width in (3, 2, 1):
                    found = _clean_date(" ".join(tokens[start : start + width]))
                    if found is not None:
                        due, rest = found, tokens[start + width :]
                        break
                if due is not None:
                    break
            if due is None:
                continue
            numbers = [n for n in (_clean_amount(t) for t in rest) if n is not None and n > 0]
            if not numbers:
                continue
            entry = None
            for i in range(len(numbers) - 2):
                emi, principal, interest = numbers[i], numbers[i + 1], numbers[i + 2]
                if abs((principal + interest) - emi) <= max(1.0, emi * 0.005):
                    entry = {
                        "due_date": due.isoformat(),
                        "amount": round(emi, 2),
                        "principal_part": principal,
                        "interest_part": interest,
                        "outstanding": numbers[i + 3] if i + 3 < len(numbers) else None,
                    }
                    break
            if entry is None:
                entry = {
                    "due_date": due.isoformat(),
                    "amount": round(numbers[0], 2),
                    "principal_part": None,
                    "interest_part": None,
                    "outstanding": None,
                }
            parsed.append(entry)
            break
    kept, warnings = _only_a_schedule(parsed, warnings)
    if not kept:
        warnings.append(
            "No schedule rows recognised — expected columns like Due Date and EMI Amount (or Principal + Interest)."
        )
    return _finish(kept, warnings)


def _only_a_schedule(parsed: list[dict], warnings: list[str]) -> tuple[list[dict], list[str]]:
    """Refuse a statement of past movements posing as a schedule.

    A lender's account statement has a date on every line too, and the first
    number after it is a value date's day or a reference number. Read that
    way, a home loan statement became 34 instalments of ₹10 and ₹31, and a
    quarterly summary one instalment of ₹19 lakh — either of which, committed,
    would have replaced a real schedule and reset the EMI to whatever the
    nonsense averaged.

    A row that checked itself — principal + interest = EMI — is trusted as it
    stands. A bare guess has to earn it: no lender bills under a hundred
    rupees, one lonely row is a summary line, and a schedule bills once a
    month, while a statement prints every movement in that month.
    """
    checked = [row for row in parsed if row["principal_part"] is not None]
    guessed = [row for row in parsed if row["principal_part"] is None and row["amount"] >= _INSTALMENT_FLOOR]
    if guessed and len(guessed) < _FEWEST_INSTALMENTS:
        guessed = []
    if guessed and len(guessed) > len({row["due_date"][:7] for row in guessed}):
        warnings.append(
            "That reads like a statement of what has been paid, not a schedule of what is due — "
            "more than one line a month. Upload the lender's own repayment schedule instead."
        )
        guessed = []
    return checked + guessed, warnings


def _parse_csv(blob: bytes, default_day, first_due):
    text = blob.decode("utf-8-sig", errors="replace")
    return _rows_to_schedule(list(csv.reader(io.StringIO(text))), default_day, first_due)


def _xlsx_cell_value(cell, shared: list[str]):
    kind = cell.get("t")
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    if kind == "inlineStr":
        node = cell.find(f"{ns}is")
        return "".join(t.text or "" for t in node.iter(f"{ns}t")) if node is not None else None
    value = cell.find(f"{ns}v")
    if value is None or value.text is None:
        return None
    if kind == "s":
        index = int(value.text)
        return shared[index] if index < len(shared) else None
    if kind == "str":
        return value.text
    try:
        number = float(value.text)
        return int(number) if number.is_integer() else number
    except ValueError:
        return value.text


def _safe_xml_root(data: bytes):
    """Parse workbook XML after refusing DTDs and entity declarations.

    Python's ElementTree never resolves external entities, and with the DTD
    check below an entity-expansion (billion-laughs) payload cannot reach the
    parser either, so the stdlib is safe here for these single-owner uploads.
    """
    head = data[:4096].lstrip()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
        raise zipfile.BadZipFile("workbook XML carries a DTD")
    return ET.fromstring(data)  # nosec B314


def _parse_xlsx(blob: bytes, default_day, first_due):
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = _safe_xml_root(archive.read("xl/sharedStrings.xml"))
            for si in root.iter(f"{ns}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{ns}t")))
        sheet_names = sorted(n for n in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
        if not sheet_names:
            return {"rows": [], "warnings": ["Workbook has no worksheets."], "needs_first_due": False}
        best = None
        for name in sheet_names:  # scan every sheet; schedules hide behind cover sheets
            rows: list[list] = []
            root = _safe_xml_root(archive.read(name))
            for row_el in root.iter(f"{ns}row"):
                row: list = []
                for cell in row_el.iter(f"{ns}c"):
                    ref = cell.get("r") or ""
                    letters = re.match(r"[A-Z]+", ref)
                    idx = 0
                    if letters:
                        for ch in letters.group(0):
                            idx = idx * 26 + (ord(ch) - 64)
                        idx -= 1
                    while len(row) < idx:
                        row.append(None)
                    row.append(_xlsx_cell_value(cell, shared))
                rows.append(row)
            result = _rows_to_schedule(rows, default_day, first_due)
            if result["rows"]:
                return result
            if best is None or result["needs_first_due"]:
                best = result
        return best


def _parse_pdf(blob: bytes, default_day, first_due, password):
    try:
        import pdfplumber
    except ImportError:
        return {
            "rows": [],
            "needs_first_due": False,
            "warnings": ["PDF support needs pdfplumber on this server — upload CSV or XLSX instead."],
        }
    rows: list[list] = []
    lines: list[list] = []
    try:
        with pdfplumber.open(io.BytesIO(blob), password=password or "") as pdf:
            for page in pdf.pages:
                for table in page.extract_tables() or []:
                    rows.extend(table)
                lines.extend([line] for line in (page.extract_text() or "").splitlines())
    except Exception as exc:
        chain = []
        node: BaseException | None = exc
        while node is not None:
            chain.append(f"{type(node).__name__}: {node}")
            node = node.__cause__ or node.__context__
        if any("password" in part.lower() for part in chain):
            return {
                "rows": [],
                "needs_first_due": False,
                "warnings": [
                    "This PDF is password-protected — enter the document password "
                    "(banks usually use your PAN or date of birth) and retry."
                ],
            }
        return {
            "rows": [],
            "needs_first_due": False,
            "warnings": ["Could not read the PDF — export the schedule as CSV or XLSX and retry."],
        }
    if not rows and not lines:
        return {
            "rows": [],
            "needs_first_due": False,
            "warnings": ["No table found in the PDF — if it is a scanned image, upload CSV or XLSX instead."],
        }
    result = (
        _rows_to_schedule(rows, default_day, first_due)
        if rows
        else {"rows": [], "warnings": [], "needs_first_due": False}
    )
    # An installment that straddles a page break is missing from the table
    # the extractor rebuilds, but it is still there in the page's text.
    from_text = _parse_lines_fallback(lines, [])
    if result["rows"] and from_text["rows"]:
        seen = {row["due_date"] for row in result["rows"]}
        rescued = [row for row in from_text["rows"] if row["due_date"] not in seen]
        if rescued:
            return _finish(result["rows"] + rescued, result["warnings"])
    if result["rows"] or result["needs_first_due"]:
        return result
    return from_text


def parse_emi_document(
    filename: str,
    blob: bytes,
    default_day: int | None = None,
    first_due: date | None = None,
    password: str | None = None,
) -> dict:
    """Return {rows, warnings, needs_first_due} for an uploaded schedule."""
    name = (filename or "").lower()
    if name.endswith(".pdf") or blob[:5] == b"%PDF-":
        return _parse_pdf(blob, default_day, first_due, password)
    if name.endswith((".xlsx", ".xlsm")) or blob[:2] == b"PK":
        try:
            return _parse_xlsx(blob, default_day, first_due)
        except (zipfile.BadZipFile, ET.ParseError, KeyError):
            return {
                "rows": [],
                "needs_first_due": False,
                "warnings": ["Could not read the workbook — export the schedule as CSV and retry."],
            }
    return _parse_csv(blob, default_day, first_due)
