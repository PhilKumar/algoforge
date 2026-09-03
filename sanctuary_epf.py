"""Reading the provident fund's own papers.

Two kinds of paper come out of the EPFO portal, and they answer different
questions:

* a **passbook** for one financial year of one member account — what went
  in, what interest was added, what was taken out, and what the account
  held at the end of the year;
* a **claim receipt** — an advance he has asked for, which is money on its
  way out of the fund and into the bank.

A working life leaves a trail of member accounts, one per employer, and
the old ones do not vanish when he moves: the EPF half is transferred and
goes to zero, while the *pension* half stays behind in the old account
until he claims it. So the fund is worth the sum of the accounts, and any
one account's passbook only ever tells part of it.

Every figure here is read from the paper. Nothing is estimated, and a year
that is still running is never given the closing balance printed on it —
that number is what the year WILL close at, interest and all, and quoting
it as today's balance would show him money he does not yet have.
"""

from __future__ import annotations

import re
from datetime import date

# 12,34,567 or 1234567 or 0 — the passbooks print lakh grouping.
_MONEY = r"-?[\d,]+"
_DATE = r"(\d{2})[-/](\d{2})[-/](\d{4})"

_MEMBER = re.compile(r"Member ID(?:/Name)?\s+([A-Z]{2}[A-Z0-9]{6,24})", re.I)
_ESTABLISHMENT = re.compile(r"Establishment ID/Name\s+\S+\s*/\s*(.+?)\s*$", re.M)
_UAN = re.compile(r"\bUAN\s+(\d{12})\b")
_FY = re.compile(r"Financial Year\s*-\s*(\d{4})\s*-\s*(\d{4})")
_OPENING = re.compile(rf"OB\s+Int\.\s+Updated\s+upto\s+{_DATE}\s+({_MONEY})\s+({_MONEY})\s+({_MONEY})", re.I)
_CONTRIB = re.compile(
    rf"Total Contributions for the year\s*\[\s*(\d{{4}})\s*\]\s+({_MONEY})\s+({_MONEY})\s+({_MONEY})", re.I
)
_TRANSFER = re.compile(
    rf"Total Transfer-Ins?(?:/VDRs)? for the year\s*\[\s*\d{{4}}\s*\]\s+({_MONEY})\s+({_MONEY})\s+({_MONEY})", re.I
)
_WITHDRAWN = re.compile(
    rf"Total Withdrawals for the year\s*\[\s*\d{{4}}\s*\]\s+({_MONEY})\s+({_MONEY})\s+({_MONEY})", re.I
)
_INTEREST = re.compile(rf"Int\.\s+Updated\s+upto\s+{_DATE}\s+({_MONEY})\s+({_MONEY})\s+({_MONEY})", re.I)
_CLOSING = re.compile(rf"Closing Balance as on\s+{_DATE}\s+({_MONEY})\s+({_MONEY})\s+({_MONEY})", re.I)

_CLAIM_ID = re.compile(r"Tracking ID\s*-?\s*(\d{6,24})")
_CLAIM_TYPE = re.compile(r"Claim Type\s+(.+?)\s*$", re.M)
_CLAIM_PARA = re.compile(r"Advance Para\s+(.+?)\s*$", re.M)
_CLAIM_ELIGIBLE = re.compile(rf"Eligible Claim Amount \(Rs\)\s+({_MONEY})")
_CLAIM_ASKED = re.compile(rf"Requested Claim Amount \(Rs\)\s+({_MONEY})")
_RECEIPT_DATE = re.compile(rf"Receipt Date\s*{_DATE}")
_JOINED = re.compile(rf"Date of Joining EPF\s+{_DATE}")
_EXIT = re.compile(rf"Date of Exit EPF\s+{_DATE}")

# The older combined form (19/10C/31) says the same things in other words:
# a purpose rather than a para, an amount rather than a requested amount,
# and its date in the line that says where it was submitted.
_OLD_PURPOSE = re.compile(r"Purpose of Advance\s+(.+?)\s*$", re.M)
_OLD_AMOUNT = re.compile(rf"Amount of Advance \(In Rs\)\s+({_MONEY})")
_OLD_SUBMITTED = re.compile(r"Submitted online at .*? on\s+(\d{4})-(\d{2})-(\d{2})")
_OLD_FORM = re.compile(r"Combined Claim Form|Claim Form 19", re.I)


