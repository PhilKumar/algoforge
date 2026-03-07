import json
import urllib.request
from collections import defaultdict

with urllib.request.urlopen("http://127.0.0.1:8000/api/broker/trades") as r:
    d = json.loads(r.read())

trades = d.get("trades", [])
today = "2026-03-02"
today_trades = [t for t in trades if (t.get("createTime", "") or "").startswith(today)]
print(f"Total trades: {len(trades)}")
print(f"Today ({today}): {len(today_trades)}")

# Group by securityId
pairs = defaultdict(list)
for t in today_trades:
    pairs[t.get("securityId", "")].append(t)
print(f"Unique instruments today: {len(pairs)}")

total_pnl = 0
for sid, tlist in sorted(pairs.items()):
    buys = [t for t in tlist if t.get("transactionType") == "BUY"]
    sells = [t for t in tlist if t.get("transactionType") == "SELL"]
    sym = tlist[0].get("tradingSymbol", sid)

    buy_qty = sum(int(t["tradedQuantity"]) for t in buys)
    sell_qty = sum(int(t["tradedQuantity"]) for t in sells)

    if buys and sells:
        buy_value = sum(float(t["tradedPrice"]) * int(t["tradedQuantity"]) for t in buys)
        sell_value = sum(float(t["tradedPrice"]) * int(t["tradedQuantity"]) for t in sells)
        matched_qty = min(buy_qty, sell_qty)
        buy_avg = buy_value / buy_qty
        sell_avg = sell_value / sell_qty
        pnl = (sell_avg - buy_avg) * matched_qty
        total_pnl += pnl
        print(f"  {sym}: BUY {buy_qty} @ {buy_avg:.2f}, SELL {sell_qty} @ {sell_avg:.2f} -> P&L: {pnl:.2f}")
    else:
        print(f"  {sym}: BUY={buy_qty} SELL={sell_qty} (unmatched)")

print(f"\nTotal Today P&L: {total_pnl:.2f}")
