"""
Forensic Trade-by-Trade Analysis: AlgoForge vs Benchmark
=========================================================
Compares Strategy_PE_New_trades.csv (AlgoForge) against
11352217-PE_BUY_LIVE_BEST_5yrs.csv (Benchmark / Golden dataset).
"""

import pandas as pd

pd.set_option("display.max_columns", 30)
pd.set_option("display.width", 200)
pd.set_option("display.max_colwidth", 40)
pd.set_option("display.float_format", "{:.2f}".format)

# ── Phase 1: Load & Normalize ─────────────────────────────────────
print("=" * 80)
print("PHASE 1: DATA NORMALIZATION & MERGING")
print("=" * 80)

# Load AlgoForge
af = pd.read_csv("/Users/philipkumar/Documents/New_Algo/Strategy_PE_New_trades.csv")
af["entry_time"] = pd.to_datetime(af["entry_time"], format="%Y-%m-%d %H:%M")
af["exit_time"] = pd.to_datetime(af["exit_time"], format="%Y-%m-%d %H:%M")
af["trade_date"] = af["entry_time"].dt.date

# Load Benchmark
bm = pd.read_csv("/Users/philipkumar/Documents/New_Algo/11352217-PE_BUY_LIVE_BEST_5yrs.csv")
bm["Entry Time"] = pd.to_datetime(bm["Entry Time"], format="%d %b %Y %H:%M:%S")
bm["Exit Time"] = pd.to_datetime(bm["Exit Time"], format="%d %b %Y %H:%M:%S")
bm["trade_date"] = bm["Entry Time"].dt.date

print(f"\nAlgoForge: {len(af)} trades, {af['entry_time'].min().date()} → {af['entry_time'].max().date()}")
print(f"Benchmark: {len(bm)} trades, {bm['Entry Time'].min().date()} → {bm['Entry Time'].max().date()}")

# Find overlapping date range
overlap_start = max(af["entry_time"].min().date(), bm["Entry Time"].min().date())
overlap_end = min(af["entry_time"].max().date(), bm["Entry Time"].max().date())
print(f"\nOverlapping period: {overlap_start} → {overlap_end}")

af_overlap = af[af["trade_date"] >= overlap_start].copy()
bm_overlap = bm[(bm["trade_date"] >= overlap_start) & (bm["trade_date"] <= overlap_end)].copy()
print(f"AlgoForge trades in overlap: {len(af_overlap)}")
print(f"Benchmark trades in overlap: {len(bm_overlap)}")

# ── Phase 2: Discrepancy Detection ────────────────────────────────
print("\n" + "=" * 80)
print("PHASE 2: DISCREPANCY DETECTION (FORENSIC AUDIT)")
print("=" * 80)

# 2a) Trade Matching by Date
af_dates = set(af_overlap["trade_date"])
bm_dates = set(bm_overlap["trade_date"])

matched_dates = af_dates & bm_dates
af_only_dates = af_dates - bm_dates
bm_only_dates = bm_dates - af_dates

print("\n── Trade Matching Summary ──")
print(f"Dates with trades in BOTH:         {len(matched_dates)}")
print(f"Dates with AlgoForge-only trades:  {len(af_only_dates)} (ghost trades)")
print(f"Dates with Benchmark-only trades:  {len(bm_only_dates)} (missed trades)")

if af_only_dates:
    print("\n  Ghost trade dates (AlgoForge took trades, Benchmark didn't):")
    for d in sorted(af_only_dates)[:15]:
        t = af_overlap[af_overlap["trade_date"] == d].iloc[0]
        print(
            f"    {d}  entry={t['entry_time'].strftime('%H:%M')}  strike={t['strike']}  exit={t['exit_reason']}  pnl={t['pnl']:.0f}"
        )
    if len(af_only_dates) > 15:
        print(f"    ... and {len(af_only_dates) - 15} more")

if bm_only_dates:
    print("\n  Missed trade dates (Benchmark had trades, AlgoForge didn't):")
    for d in sorted(bm_only_dates)[:15]:
        t = bm_overlap[bm_overlap["trade_date"] == d].iloc[0]
        print(f"    {d}  entry={t['Entry Time'].strftime('%H:%M')}  pnl={t['Profit']:.0f}")
    if len(bm_only_dates) > 15:
        print(f"    ... and {len(bm_only_dates) - 15} more")