def _rupees(raw: str | None) -> float:
    """A passbook figure as a number. Blank, a dash or nonsense reads zero —
    a paper that will not say is not a paper that says zero is owed, but
    every one of these fields is printed as 0 when it is nil."""
    if not raw:
        return 0.0
    cleaned = raw.replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _date(match: re.Match | None, at: int = 1) -> str:
    """The ISO form of a DD-MM-YYYY the paper printed, or ""."""
    if not match:
        return ""
    day, month, year = match.group(at), match.group(at + 1), match.group(at + 2)
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return ""


def mask(member_id: str) -> str:
    """A member account said in public. The number identifies him to the
    fund, so only its tail is ever shown or stored."""
    tail = (member_id or "").strip()[-5:]
    return f"··{tail}" if tail else ""


def read_passbook(text: str) -> dict | None:
    """One financial year of one member account, or None if this is not one.

    The three columns are always employee, employer, pension — in that
    order, in every row of the summary.
    """
    body = text or ""
    fy = _FY.search(body)
    member = _MEMBER.search(body)
    if not fy or not member:
        return None
    closing = _CLOSING.search(body)
    opening = _OPENING.search(body)
    contrib = _CONTRIB.search(body)
    transfer = _TRANSFER.search(body)
    withdrawn = _WITHDRAWN.search(body)
    interest = _INTEREST.search(body)
    establishment = _ESTABLISHMENT.search(body)
    uan = _UAN.search(body)

    def triple(match: re.Match | None, at: int = 1) -> dict:
        if not match:
            return {"employee": 0.0, "employer": 0.0, "pension": 0.0}
        return {
            "employee": _rupees(match.group(at)),
            "employer": _rupees(match.group(at + 1)),
            "pension": _rupees(match.group(at + 2)),
        }

    return {
        "kind": "passbook",
        "member_id": member.group(1),
        "member": mask(member.group(1)),
        "employer_name": establishment.group(1).strip() if establishment else "",
        "uan": uan.group(1) if uan else "",
        "year_from": int(fy.group(1)),
        "year_to": int(fy.group(2)),
        # A year's statement opens where the last one closed, on 31 March.
        "opened_on": _date(opening),
        "opening": triple(opening, 4),
        "contributions": triple(contrib, 2),
        "transfer_in": triple(transfer),
        "withdrawals": triple(withdrawn),
        "interest": triple(interest, 4),
        "closes_on": _date(closing),
        "closing": triple(closing, 4),
    }


def _read_old_claim(body: str) -> dict | None:
    """The combined form of the older portal — same event, other words."""
    amount = _OLD_AMOUNT.search(body)
    if not amount or not _OLD_FORM.search(body):
        return None
    member = _MEMBER.search(body)
    purpose = _OLD_PURPOSE.search(body)
    submitted = _OLD_SUBMITTED.search(body)
    asked_on = ""
    if submitted:
        try:
            asked_on = date(int(submitted.group(1)), int(submitted.group(2)), int(submitted.group(3))).isoformat()
        except ValueError:
            asked_on = ""
    return {
        "kind": "claim",
        "tracking_id": (_CLAIM_ID.search(body).group(1) if _CLAIM_ID.search(body) else ""),
        "member_id": member.group(1) if member else "",
        "member": mask(member.group(1)) if member else "",
        "claim_type": "PF Advance (FORM 31)",
        "reason": purpose.group(1).strip() if purpose else "",
        "eligible": 0.0,
        "requested": _rupees(amount.group(1)),
        "asked_on": asked_on,
        "joined_on": "",
        "left_on": "",
    }


def read_claim(text: str) -> dict | None:
    """A claim receipt — what he asked the fund for, and when."""
    body = text or ""
    tracking = _CLAIM_ID.search(body)
    asked = _CLAIM_ASKED.search(body)
    if not tracking or not asked:
        return _read_old_claim(body)
    member = _MEMBER.search(body)
    kind = _CLAIM_TYPE.search(body)
    para = _CLAIM_PARA.search(body)
    eligible = _CLAIM_ELIGIBLE.search(body)
    return {
        "kind": "claim",
        "tracking_id": tracking.group(1),
        "member_id": member.group(1) if member else "",
        "member": mask(member.group(1)) if member else "",
        "claim_type": kind.group(1).strip() if kind else "",
        "reason": para.group(1).strip() if para else "",
        "eligible": _rupees(eligible.group(1)) if eligible else 0.0,
        "requested": _rupees(asked.group(1)),
        "asked_on": _date(_RECEIPT_DATE.search(body)),
        "joined_on": _date(_JOINED.search(body)),
        "left_on": _date(_EXIT.search(body)),
    }


