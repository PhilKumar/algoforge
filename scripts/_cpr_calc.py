"""Calculate 2H and 4H CPR levels for NIFTY on March 6, 2026"""

import json
import sys
from datetime import datetime, timedelta, timezone

import requests

sys.path.insert(0, "/home/ec2-user/algoforge")
import config

IST = timezone(timedelta(hours=5, minutes=30))
headers = {
    "access-token": config.DHAN_ACCESS_TOKEN,
    "client-id": config.DHAN_CLIENT_ID,
    "Content-Type": "application/json",
}

# Fetch 5-min candles for NIFTY Index (SID=13)
payload = {
    "securityId": "13",
    "exchangeSegment": "IDX_I",
    "instrument": "INDEX",
    "interval": "5",
    "fromDate": "2026-03-06",
    "toDate": "2026-03-06",
}
r = requests.post("https://api.dhan.co/v2/charts/intraday", headers=headers, json=payload, timeout=15)
data = r.json()

if "open" not in data:
    print("ERROR:", json.dumps(data)[:300])
    sys.exit(1)

opens = data["open"]
highs = data["high"]
lows = data["low"]
closes = data["close"]
timestamps = data["timestamp"]
n = len(opens)
print(f"Got {n} candles")

# Group candles into 2H and 4H buckets
# Market hours: 09:15 - 15:30
# 2H periods: 09:15-11:15, 11:15-13:15, 13:15-15:15, 15:15-15:30
# 4H periods: 09:15-13:15, 13:15-15:30 (only ~2 periods in a day)


def group_candles(candles_data, period_minutes):
    """Group 5-min candles into larger period OHLC"""
    periods = []
    current = None
    period_start = None

    for i in range(n):
        ts = datetime.fromtimestamp(timestamps[i], tz=IST)
        # Calculate which period this candle belongs to
        minutes_from_open = (ts.hour - 9) * 60 + ts.minute - 15
        period_idx = minutes_from_open // period_minutes

        if current is None or period_idx != current["idx"]:
            if current is not None:
                periods.append(current)
            current = {
                "idx": period_idx,
                "start": ts,
                "open": opens[i],
                "high": highs[i],
                "low": lows[i],
                "close": closes[i],
            }
        else:
            current["high"] = max(current["high"], highs[i])
            current["low"] = min(current["low"], lows[i])
            current["close"] = closes[i]

    if current is not None:
        periods.append(current)
    return periods


def calc_cpr(h, l, c):
    """Calculate Traditional pivot levels"""
    pp = round((h + l + c) / 3, 2)
    bc = round((h + l) / 2, 2)
    tc = round(2 * pp - bc, 2)
    r1 = round(2 * pp - l, 2)
    s1 = round(2 * pp - h, 2)
    r2 = round(pp + (h - l), 2)
    s2 = round(pp - (h - l), 2)
    r3 = round(h + 2 * (pp - l), 2)
    s3 = round(l - 2 * (h - pp), 2)
    r4 = round(r3 + (r1 - pp), 2)
    s4 = round(s3 - (pp - s1), 2)
    # Midpoints
    r0_5 = round((pp + r1) / 2, 2)
    s0_5 = round((pp + s1) / 2, 2)
    r1_5 = round((r1 + r2) / 2, 2)
    s1_5 = round((s1 + s2) / 2, 2)
    r2_5 = round((r2 + r3) / 2, 2)
    s2_5 = round((s2 + s3) / 2, 2)
    r3_5 = round((r3 + r4) / 2, 2)
    s3_5 = round((s3 + s4) / 2, 2)
    return {
        "PP": pp,
        "TC": tc,
        "BC": bc,
        "R1": r1,
        "R1.5": r1_5,
        "R2": r2,
        "R2.5": r2_5,
        "R3": r3,
        "R3.5": r3_5,
        "R4": r4,
        "S1": s1,
        "S1.5": s1_5,
        "S2": s2,
        "S2.5": s2_5,
        "S3": s3,
        "S3.5": s3_5,
        "S4": s4,
        "R0.5": r0_5,
        "S0.5": s0_5,
    }


