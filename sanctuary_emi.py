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
        "emi amount",
        "emi",
        "installment amount",
        "instalment amount",
        "installment",
        "instalment",
        "total payment",
        "total amount",
        "payment",
        "amount",
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


def _match_headers(row: list) -> dict[str, int] | None:
    cells = [str(c).strip().lower() if c is not None else "" for c in row]
    mapping: dict[str, int] = {}
    for field, names in HEADER_SYNONYMS.items():
        for idx, cell in enumerate(cells):
            if idx in mapping.values() or not cell:
                continue
            if any(cell == name or name in cell for name in names):
                mapping[field] = idx
                break
    if "due_date" not in mapping:
        for idx, cell in enumerate(cells):
            if idx in mapping.values() or not cell:
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


def _rows_to_schedule(rows: list[list], default_day, first_due: date | None) -> dict:
    mapping = None
    parsed, warnings = [], []
    period_rows = []  # (period, amounts) when the date column is 1, 2, 3…
    for row in rows:
        if mapping is None:
            mapping = _match_headers(row)
            continue

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
    if mapping is None:
        return _parse_lines_fallback(rows, warnings)
    if period_rows and not parsed:
        if first_due is None:
            warnings.append(
                f"The sheet numbers its {len(period_rows)} installments (1, 2, 3…) "
                "instead of dating them — set the first EMI date to anchor them."
            )
            return _finish([], warnings, needs_first_due=True)
        base = min(p for p, _ in period_rows)
        for period, values in period_rows:
            parsed.append({"due_date": _add_months(first_due, period - base).isoformat(), **values})
    return _finish(parsed, warnings)


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
            for width in (3, 2, 1):
                due = _clean_date(" ".join(tokens[:width]))
                if due is not None:
                    rest = tokens[width:]
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
    if not parsed:
        warnings.append(
            "No schedule rows recognised — expected columns like Due Date and EMI Amount (or Principal + Interest)."
        )
    return _finish(parsed, warnings)


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
    try:
        with pdfplumber.open(io.BytesIO(blob), password=password or "") as pdf:
            for page in pdf.pages:
                for table in page.extract_tables() or []:
                    rows.extend(table)
            if not rows:
                for page in pdf.pages:
                    rows.extend([[line]] for line in (page.extract_text() or "").splitlines())
                rows = [r[0] for r in rows]
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
    if not rows:
        return {
            "rows": [],
            "needs_first_due": False,
            "warnings": ["No table found in the PDF — if it is a scanned image, upload CSV or XLSX instead."],
        }
    return _rows_to_schedule(rows, default_day, first_due)


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