# 2b) Merge matched-date trades for detailed comparison
af_matched = af_overlap[af_overlap["trade_date"].isin(matched_dates)].copy()
bm_matched = bm_overlap[bm_overlap["trade_date"].isin(matched_dates)].copy()

# Group by date — take first trade per date (since strategy is 1 trade/day)
af_daily = af_matched.groupby("trade_date").first().reset_index()
bm_daily = bm_matched.groupby("trade_date").first().reset_index()

merged = pd.merge(af_daily, bm_daily, on="trade_date", suffixes=("_af", "_bm"))

# 2c) Price Slippage Analysis
print(f"\n── Price Slippage Analysis ({len(merged)} matched trades) ──")
merged["entry_price_diff"] = merged["entry_price"] - merged["Entry Price"]
merged["exit_price_diff"] = merged["exit_price"] - merged["Exit Price"]
merged["pnl_diff"] = merged["pnl"] - merged["Profit"]

print("\n  Entry Price Difference (AF - BM):")
print(f"    Mean:   {merged['entry_price_diff'].mean():+.2f}")
print(f"    Median: {merged['entry_price_diff'].median():+.2f}")
print(f"    Std:    {merged['entry_price_diff'].std():.2f}")
print(f"    Min:    {merged['entry_price_diff'].min():+.2f}")
print(f"    Max:    {merged['entry_price_diff'].max():+.2f}")

print("\n  Exit Price Difference (AF - BM):")
print(f"    Mean:   {merged['exit_price_diff'].mean():+.2f}")
print(f"    Median: {merged['exit_price_diff'].median():+.2f}")
print(f"    Std:    {merged['exit_price_diff'].std():.2f}")

print("\n  P&L Difference (AF - BM):")
print(f"    Mean:   {merged['pnl_diff'].mean():+.2f}")
print(f"    Median: {merged['pnl_diff'].median():+.2f}")
print(f"    Total:  {merged['pnl_diff'].sum():+.2f}")
print(f"    AF total P&L:  {merged['pnl'].sum():+.2f}")
print(f"    BM total P&L:  {merged['Profit'].sum():+.2f}")

# 2d) Timing Mismatches
print("\n── Timing Mismatches ──")
merged["entry_time_af"] = pd.to_datetime(merged["entry_time"])
merged["entry_time_bm"] = pd.to_datetime(merged["Entry Time"])
merged["exit_time_af"] = pd.to_datetime(merged["exit_time"])
merged["exit_time_bm"] = pd.to_datetime(merged["Exit Time"])

merged["entry_delta_min"] = (merged["entry_time_af"] - merged["entry_time_bm"]).dt.total_seconds() / 60
merged["exit_delta_min"] = (merged["exit_time_af"] - merged["exit_time_bm"]).dt.total_seconds() / 60

print("\n  Entry Time Delta (AF - BM) in minutes:")
print(f"    Mean:  {merged['entry_delta_min'].mean():+.1f} min")
print(f"    Median: {merged['entry_delta_min'].median():+.1f} min")
print(f"    Exact match (0 min): {(merged['entry_delta_min'] == 0).sum()} / {len(merged)}")
print(f"    Within ±5 min:       {(merged['entry_delta_min'].abs() <= 5).sum()} / {len(merged)}")
print(f"    > 5 min off:         {(merged['entry_delta_min'].abs() > 5).sum()} / {len(merged)}")

dist = merged["entry_delta_min"].value_counts().sort_index()
print("\n  Entry Time Delta Distribution:")
for delta, count in dist.items():
    print(f"    {delta:+6.0f} min: {count} trades")

print("\n  Exit Time Delta (AF - BM) in minutes:")
print(f"    Mean:  {merged['exit_delta_min'].mean():+.1f} min")
print(f"    Median: {merged['exit_delta_min'].median():+.1f} min")
print(f"    Exact match (0 min): {(merged['exit_delta_min'] == 0).sum()} / {len(merged)}")

exit_dist = merged["exit_delta_min"].value_counts().sort_index()
print("\n  Exit Time Delta Distribution (top 15):")
for delta, count in exit_dist.head(15).items():
    print(f"    {delta:+6.0f} min: {count} trades")

# ── Phase 3: Root Cause Diagnosis ─────────────────────────────────
print("\n" + "=" * 80)
print("PHASE 3: ROOT CAUSE DIAGNOSIS")
print("=" * 80)