# ── 2H CPR ──
print("\n=== 2H (120 min) Periods ===")
periods_2h = group_candles(data, 120)
for p in periods_2h:
    print(
        f"  Period {p['idx']}: {p['start'].strftime('%H:%M')} O={p['open']:.2f} H={p['high']:.2f} L={p['low']:.2f} C={p['close']:.2f}"
    )

# CPR for current 2H candle uses PREVIOUS completed 2H candle's OHLC
if len(periods_2h) >= 2:
    prev = periods_2h[-2]  # previous completed 2H period
    curr_start = periods_2h[-1]["start"].strftime("%H:%M")
    print(f"\n2H CPR (for period starting ~{curr_start}, using prev period OHLC):")
    print(f"  Previous 2H: H={prev['high']:.2f} L={prev['low']:.2f} C={prev['close']:.2f}")
    levels = calc_cpr(prev["high"], prev["low"], prev["close"])
    for k in [
        "R4",
        "R3.5",
        "R3",
        "R2.5",
        "R2",
        "R1.5",
        "R1",
        "R0.5",
        "TC",
        "PP",
        "BC",
        "S0.5",
        "S1",
        "S1.5",
        "S2",
        "S2.5",
        "S3",
        "S3.5",
        "S4",
    ]:
        print(f"  {k:5s}: {levels[k]:.2f}")

# Also calculate for the previous period (the one the user might be looking at)
if len(periods_2h) >= 3:
    prev2 = periods_2h[-3]
    curr2_start = periods_2h[-2]["start"].strftime("%H:%M")
    print(f"\n2H CPR (for period starting ~{curr2_start}, using prev period OHLC):")
    print(f"  Previous 2H: H={prev2['high']:.2f} L={prev2['low']:.2f} C={prev2['close']:.2f}")
    levels2 = calc_cpr(prev2["high"], prev2["low"], prev2["close"])
    for k in [
        "R4",
        "R3.5",
        "R3",
        "R2.5",
        "R2",
        "R1.5",
        "R1",
        "R0.5",
        "TC",
        "PP",
        "BC",
        "S0.5",
        "S1",
        "S1.5",
        "S2",
        "S2.5",
        "S3",
        "S3.5",
        "S4",
    ]:
        print(f"  {k:5s}: {levels2[k]:.2f}")

# ── 4H CPR ──
print("\n=== 4H (240 min) Periods ===")
periods_4h = group_candles(data, 240)
for p in periods_4h:
    print(
        f"  Period {p['idx']}: {p['start'].strftime('%H:%M')} O={p['open']:.2f} H={p['high']:.2f} L={p['low']:.2f} C={p['close']:.2f}"
    )

if len(periods_4h) >= 2:
    prev4 = periods_4h[-2]
    curr4_start = periods_4h[-1]["start"].strftime("%H:%M")
    print(f"\n4H CPR (for period starting ~{curr4_start}, using prev period OHLC):")
    print(f"  Previous 4H: H={prev4['high']:.2f} L={prev4['low']:.2f} C={prev4['close']:.2f}")
    levels4 = calc_cpr(prev4["high"], prev4["low"], prev4["close"])
    for k in [
        "R4",
        "R3.5",
        "R3",
        "R2.5",
        "R2",
        "R1.5",
        "R1",
        "R0.5",
        "TC",
        "PP",
        "BC",
        "S0.5",
        "S1",
        "S1.5",
        "S2",
        "S2.5",
        "S3",
        "S3.5",
        "S4",
    ]:
        print(f"  {k:5s}: {levels4[k]:.2f}")

# Also print all periods for both
print("\n=== All 2H period OHLC ===")
for p in periods_2h:
    print(
        f"  {p['start'].strftime('%H:%M')}-... O={p['open']:.2f} H={p['high']:.2f} L={p['low']:.2f} C={p['close']:.2f}"
    )
print("\n=== All 4H period OHLC ===")
for p in periods_4h:
    print(
        f"  {p['start'].strftime('%H:%M')}-... O={p['open']:.2f} H={p['high']:.2f} L={p['low']:.2f} C={p['close']:.2f}"
    )
