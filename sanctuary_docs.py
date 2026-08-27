"""Naming a document from its filename, so a folder can be filed in one drop.

A lifetime of papers arrives with the names their sources gave them —
"Apr 2016.pdf", "Kyndryl/Sep2021.pdf", "Form16_FY_21_22.pdf". The vault
would rather hold them named, dated and grouped than as a heap, and the
owner would rather not type 154 titles. This reads what the filename and
its folder already say, and leaves anything it cannot read as Other for
the owner to correct — never a confident wrong guess.
"""

from __future__ import annotations

import re

CATEGORIES = ("Identity", "Work", "Vehicle", "Finance", "Family", "Other")

_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
# The year need not end the word: "Apr2021shiftAll" is April 2021 too, so
# the trailing boundary is dropped and the month must start a word.
_MONTH_RE = re.compile(
    r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\s*[-_ ]?\s*((?:19|20)\d{2})",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")

# First match wins. Each rule reads a word the filename or its folder
# carries; anything unmatched stays Other rather than guessing.
_RULES = (
    # Shift allowances live INSIDE the payslips folder, so they must be read
    # before the word "payslip" in their own path claims them.
    ("shift allowance", "Work", "Shift allowances"),
    ("shiftall", "Work", "Shift allowances"),
    ("payslip", "Work", "Payslips"),
    ("form 16", "Finance", "Form 16"),
    ("form16", "Finance", "Form 16"),
    ("12bb", "Finance", "Tax declarations"),
    ("provisionit", "Finance", "Tax declarations"),
    ("incomeorloss", "Finance", "Tax declarations"),
    ("houseproperty", "Finance", "Tax declarations"),
    ("nps", "Finance", "NPS"),
    ("pran", "Finance", "NPS"),
    ("home loan", "Finance", "Home loan"),
    ("homeloan", "Finance", "Home loan"),
    ("loan", "Finance", "Loan papers"),
    ("fees_receipt", "Family", "School fees"),
    ("fee receipt", "Family", "School fees"),
    ("child", "Family", ""),
    ("policy", "Finance", "Insurance"),
    ("insurance", "Finance", "Insurance"),
    ("medibuddy", "Finance", "Insurance"),
    ("mediassist", "Finance", "Insurance"),
    ("two wheeler", "Vehicle", "Insurance"),
    ("aadhaar", "Identity", ""),
    ("aadhar", "Identity", ""),
    ("pan card", "Identity", ""),
    ("passport", "Identity", ""),
    ("licence", "Identity", ""),
    ("license", "Identity", ""),
    ("award", "Work", "Recognition"),
    ("reference letter", "Work", "Letters"),
    ("intimation letter", "Work", "Letters"),
    ("letter", "Work", "Letters"),
    ("claimform", "Work", "Claims"),
    ("benefit", "Work", "Benefits"),
    ("bills", "Finance", "Bills"),
    ("receipt", "Finance", "Bills"),
    # ── read from his own folder names and the words his papers use ──
    ("term fees", "Family", "School fees"),
    ("fees", "Family", "School fees"),
    ("std_", "Family", "School fees"),
    ("itrv", "Finance", "Tax returns"),
    ("itr ", "Finance", "Tax returns"),
    ("it submission", "Finance", "Tax declarations"),
    ("it_proof", "Finance", "Tax declarations"),
    ("proof_submission", "Finance", "Tax declarations"),
    ("80d", "Finance", "Tax declarations"),
    ("sec-80", "Finance", "Tax declarations"),
    ("elss", "Finance", "Investments"),
    ("declaration", "Finance", "Tax declarations"),
    ("epf", "Finance", "EPF"),
    ("e-nomination", "Finance", "EPF"),
    ("enomination", "Finance", "EPF"),
    ("broadband", "Finance", "Utilities"),
    ("invoice", "Finance", "Utilities"),
    ("jio", "Finance", "Utilities"),
    ("appraisal", "Work", "Appraisals"),
    ("resume", "Work", "Career"),
    ("payment agreement", "Work", "Career"),
    ("sez", "Work", "Career"),
    ("mentee", "Work", "Career"),
    ("citrix", "Work", "Access & tools"),
    ("medical", "Family", "Health"),
    ("doctor", "Family", "Health"),
    ("hospital", "Family", "Health"),
    ("trip", "Family", "Travel"),
    ("paramakudi", "Family", "Travel"),
    ("suites", "Family", "Travel"),
    ("pdfdrive", "Other", "Reading"),
    ("learn", "Other", "Reading"),
    ("goals_", "Work", "Goals"),
    ("reflections", "Work", "Goals"),
    ("conversations", "Work", "Goals"),
)


def _pretty(stem: str) -> str:
    text = re.sub(r"[_]+", " ", stem)
    text = re.sub(r"\s+", " ", text).strip(" -")
    return text[:120] or "Document"


def classify_document(filename: str, folder: str = "") -> dict:
    """Read a document's own name for its title, kind, series and date."""
    name = (filename or "").rsplit("/", 1)[-1]
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", name)
    haystack = f"{folder} {stem}".lower().replace("-", " ")

    category, series = "Other", ""
    for needle, cat, grp in _RULES:
        if needle in haystack:
            category, series = cat, grp
            break

    doc_date = ""
    month_match = _MONTH_RE.search(stem) or _MONTH_RE.search(folder)
    if month_match:
        month = _MONTHS[month_match.group(1).lower()]
        doc_date = f"{int(month_match.group(2)):04d}-{month:02d}-01"
    else:
        year_match = _YEAR_RE.search(stem)
        if year_match:
            doc_date = f"{year_match.group(1)}-01-01"

    # A payslip folder names its payslips even when the file says only a
    # month, and an employer folder ("Kyndryl") belongs in the title.
    title = _pretty(stem)
    employer = ""
    for part in [p for p in folder.split("/") if p]:
        low = part.lower()
        if low in ("payslips", "shift allowances", "documents", "payslip"):
            continue
        if not _MONTH_RE.search(part) and not part.isdigit():
            employer = part[:40]
    if series == "Payslips" and employer:
        title = f"{employer} · {title}"
    return {"title": title, "category": category, "series": series, "doc_date": doc_date}
