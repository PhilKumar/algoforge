"""The numbers a working life leaves behind, read out of the papers.

A payslip is not only a month's pay. It carries the registrations a family
would have to quote to claim anything after him: the UAN that follows him
between employers, the provident fund account, the personnel number each
employer knew him by. His tax papers carry the PAN. None of it is written
anywhere in the sanctuary today, because nobody types their UAN into a
web page for fun — it only ever gets copied off a payslip when it is
already needed.

Two rules run through everything here.

A masked number is not a number. Later payslips print "UAN Number :
******1234", and storing that as the UAN would be worse than storing
nothing — it would look answered. Anything carrying an asterisk or a run
of X's is refused.

A PAN on a page is not necessarily HIS PAN. His Form 12BB names the
lender's PAN two lines below his own, and his home-loan declarations carry
the housing company's. A PAN is only taken when the line says it belongs
to the employee, and never when the line names a lender, a deductor or a
company.
"""

from __future__ import annotations

import re

# Every kind this module can read, in the order Important info shows them.
KINDS: tuple[tuple[str, str], ...] = (
    ("uan", "UAN"),
    ("pan", "PAN"),
    ("pran", "PRAN (NPS)"),
    ("pf", "Provident fund"),
    ("esic", "ESIC"),
    ("employee_id", "Employee ID"),
    ("pernr", "Personnel number"),
)
LABELS = dict(KINDS)

# A value printed with its middle eaten away — "******1234", "XXXXXX1234".
_MASKED = re.compile(r"[*]|X{3,}", re.IGNORECASE)

_UAN = re.compile(r"UAN\s*(?:No\.?|Number)?\s*[:\-]?\s*([\dX*]{8,20})", re.IGNORECASE)
_PRAN = re.compile(r"PRAN\s*(?:No\.?|Number)?\s*[:\-]?\s*([\dX*]{10,20})", re.IGNORECASE)
# "PY/KRP/0012345/0001234" — the EPF office, establishment and member
_PF = re.compile(r"PF\s*No\.?\s*[:\-]?\s*([A-Z]{2}/[A-Z]{3}/\d{4,8}/\d{4,8})", re.IGNORECASE)
_ESIC = re.compile(r"ESIC?\s*(?:No\.?|Number)\s*[:\-]?\s*([\dX*]{10,20})", re.IGNORECASE)
_EMP_ID = re.compile(r"Employee\s*(?:ID|Code|No\.?)\s*[:\-]?\s*(\d{5,10})\b", re.IGNORECASE)
_PERNR = re.compile(r"PERNR\s*[:\-]?\s*(\d{6,10})\b", re.IGNORECASE)
_PAN_TOKEN = re.compile(r"\b([A-Z]{5}[0-9]{4}[A-Z])\b")

# A PAN sitting on one of these lines belongs to somebody else — the bank
# that lent him the money, the employer who deducted the tax, the landlord
# whose rent he claims.
_NOT_HIS = re.compile(
    r"lender|deductor|employer|landlord|company|bank|ltd\b|limited|corporation|" r"institution|trust|firm|society",
    re.IGNORECASE,
)
_HIS = re.compile(r"of\s+the\s+employee|employee'?s\s+PAN|^\s*PAN\b|self", re.IGNORECASE)


def _clean(value: str) -> str:
    return re.sub(r"[\s\-]", "", value or "").strip()


def _usable(value: str) -> bool:
    """A number is usable when it is whole. Masked is worse than absent."""
    return bool(value) and not _MASKED.search(value)


def _digits_only(value: str, low: int, high: int) -> str:
    clean = _clean(value)
    return clean if clean.isdigit() and low <= len(clean) <= high else ""