def read_epf(text: str) -> dict | None:
    """Whichever of the two this paper is."""
    return read_passbook(text) or read_claim(text)


def _fy_end(year_from: int) -> date:
    """A financial year runs to the 31st of March of the following year."""
    return date(year_from + 1, 3, 31)


def _balance_now(paper: dict, today: date) -> dict:
    """What the account holds today, by this paper.

    A finished year has its closing balance printed and that is the answer.
    A year still running prints the balance it WILL close at — interest for
    the whole year included, months that have not happened included — so
    that number is never used. What is certain is where the year opened
    plus what has actually gone in and out since.
    """
    if today > _fy_end(paper["year_from"]):
        return dict(paper["closing"])
    return {
        side: paper["opening"][side]
        + paper["contributions"][side]
        + paper["transfer_in"][side]
        - paper["withdrawals"][side]
        for side in ("employee", "employer", "pension")
    }


# A claim is answered by the bank, not by the fund's own paperwork. The
# passbook that would prove it arrives months later — a year later, if the
# fund is slow — so until then the page went on promising money that was
# already spent. The statement knows: the fund pays by NEFT and the
# narration says so.
_FUND_PAYS = ("epf", "provident fund", "provident-fund", "pf advance")
# Wide enough for the tax and the rounding a payout comes with, narrow
# enough that an unrelated credit of a different size is not mistaken for
# it. And a payout does not arrive years later: a claim still open after
# half a year is a claim that failed, not one still in the post.
_CLAIM_TOLERANCE = (0.80, 1.05)
_CLAIM_WINDOW_DAYS = 200


def looks_like_the_fund_paying(note: str) -> bool:
    lowered = (note or "").lower()
    return any(word in lowered for word in _FUND_PAYS)


def settle_claims(summary: dict, credits: list[dict], today: date) -> dict:
    """Mark as paid every claim the bank has since received.

    Each credit answers at most one claim, so two advances of the same size
    are not both settled by one payment.
    """
    claims = [dict(c) for c in summary.get("claims") or []]
    spare = [
        c for c in credits if c.get("source") == "statement-in" and looks_like_the_fund_paying(c.get("note") or "")
    ]
    used: set[int] = set()
    for claim in claims:
        if not claim.get("awaiting"):
            continue
        asked = str(claim.get("asked_on") or "")
        wanted = float(claim.get("requested") or 0)
        if not asked or wanted <= 0:
            continue
        for index, credit in enumerate(spare):
            if index in used:
                continue
            when = str(credit.get("entry_date") or "")
            if when < asked or (date.fromisoformat(when) - date.fromisoformat(asked)).days > _CLAIM_WINDOW_DAYS:
                continue
            paid = float(credit.get("amount") or 0)
            if not wanted * _CLAIM_TOLERANCE[0] <= paid <= wanted * _CLAIM_TOLERANCE[1]:
                continue
            used.add(index)
            claim["awaiting"] = False
            claim["paid_on"] = when
            claim["paid"] = round(paid, 2)
            break
    settled = dict(summary)
    settled["claims"] = claims
    settled["claimed_pending"] = round(sum(float(c.get("requested") or 0) for c in claims if c.get("awaiting")), 2)
    settled["claimed_paid"] = round(sum(float(c.get("paid") or 0) for c in claims if c.get("paid")), 2)
    return _less_what_it_has_since_paid(settled)


