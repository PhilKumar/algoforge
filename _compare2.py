import pandas as pd

new = pd.read_csv("Strategy_PE_New_copy_trades (1).csv")
bm = pd.read_csv("11352217-PE_BUY_LIVE_BEST_5yrs.csv")

print("=" * 70)
print("ANALYSIS: New Backtest (theta reverted) vs Benchmark")
print("=" * 70)

print(f"\nNew AlgoForge: {len(new)} trades,  Total P&L: {new['pnl'].sum():>12,.2f}")
print(f"Benchmark:     {len(bm)} trades,  Total P&L: {bm['Profit'].sum():>12,.2f}")

# Entry prices
print("\n--- Entry Prices ---")
print(
    f"  New:  min={new['entry_price'].min():.2f}  max={new['entry_price'].max():.2f}  mean={new['entry_price'].mean():.2f}  unique={new['entry_price'].nunique()}"
)
print(
    f"  BM:   min={bm['Entry Price'].min():.2f}  max={bm['Entry Price'].max():.2f}  mean={bm['Entry Price'].mean():.2f}"
)

# Exit reasons
print("\n--- Exit Reasons ---")
for reason in sorted(new["exit_reason"].unique()):
    sub = new[new["exit_reason"] == reason]
    print(f"  {reason:12s}: {len(sub):3d} trades  P&L={sub['pnl'].sum():>10,.0f}  avg={sub['pnl'].mean():>8,.0f}")

# SL/TP prices
sl = new[new["exit_reason"] == "StopLoss"]
tp = new[new["exit_reason"] == "StrategyTP"]
print("\n--- SL Exit Prices ---")
if len(sl):
    print(f"  min={sl['exit_price'].min():.2f}  max={sl['exit_price'].max():.2f}  mean={sl['exit_price'].mean():.2f}")
print("--- TP Exit Prices ---")
if len(tp):
    print(f"  min={tp['exit_price'].min():.2f}  max={tp['exit_price'].max():.2f}  mean={tp['exit_price'].mean():.2f}")

# Win rate / stats
wins = (new["pnl"] > 0).sum()
print("\n--- Stats ---")
print(f"  Win rate:     {wins}/{len(new)} ({wins/len(new)*100:.1f}%)")
print(f"  Avg P&L:      {new['pnl'].mean():,.2f}")
print(f"  Final cum:    {new['cumulative'].iloc[-1]:,.2f}")
print(f"  Max drawdown: {(new['cumulative'].cummax() - new['cumulative']).max():,.2f}")

# Overlap comparison with benchmark
new["trade_date"] = pd.to_datetime(new["entry_time"]).dt.date
bm["trade_date"] = pd.to_datetime(bm["Entry Time"], format="%d %b %Y %H:%M:%S").dt.date
overlap_start = max(new["trade_date"].min(), bm["trade_date"].min())
overlap_end = min(new["trade_date"].max(), bm["trade_date"].max())
af_ov = new[(new["trade_date"] >= overlap_start) & (new["trade_date"] <= overlap_end)]
bm_ov = bm[(bm["trade_date"] >= overlap_start) & (bm["trade_date"] <= overlap_end)]

print(f"\n--- Overlap: {overlap_start} to {overlap_end} ---")
print(f"  AF: {len(af_ov)} trades, P&L = {af_ov['pnl'].sum():>10,.2f}")
print(f"  BM: {len(bm_ov)} trades, P&L = {bm_ov['Profit'].sum():>10,.2f}")
print(f"  Gap:                       {af_ov['pnl'].sum() - bm_ov['Profit'].sum():>+10,.2f}")

# Matched date comparison
merged = pd.merge(
    af_ov.groupby("trade_date")
    .first()
    .reset_index()[["trade_date", "entry_price", "exit_price", "pnl", "exit_reason", "strike"]],
    bm_ov.groupby("trade_date")
    .first()
    .reset_index()[["trade_date", "Entry Price", "Exit Price", "Profit", "Instrument"]],
    on="trade_date",
)
print(f"  Matched-date trades: {len(merged)}")
print(
    f"  Entry price diff (AF-BM): mean={merged['entry_price'].sub(merged['Entry Price']).mean():+.2f}  std={merged['entry_price'].sub(merged['Entry Price']).std():.2f}"
)
print(
    f"  P&L diff (AF-BM):        mean={merged['pnl'].sub(merged['Profit']).mean():+.2f}  total={merged['pnl'].sum()-merged['Profit'].sum():+,.2f}"
)

# Dates only in AF (ghost) vs dates only in BM (missed)
af_dates = set(af_ov["trade_date"])
bm_dates = set(bm_ov["trade_date"])
print(f"\n  Ghost trades (AF-only dates): {len(af_dates - bm_dates)}")
print(f"  Missed trades (BM-only dates): {len(bm_dates - af_dates)}")

# Ghost trade P&L
ghost_pnl = af_ov[af_ov["trade_date"].isin(af_dates - bm_dates)]["pnl"].sum()
print(f"  Ghost trades total P&L: {ghost_pnl:+,.2f}")

# Sample matched trades
print("\n--- First 15 Matched Trades ---")
print(
    f"{'Date':>12s} {'AF_EP':>7s} {'BM_EP':>7s} {'AF_XP':>7s} {'BM_XP':>7s} {'AF_PnL':>9s} {'BM_PnL':>9s} {'Reason':>12s}"
)
for _, r in merged.head(15).iterrows():
    print(
        f"{str(r['trade_date']):>12s} {r['entry_price']:7.2f} {r['Entry Price']:7.2f} {r['exit_price']:7.2f} {r['Exit Price']:7.2f} {r['pnl']:9.0f} {r['Profit']:9.0f} {r['exit_reason']:>12s}"
    )

# Where AF exit reason differs from potential BM outcome
print("\n--- Biggest P&L disagreements (top 10) ---")
merged["pnl_diff"] = merged["pnl"] - merged["Profit"]
worst = merged.nsmallest(10, "pnl_diff")
for _, r in worst.iterrows():
    print(
        f"  {r['trade_date']}: AF={r['pnl']:+8.0f} BM={r['Profit']:+8.0f} diff={r['pnl_diff']:+8.0f}  AF_reason={r['exit_reason']}  AF_EP={r['entry_price']:.0f} BM_EP={r['Entry Price']:.0f}"
    )
