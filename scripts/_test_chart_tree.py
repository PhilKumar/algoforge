"""Simulate the /api/charts/tree endpoint against actual Daily Charts folder."""

import calendar as _cal
import json
import os
import re as _re

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHARTS_DIR = os.path.join(_HERE, "Daily Charts")

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


tree = {}
for year in sorted(os.listdir(CHARTS_DIR)):
    year_path = os.path.join(CHARTS_DIR, year)
    if not os.path.isdir(year_path) or not year.isdigit():
        continue
    months_list = []
    for mfolder in os.listdir(year_path):
        month_path = os.path.join(year_path, mfolder)
        if not os.path.isdir(month_path):
            continue
        parsed = _parse_month_folder(mfolder)
        if parsed is None:
            print(f"  WARN: skipping unparseable month folder: {mfolder}")
            continue
        month_num, month_label = parsed
        days_list = []
        for dfolder in os.listdir(month_path):
            day_path = os.path.join(month_path, dfolder)
            if not os.path.isdir(day_path):
                continue
            has_img = any(f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")) for f in os.listdir(day_path))
            if not has_img:
                continue
            sort_key, day_label = _parse_day_folder(dfolder)
            days_list.append({"folder": dfolder, "label": day_label, "sort": sort_key})
        if not days_list:
            continue
        days_list.sort(key=lambda d: d["sort"])
        months_list.append({"folder": mfolder, "label": month_label, "num": month_num, "days": days_list})
    if not months_list:
        continue
    months_list.sort(key=lambda m: m["num"])
    tree[year] = months_list

# Summary
total_days = sum(sum(len(m["days"]) for m in months) for months in tree.values())
print(f"Years: {list(tree.keys())}")
print(f"Total day folders with images: {total_days}")
for y, months in sorted(tree.items()):
    for mo in months:
        print(f"  {y}/{mo['folder']} ({mo['label']}) -> {len(mo['days'])} days")
