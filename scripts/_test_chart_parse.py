"""Quick test for chart viewer parsing functions."""

import calendar as _cal
import re as _re

_MONTH_MAP = {}
for _i in range(1, 13):
    _MONTH_MAP[_cal.month_abbr[_i].upper()] = _i
    _MONTH_MAP[_cal.month_name[_i].upper()] = _i


def _parse_month_folder(name):
    parts = _re.split(r"[_-]", name, maxsplit=1)
    if len(parts) < 2:
        return None
    num = _MONTH_MAP.get(parts[0].upper()) or _MONTH_MAP.get(parts[0].upper()[:3])
    if num is None:
        return None
    return num, _cal.month_abbr[num]


def _parse_day_folder(name):
    m = _re.match(r"^(\d{1,2})[_-](\d{1,2})[_-](\d{4})$", name)
    if m:
        dd, mm, yyyy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        label = "%02d %s" % (dd, _cal.month_abbr[mm])
        return "%04d-%02d-%02d" % (yyyy, mm, dd), label
    m = _re.match(r"^(\d{1,2})-([A-Za-z]+)-(\d{4})$", name)
    if m:
        dd = int(m.group(1))
        num = _MONTH_MAP.get(m.group(2).upper()) or _MONTH_MAP.get(m.group(2).upper()[:3])
        if num:
            label = "%02d %s" % (dd, _cal.month_abbr[num])
            return "%04d-%02d-%02d" % (int(m.group(3)), num, dd), label
    return name, name


print("=== Month Parsing ===")
for t in ["APR_2023", "Aug_2023", "JULY_2023", "DEC_2023", "Apr-2024", "July-2024", "Feb-2026"]:
    print(f"  {t:15s} -> {_parse_month_folder(t)}")

print("\n=== Day Parsing ===")
for t in ["03_04_2023", "01-08-2023", "01-Feb-2026", "Feb-12-15", "Feb-4-5-6", "Feb-19-23"]:
    print(f"  {t:15s} -> {_parse_day_folder(t)}")

print("\nAll OK!")