# 3a) Entry Price Pattern — AlgoForge uses fixed 250 premium?
print("\n── Entry Price Analysis ──")
print(f"  AlgoForge entry prices (unique): {sorted(af_overlap['entry_price'].unique())}")
print(
    f"  Benchmark entry prices (range):  {bm_overlap['Entry Price'].min():.2f} → {bm_overlap['Entry Price'].max():.2f}"
)
print(f"  Benchmark entry price mean:      {bm_overlap['Entry Price'].mean():.2f}")
print(f"  Benchmark entry price std:       {bm_overlap['Entry Price'].std():.2f}")

af_fixed_entry = af_overlap["entry_price"].nunique() <= 3
if af_fixed_entry:
    print(f"\n  ⚠ FINDING: AlgoForge uses FIXED entry price(s): {af_overlap['entry_price'].unique()}")
    print("  This indicates 'Premium Near' strike selection targeting a fixed premium level.")
    print("  The benchmark uses actual market entry prices that vary significantly.")

# 3b) Exit Price Pattern
print("\n── Exit Price Analysis ──")
print(f"  AlgoForge exit prices (unique): {sorted(af_overlap['exit_price'].unique())[:20]}")
af_exit_counts = af_overlap["exit_price"].value_counts().head(5)
print("  AlgoForge most common exit prices:")
for price, count in af_exit_counts.items():
    print(f"    {price:.2f}: {count} times ({count / len(af_overlap) * 100:.1f}%)")

bm_exit_counts = bm_overlap["Exit Price"].value_counts().head(5)
print("  Benchmark most common exit prices:")
for price, count in bm_exit_counts.items():
    print(f"    {price:.2f}: {count} times ({count / len(bm_overlap) * 100:.1f}%)")

# 3c) Exit Reason Analysis
print("\n── Exit Reason Analysis ──")
af_exit_reasons = af_overlap["exit_reason"].value_counts()
print("  AlgoForge exit reasons:")
for reason, count in af_exit_reasons.items():
    print(f"    {reason}: {count} ({count / len(af_overlap) * 100:.1f}%)")

# 3d) Qty / lot size comparison
print("\n── Quantity & Lot Size ──")
print(f"  AlgoForge qty:  {af_overlap['qty'].unique()}")
print(f"  Benchmark qty:  {bm_overlap['Qty'].unique()}")

# 3e) Strike comparison on matched dates
print("\n── Strike Selection Comparison (matched dates) ──")
strike_mismatches = 0
strike_samples = []
for _, row in merged.head(30).iterrows():
    af_strike = row.get("strike", "")
    bm_instrument = row.get("Instrument", "")
    # Extract strike from benchmark instrument string (e.g., NIFTY07MAR2422500PE)
    # and from AlgoForge (e.g., NIFTY 22500 PE)
    af_strike_num = ""
    if isinstance(af_strike, str):
        parts = af_strike.split()
        for p in parts:
            if p.isdigit():
                af_strike_num = p
                break
    bm_strike_num = ""
    if isinstance(bm_instrument, str):
        # Extract digits before PE/CE at the end
        import re

        m = re.search(r"(\d{4,6})(PE|CE)", bm_instrument)
        if m:
            bm_strike_num = m.group(1)

    if af_strike_num and bm_strike_num and af_strike_num != bm_strike_num:
        strike_mismatches += 1
        strike_samples.append(
            {
                "date": row["trade_date"],
                "af_strike": af_strike_num,
                "bm_strike": bm_strike_num,
                "diff": int(af_strike_num) - int(bm_strike_num),
            }
        )

print(f"  Strike mismatches in first 30 matched trades: {strike_mismatches}/30")
if strike_samples:
    print("  Sample mismatches:")
    for s in strike_samples[:10]:
        print(f"    {s['date']}: AF={s['af_strike']} vs BM={s['bm_strike']} (diff={s['diff']:+d})")

# 3f) SL/TP analysis
print("\n── StopLoss/Target Price Mechanics ──")
# AlgoForge: Check if SL exits use a fixed exit price
sl_trades = af_overlap[af_overlap["exit_reason"] == "StopLoss"]
tp_trades = af_overlap[af_overlap["exit_reason"] == "StrategyTP"]
sig_trades = af_overlap[af_overlap["exit_reason"] == "Signal"]
sqoff_trades = af_overlap[af_overlap["exit_reason"] == "SquareOff"]

