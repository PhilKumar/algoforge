"""Reading a plan the way it gets written down.

Nobody opens a date picker to note that the bank has to be called before
next Wednesday. They write "call the bank before next wednesday" and move
on. So the planner takes one line and pulls three things out of it: what
has to be done, when, and whether that when is a deadline, an appointment,
or a gate he must not start before.

  call the bank before next wednesday  →  by   Wed 3 Sep
  school fees before 15th of October   →  by   Wed 15 Oct
  renew the policy after this month    →  after Mon 31 Aug
  Oliver's results tomo                →  on   Fri 28 Aug

Three rules hold the reading honest.

The LONGEST phrase wins. "15th of October" must not be read as the 15th of
whatever month it is now because a bare number matched first, and "next
wednesday" must beat "wednesday".

The word in front of the date decides the kind, not the date itself. The
same Wednesday is a deadline in "before wednesday", an appointment in "on
wednesday", and a starting gate in "after wednesday".

A phrase this module cannot read is left alone rather than guessed at. The
whole line becomes the task and it sits under Someday, where he can say
when himself. A wrong date on a school fee is worse than no date.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

# on = it happens that day · by = it must be done before then · after = not
# before then. The page shows the word, so a misread is visible at a glance.
KINDS = ("on", "by", "after")

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_WEEKDAYS = {
    "mon": 0,
    "tue": 1,
    "tues": 1,
    "wed": 2,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}
_MONTH_RE = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
_DAY_RE = "mon|tues|tue|wednes|wed|thurs|thur|thu|fri|satur|sat|sun"
# "before" and its cousins turn a date into a deadline; "after" into a gate.
_KIND_WORDS = {
    "before": "by",
    "by": "by",
    "b4": "by",
    "till": "by",
    "until": "by",
    "due": "by",
    "latest": "by",
    "within": "by",
    "after": "after",
    "post": "after",
    "from": "after",
    "on": "on",
    "at": "on",
    "this": "on",
}


def _month_end(day: date) -> date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def _week_start(day: date) -> date:
    """Monday. A week that ends on Sunday is how the working week reads."""
    return day - timedelta(days=day.weekday())


def _add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    year = day.year + month // 12
    month = month % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _weekday_key(word: str) -> int:
    word = word[:4] if word[:4] in _WEEKDAYS else word[:3]
    return _WEEKDAYS[word]


def _coming(today: date, weekday: int) -> date:
    """The next one of that weekday, never today — 'friday' on a Friday
    means the one coming, not the one being lived through."""
    ahead = (weekday - today.weekday()) % 7
    return today + timedelta(days=ahead or 7)


def _next_week_day(today: date, weekday: int) -> date:
    """'next wednesday' is the Wednesday of NEXT week, not the nearest one.
    Said on a Monday it means eight days away, not two."""
    return _week_start(today) + timedelta(days=7 + weekday)


def _year_for(today: date, month: int, day_num: int, year: int | None) -> int:
    """A bare '15 October' means the next 15 October there is. In December
    that is next year, and a planner that puts it in the past is useless."""
    if year:
        return year + 2000 if year < 100 else year
    try:
        candidate = date(today.year, month, day_num)
    except ValueError:
        return today.year
    return today.year if candidate >= today else today.year + 1


def _safe(year: int, month: int, day_num: int) -> date | None:
    try:
        return date(year, month, day_num)
    except ValueError:
        return None


def _patterns() -> list[tuple[re.Pattern, str]]:
    return [
        (re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"), "iso"),
        (re.compile(r"\b(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?\b"), "dmy"),
        (re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_RE})[a-z]*\.?(?:\s+(\d{{4}}))?\b"), "dm"),
        (re.compile(rf"\b({_MONTH_RE})[a-z]*\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?\b"), "md"),
        (re.compile(r"\bin\s+(\d{1,3})\s+(day|week|month|year)s?\b"), "in_n"),
        (re.compile(r"\b(\d{1,3})\s+(day|week|month|year)s?\s+(?:later|from\s+now)\b"), "in_n_rev"),
        (re.compile(r"\bnext\s+week\s?end\b"), "next_weekend"),
        (re.compile(r"\bnext\s+(week|month|year)\b"), "next_unit"),
        (re.compile(r"\bthis\s+week\s?end\b"), "this_weekend"),
        (re.compile(r"\bthis\s+(week|month|year)\b"), "this_unit"),
        (re.compile(r"\b(?:the\s+)?end\s+of\s+(?:the\s+|this\s+)?(week|month|year)\b"), "end_of"),
        (re.compile(r"\b(?:month\s*end|eom)\b"), "eom"),
        (re.compile(r"\b(?:year\s*end|eoy)\b"), "eoy"),
        (re.compile(rf"\bnext\s+({_DAY_RE})[a-z]*\b"), "next_day"),
        (re.compile(rf"\b(?:this\s+)?(?:coming\s+)?({_DAY_RE})day\b"), "coming_day"),
        (re.compile(rf"\b({_DAY_RE})\b"), "coming_day_short"),
        # "before the 15th" — the 15th of this month, or next month's if
        # that day is already behind him.
        (re.compile(r"\b(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\b"), "ordinal"),
        # A bare month name. "may" is left out on purpose: it is a word he
        # will write far more often than a month.
        (re.compile(r"\b(jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b"), "bare_month"),
        (re.compile(r"\bday\s+after\s+tomorrow\b"), "day_after"),
        (re.compile(r"\bday\s+after\b"), "day_after"),
        (re.compile(r"\b(?:tomorrow|tomo+row|tomorow|tomo|tmrw|tmr)\b"), "tomorrow"),
        (re.compile(r"\b(?:today|tonight|tonite|now)\b"), "today"),
        (re.compile(r"\bweek\s?end\b"), "this_weekend"),
        (re.compile(r"\b(?:someday|sometime|one\s+day|no\s+rush)\b"), "someday"),
    ]


def _resolve(rule: str, match: re.Match, today: date) -> date | None:
    if rule == "iso":
        return _safe(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    if rule == "dmy":
        day_num, month = int(match.group(1)), int(match.group(2))
        if not 1 <= month <= 12:
            return None
        return _safe(_year_for(today, month, day_num, int(match.group(3)) if match.group(3) else None), month, day_num)
    if rule in ("dm", "md"):
        day_num = int(match.group(1) if rule == "dm" else match.group(2))
        month = _MONTHS[(match.group(2) if rule == "dm" else match.group(1))[:3]]
        return _safe(_year_for(today, month, day_num, int(match.group(3)) if match.group(3) else None), month, day_num)
    if rule in ("in_n", "in_n_rev"):
        count = int(match.group(1) if rule == "in_n" else match.group(1))
        unit = match.group(2)
        if unit == "day":
            return today + timedelta(days=count)
        if unit == "week":
            return today + timedelta(weeks=count)
        if unit == "month":
            return _add_months(today, count)
        return _add_months(today, count * 12)
    if rule == "next_weekend":
        return _week_start(today) + timedelta(days=12)
    if rule == "this_weekend":
        return _coming(today, 5)
    if rule == "next_unit":
        unit = match.group(1)
        if unit == "week":
            return _week_start(today) + timedelta(days=7)
        if unit == "month":
            return _add_months(today.replace(day=1), 1)
        return date(today.year + 1, 1, 1)
    if rule == "this_unit":
        unit = match.group(1)
        if unit == "week":
            return _week_start(today) + timedelta(days=6)
        if unit == "month":
            return _month_end(today)
        return date(today.year, 12, 31)
    if rule == "end_of":
        unit = match.group(1)
        if unit == "week":
            return _week_start(today) + timedelta(days=6)
        if unit == "month":
            return _month_end(today)
        return date(today.year, 12, 31)
    if rule == "eom":
        return _month_end(today)
    if rule == "eoy":
        return date(today.year, 12, 31)
    if rule == "next_day":
        return _next_week_day(today, _weekday_key(match.group(1)))
    if rule in ("coming_day", "coming_day_short"):
        return _coming(today, _weekday_key(match.group(1)))
    if rule == "ordinal":
        day_num = int(match.group(1))
        here = _safe(today.year, today.month, day_num)
        if here and here >= today:
            return here
        nxt = _add_months(today.replace(day=1), 1)
        return _safe(nxt.year, nxt.month, day_num)
    if rule == "bare_month":
        month = _MONTHS[match.group(1)[:3]]
        year = today.year if month >= today.month else today.year + 1
        return date(year, month, 1)
    if rule == "day_after":
        return today + timedelta(days=2)
    if rule == "tomorrow":
        return today + timedelta(days=1)
    if rule == "today":
        return today
    return None


# Only the words a date phrase leaves dangling. "this", "it" and "is" are
# deliberately absent: "clear this" and "do it" are whole tasks, and
# stripping their object leaves a line that means nothing.
_TRAILING = re.compile(
    r"[\s,;:.\-–—]*\b(?:before|by|b4|after|on|at|due|till|until|from|post|latest|within|in|the|of)\b[\s,;:.\-–—]*$",
    re.IGNORECASE,
)
_TIDY = re.compile(r"\s{2,}")


def _tidy_title(text: str) -> str:
    title = _TIDY.sub(" ", text).strip(" ,;:.-–—\t")
    # Strip the preposition the date phrase left dangling — "call the bank
    # before" reads as unfinished; "call the bank" is the task.
    while True:
        trimmed = _TRAILING.sub("", title).strip(" ,;:.-–—\t")
        if trimmed == title:
            return title
        title = trimmed


def read_plan(text: str, today: date | None = None) -> dict:
    """One written line in, a task and its date out.

    Returns `{title, due, kind, said}` — `due` an ISO date or "", `kind` one
    of on/by/after, and `said` the words that were read as the date, so the
    page can show its working and he can see when it has misread him.
    """
    today = today or date.today()
    raw = (text or "").strip()
    if not raw:
        return {"title": "", "due": "", "kind": "on", "said": ""}

    lowered = raw.lower()
    best: tuple[int, int, int, str, re.Match] | None = None
    for pattern, rule in _patterns():
        for match in pattern.finditer(lowered):
            # A bare month is only a month when something points at it.
            # "march the kids to school" is a verb, and reading it as March
            # beat the "tomo" at the end of the same line.
            if rule == "bare_month":
                lead = re.search(r"([a-z0-9]+)\W*$", lowered[: match.start()])
                if not lead or lead.group(1) not in _KIND_WORDS:
                    continue
            span = match.end() - match.start()
            # Longest phrase first, then the latest one written: "pay the
            # 15th instalment before 3 Oct" ends with the date that matters.
            key = (span, match.start())
            if best is None or key > (best[0], best[1]):
                best = (span, match.start(), 0, rule, match)
    if best is None:
        return {"title": _tidy_title(raw), "due": "", "kind": "on", "said": ""}

    _, start, _, rule, match = best
    if rule == "someday":
        return {
            "title": _tidy_title(raw[: match.start()] + " " + raw[match.end() :]),
            "due": "",
            "kind": "on",
            "said": raw[match.start() : match.end()],
        }
    due = _resolve(rule, match, today)
    if due is None:
        return {"title": _tidy_title(raw), "due": "", "kind": "on", "said": ""}

    # The word immediately in front of the phrase says what the date means.
    kind = "on"
    head = raw[:start].rstrip()
    lead = re.search(r"([A-Za-z0-9]+)\W*$", head)
    said_from = start
    if lead and lead.group(1).lower() in _KIND_WORDS:
        word = lead.group(1).lower()
        if word != "this":  # "this friday" is an appointment, not a deadline
            kind = _KIND_WORDS[word]
        said_from = lead.start(1)
    title = _tidy_title(raw[:said_from] + " " + raw[match.end() :])
    return {
        "title": title,
        "due": due.isoformat(),
        "kind": kind if kind in KINDS else "on",
        "said": raw[said_from : match.end()].strip(),
    }


def horizon(due: str, today: date) -> str:
    """Which shelf of the planner a task belongs on.

    A planner sorted purely by date reads as a wall of dates. Grouped by how
    soon it is, it reads the way the week actually feels.
    """
    if not due:
        return "someday"
    when = date.fromisoformat(due)
    if when < today:
        return "overdue"
    if when == today:
        return "today"
    if when == today + timedelta(days=1):
        return "tomorrow"
    if when <= _week_start(today) + timedelta(days=6):
        return "this week"
    if when <= _week_start(today) + timedelta(days=13):
        return "next week"
    if when <= _month_end(today):
        return "this month"
    return "later"


HORIZONS = ("overdue", "today", "tomorrow", "this week", "next week", "this month", "later", "someday")