def find_identifiers(text: str) -> list[dict]:
    """Every registration number this page of text can be trusted for.

    Returns `{kind, label, number}` per find, deduplicated within the one
    document. A document that names a number twice is one vote, not two.
    """
    found: dict[str, str] = {}
    body = text or ""

    def keep(kind: str, value: str) -> None:
        if value and kind not in found:
            found[kind] = value

    for match in _UAN.finditer(body):
        raw = match.group(1)
        if _usable(raw):
            keep("uan", _digits_only(raw, 12, 12))
    for match in _PRAN.finditer(body):
        raw = match.group(1)
        if _usable(raw):
            keep("pran", _digits_only(raw, 12, 12))
    for match in _ESIC.finditer(body):
        raw = match.group(1)
        if _usable(raw):
            keep("esic", _digits_only(raw, 10, 17))
    for match in _PF.finditer(body):
        # Some payslips print the shape rather than the number —
        # "XX/XXX/0000000/0000000". That is a template, not an account.
        raw = match.group(1).upper()
        if _usable(raw):
            keep("pf", raw)
    for match in _EMP_ID.finditer(body):
        keep("employee_id", match.group(1))
    for match in _PERNR.finditer(body):
        keep("pernr", match.group(1))

    # The PAN is judged a line at a time, because his own and his lender's
    # sit four lines apart on the same declaration.
    for line in body.splitlines():
        token = _PAN_TOKEN.search(line)
        if not token:
            continue
        if _NOT_HIS.search(line):
            continue
        if not _HIS.search(line.strip()):
            continue
        keep("pan", token.group(1))
        break

    return [{"kind": kind, "label": LABELS[kind], "number": number} for kind, number in found.items() if number]


def merge_findings(tally: dict[str, dict], found: list[dict], source: str) -> None:
    """Fold one document's findings into the running tally, in place.

    Each number keeps a count of the papers that agree on it and the name
    of the first one, so the page can say "sixteen papers say this" rather
    than asking him to trust a single read.
    """
    for item in found:
        key = f"{item['kind']}:{item['number']}"
        entry = tally.get(key)
        if entry:
            entry["papers"] += 1
        else:
            tally[key] = {
                "kind": item["kind"],
                "label": item["label"],
                "number": item["number"],
                "papers": 1,
                "source": source,
            }


def best_of(tally: dict[str, dict]) -> list[dict]:
    """One answer per kind — the number the most papers agree on.

    Two different UANs in one vault means one of them is somebody else's
    payslip or a misread; the page shows the winner and says how many
    papers stood behind it, and the loser is dropped rather than offered
    as a choice nobody can adjudicate.
    """
    by_kind: dict[str, dict] = {}
    for entry in tally.values():
        current = by_kind.get(entry["kind"])
        if current is None or entry["papers"] > current["papers"]:
            by_kind[entry["kind"]] = entry
    order = [kind for kind, _ in KINDS]
    return sorted(by_kind.values(), key=lambda e: order.index(e["kind"]) if e["kind"] in order else 99)


def runners_up(tally: dict[str, dict], floor: int = 2) -> list[dict]:
    """The candidates that lost their kind's vote but are not noise.

    Usually a loser is somebody else's number — an insurer's PAN off a
    policy, the shape a payslip prints instead of an account. Sometimes it
    is genuinely his: he has one personnel number from each employer, and
    only one of them can win. So the ones with real corroboration behind
    them are offered too, unticked, saying plainly how few papers agree.
    """
    winners = {id(entry) for entry in best_of(tally)}
    return sorted(
        (e for e in tally.values() if id(e) not in winners and e["papers"] >= floor),
        key=lambda e: -e["papers"],
    )


def is_complete(tally: dict[str, dict]) -> bool:
    """True once the numbers worth stopping for are each corroborated.

    Reading five hundred encrypted papers takes minutes. Once the UAN, the
    PAN and the provident fund account each have two papers behind them,
    another two hundred payslips will not say anything new.
    """
    counts: dict[str, int] = {}
    for entry in tally.values():
        counts[entry["kind"]] = max(counts.get(entry["kind"], 0), entry["papers"])
    return all(counts.get(kind, 0) >= 2 for kind in ("uan", "pan", "pf"))