if len(sl_trades) > 0:
    print(f"  StopLoss exits: {len(sl_trades)} trades")
    print(f"    Exit prices: {sorted(sl_trades['exit_price'].unique())}")
    print(f"    Mean P&L: {sl_trades['pnl'].mean():.2f}")
    # Check if SL is calculated on entry premium
    sl_pct = (sl_trades["entry_price"] - sl_trades["exit_price"]) / sl_trades["entry_price"] * 100
    print(f"    SL as % of entry: {sl_pct.mean():.1f}% (range: {sl_pct.min():.1f}% to {sl_pct.max():.1f}%)")

if len(tp_trades) > 0:
    print(f"  Target exits: {len(tp_trades)} trades")
    print(f"    Exit prices: {sorted(tp_trades['exit_price'].unique())}")
    print(f"    Mean P&L: {tp_trades['pnl'].mean():.2f}")
    # Check if TP is calculated on entry premium
    tp_pct = (tp_trades["exit_price"] - tp_trades["entry_price"]) / tp_trades["entry_price"] * 100
    print(f"    TP as % of entry: {tp_pct.mean():.1f}% (range: {tp_pct.min():.1f}% to {tp_pct.max():.1f}%)")

if len(sig_trades) > 0:
    print(f"  Signal exits: {len(sig_trades)} trades")
    print(f"    Mean P&L: {sig_trades['pnl'].mean():.2f}")

if len(sqoff_trades) > 0:
    print(f"  SquareOff exits: {len(sqoff_trades)} trades")
    print(f"    Mean P&L: {sqoff_trades['pnl'].mean():.2f}")

# 3g) Intra-candle vs Candle-close execution
print("\n── Intra-Candle vs. Candle-Close Execution ──")
# Check if benchmark exit times have :44 seconds (= near candle close for 5min candles)
bm_exit_seconds = bm_overlap["Exit Time"].dt.second
bm_near_close = (bm_exit_seconds >= 40).sum()
bm_exact_min = (bm_exit_seconds == 0).sum()
print(
    f"  Benchmark exit times with seconds >= :40 (near candle close): {bm_near_close}/{len(bm_overlap)} ({bm_near_close / len(bm_overlap) * 100:.1f}%)"
)
print(
    f"  Benchmark exit times at exact minute (:00): {bm_exact_min}/{len(bm_overlap)} ({bm_exact_min / len(bm_overlap) * 100:.1f}%)"
)

# Check if AF exits are at exact 5-min boundaries
af_exit_minutes = af_overlap["exit_time"].dt.minute % 5
af_on_5min = (af_exit_minutes == 0).sum()
print(
    f"  AlgoForge exits at 5-min boundaries: {af_on_5min}/{len(af_overlap)} ({af_on_5min / len(af_overlap) * 100:.1f}%)"
)

# Check AF entry times
af_entry_minutes = af_overlap["entry_time"].dt.minute % 5
af_entry_on_5min = (af_entry_minutes == 0).sum()
print(
    f"  AlgoForge entries at 5-min boundaries: {af_entry_on_5min}/{len(af_overlap)} ({af_entry_on_5min / len(af_overlap) * 100:.1f}%)"
)

# Benchmark entry times
bm_entry_seconds = bm_overlap["Entry Time"].dt.second
bm_entry_exact = (bm_entry_seconds == 0).sum()
print(
    f"  Benchmark entries at exact minute (:00): {bm_entry_exact}/{len(bm_overlap)} ({bm_entry_exact / len(bm_overlap) * 100:.1f}%)"
)

# 3h) AlgoForge fee structure — does it subtract broker fees?
print("\n── Fee Structure ──")
if len(tp_trades) > 0:
    sample_tp = tp_trades.iloc[0]
    raw_pnl = (sample_tp["exit_price"] - sample_tp["entry_price"]) * sample_tp["qty"]
    actual_pnl = sample_tp["pnl"]
    implied_fee = raw_pnl - actual_pnl
    print(
        f"  Sample TP trade: entry={sample_tp['entry_price']}, exit={sample_tp['exit_price']}, qty={sample_tp['qty']}"
    )
    print(f"    Raw P&L (no fees):  {raw_pnl:.2f}")
    print(f"    Actual P&L:         {actual_pnl:.2f}")
    print(f"    Implied fee:        {implied_fee:.2f} ({implied_fee / raw_pnl * 100:.2f}% of gross)")