def _less_what_it_has_since_paid(summary: dict) -> dict:
    """Take a paid claim out of the fund it was paid from.

    A passbook is a photograph of a day. Four and a half lakh left the fund
    on the first of September against a passbook dated the thirty-first of
    March, so the passbook still counts money that is now sitting in his
    current account — and the page counted it twice, once as fund and once
    as bank.

    Only a payout dated AFTER the account's own statement is subtracted.
    One paid before it is already inside the passbook's figure, and taking
    it off again would spend it a second time in the other direction.
    """
    accounts = [dict(a) for a in summary.get("accounts") or []]
    paid_out = 0.0
    for account in accounts:
        as_of = str(account.get("as_of") or "")
        gone = round(
            sum(
                float(c.get("paid") or 0)
                for c in summary.get("claims") or []
                if str(c.get("member") or "") == str(account.get("member") or "")
                and str(c.get("paid_on") or "") > as_of
            ),
            2,
        )
        if gone <= 0:
            continue
        # A fund cannot pay out more than it holds. Where the papers and the
        # bank disagree the bank is the one that moved money, so the fund is
        # emptied rather than driven below zero.
        gone = min(gone, float(account.get("fund") or 0))
        account["fund"] = round(float(account.get("fund") or 0) - gone, 2)
        account["paid_out_since"] = gone
        paid_out += gone
    if paid_out <= 0:
        return summary
    lighter = dict(summary)
    lighter["accounts"] = accounts
    lighter["fund"] = round(float(summary.get("fund") or 0) - paid_out, 2)
    lighter["worth"] = round(lighter["fund"] + float(summary.get("pension") or 0), 2)
    lighter["paid_out_since"] = round(paid_out, 2)
    return lighter


def summarise(papers: list[dict], today: date) -> dict:
    """Every paper he has, read as one fund.

    One account per member id, each taking its newest passbook — an older
    year is history, not a second balance. Claims are listed but never
    subtracted: the fund still holds the money until it pays, and the day
    it pays the next passbook will say so.
    """
    latest: dict[str, dict] = {}
    for paper in papers:
        if paper.get("kind") != "passbook":
            continue
        member = paper.get("member_id") or ""
        held = latest.get(member)
        if held is None or paper["year_from"] > held["year_from"]:
            latest[member] = paper

    accounts = []
    for paper in sorted(latest.values(), key=lambda p: (-p["year_from"], p["member"])):
        now = _balance_now(paper, today)
        running = today <= _fy_end(paper["year_from"])
        accounts.append(
            {
                "member": paper["member"],
                "employer_name": paper["employer_name"],
                "year_from": paper["year_from"],
                "year_to": paper["year_to"],
                "as_of": paper["opened_on"] if running else paper["closes_on"],
                "still_running": running,
                "employee": round(now["employee"], 2),
                "employer": round(now["employer"], 2),
                "pension": round(now["pension"], 2),
                "fund": round(now["employee"] + now["employer"], 2),
                "paid_in_this_year": round(paper["contributions"]["employee"] + paper["contributions"]["employer"], 2),
                "taken_out_this_year": round(paper["withdrawals"]["employee"] + paper["withdrawals"]["employer"], 2),
                # An account whose fund half is empty has been moved on; only
                # its pension is still sitting there waiting to be claimed.
                "moved_on": round(now["employee"] + now["employer"], 2) == 0 and now["pension"] > 0,
            }
        )

    claims = sorted(
        (dict(p) for p in papers if p.get("kind") == "claim"),
        key=lambda c: c.get("asked_on") or "",
        reverse=True,
    )
    # A claim is money still on its way only if no statement has been issued
    # since he asked. The passbooks run to a date; a claim asked before that
    # date has already had its answer, and its withdrawal is in the figures
    # above. Otherwise a COVID advance from 2020 would sit on the page for
    # ever as three lakh rupees about to arrive.
    newest_statement = max((a["as_of"] for a in accounts if a["as_of"]), default="")
    for claim in claims:
        claim["member"] = claim.get("member") or ""
        claim.pop("member_id", None)
        claim.pop("tracking_id", None)
        asked = claim.get("asked_on") or ""
        claim["awaiting"] = bool(asked) and asked > newest_statement and asked <= today.isoformat()

    fund = round(sum(a["fund"] for a in accounts), 2)
    pension = round(sum(a["pension"] for a in accounts), 2)
    years = [a["as_of"] for a in accounts if a["as_of"]]
    return {
        "accounts": accounts,
        "fund": fund,
        "pension": pension,
        "worth": round(fund + pension, 2),
        "paid_in_this_year": round(sum(a["paid_in_this_year"] for a in accounts), 2),
        "as_of": max(years) if years else "",
        "claims": claims,
        "claimed_pending": round(sum(c["requested"] for c in claims if c.get("awaiting")), 2),
    }