# 3i) Check duration patterns
print("\n── Trade Duration Analysis ──")
af_overlap_copy = af_overlap.copy()
af_overlap_copy["duration_min"] = (af_overlap["exit_time"] - af_overlap["entry_time"]).dt.total_seconds() / 60
bm_overlap_copy = bm_overlap.copy()
bm_overlap_copy["duration_min"] = (bm_overlap["Exit Time"] - bm_overlap["Entry Time"]).dt.total_seconds() / 60

print(f"  AlgoForge avg duration: {af_overlap_copy['duration_min'].mean():.1f} min")
print(f"  Benchmark avg duration: {bm_overlap_copy['duration_min'].mean():.1f} min")
print(f"  AlgoForge median duration: {af_overlap_copy['duration_min'].median():.1f} min")
print(f"  Benchmark median duration: {bm_overlap_copy['duration_min'].median():.1f} min")

# Duration for SL exits specifically
if len(sl_trades) > 0:
    sl_dur = (sl_trades["exit_time"] - sl_trades["entry_time"]).dt.total_seconds() / 60
    print(f"  AlgoForge SL exit avg duration: {sl_dur.mean():.1f} min")
    print(f"  AlgoForge SL exits in 5 min or less: {(sl_dur <= 5).sum()}/{len(sl_trades)}")

# ── Summary Table ─────────────────────────────────────────────────
print("\n" + "=" * 80)
print("MISMATCH SUMMARY TABLE")
print("=" * 80)

summary = {
    "Metric": [
        "Overlapping Period",
        "AlgoForge trades (overlap)",
        "Benchmark trades (overlap)",
        "Matched-date trades",
        "AlgoForge-only (ghost) dates",
        "Benchmark-only (missed) dates",
        "Entry price fixed?",
        "Exit price (SL) fixed?",
        "Exit price (TP) fixed?",
        "Mean entry time delta (min)",
        "Mean exit time delta (min)",
        "Strike mismatches (first 30)",
        "AlgoForge qty per trade",
        "Benchmark qty per trade",
        "AlgoForge total P&L (overlap)",
        "Benchmark total P&L (overlap)",
        "P&L gap",
    ],
    "Value": [
        f"{overlap_start} → {overlap_end}",
        str(len(af_overlap)),
        str(len(bm_overlap)),
        str(len(merged)),
        str(len(af_only_dates)),
        str(len(bm_only_dates)),
        f"Yes ({af_overlap['entry_price'].unique()})" if af_fixed_entry else "No",
        f"Yes ({sl_trades['exit_price'].unique()})"
        if len(sl_trades) > 0 and sl_trades["exit_price"].nunique() <= 3
        else "No",
        f"Yes ({tp_trades['exit_price'].unique()})"
        if len(tp_trades) > 0 and tp_trades["exit_price"].nunique() <= 3
        else "No",
        f"{merged['entry_delta_min'].mean():+.1f}",
        f"{merged['exit_delta_min'].mean():+.1f}",
        f"{strike_mismatches}/30",
        str(af_overlap["qty"].unique()),
        str(bm_overlap["Qty"].unique()),
        f"₹{af_overlap['pnl'].sum():,.0f}",
        f"₹{bm_overlap['Profit'].sum():,.0f}",
        f"₹{af_overlap['pnl'].sum() - bm_overlap['Profit'].sum():+,.0f}",
    ],
}
summary_df = pd.DataFrame(summary)
print(summary_df.to_string(index=False))

# ── Detailed mismatch samples ─────────────────────────────────────
print("\n── Sample Matched Trades (first 15) ──")
sample_cols = merged[
    [
        "trade_date",
        "entry_time_af",
        "entry_time_bm",
        "entry_price",
        "Entry Price",
        "exit_time_af",
        "exit_time_bm",
        "exit_price",
        "Exit Price",
        "pnl",
        "Profit",
        "pnl_diff",
        "strike",
        "Instrument",
    ]
].head(15)
sample_cols.columns = [
    "Date",
    "AF Entry",
    "BM Entry",
    "AF EntryPx",
    "BM EntryPx",
    "AF Exit",
    "BM Exit",
    "AF ExitPx",
    "BM ExitPx",
    "AF PnL",
    "BM PnL",
    "PnL Diff",
    "AF Strike",
    "BM Instrument",
]
print(sample_cols.to_string(index=False))

print("\n\nAnalysis complete.")
