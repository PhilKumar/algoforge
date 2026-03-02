# AlgoForge — Complete Algo Trading Platform

## What's Inside
```
algoforge/
├── app.py               ← FastAPI backend (run this first)
├── strategy.html        ← Frontend UI (auto-served at localhost:8000)
├── config.py            ← Your Dhan API keys go here
├── requirements.txt     ← Python dependencies
├── engine/
│   ├── indicators.py    ← SuperTrend, EMA, CPR, RSI, Yesterday candle
│   ├── backtest.py      ← Full backtest engine with condition evaluator
│   └── live.py          ← Live trading loop with Dhan integration
└── broker/
    └── dhan.py          ← Complete Dhan API wrapper
```

---

## SETUP (One Time, ~5 minutes)

### Step 1 — Python
Make sure Python 3.10+ is installed:
```bash
python --version
```

### Step 2 — Install Dependencies
```bash
cd algoforge
pip install -r requirements.txt
```

### Step 3 — Add Your Dhan Credentials
Edit `config.py`:
```python
DHAN_CLIENT_ID    = "YOUR_CLIENT_ID_HERE"    # ← Replace this
DHAN_ACCESS_TOKEN = "YOUR_ACCESS_TOKEN_HERE" # ← Replace this
```

**How to get Dhan API credentials:**
1. Login to https://dhanhq.co
2. Go to Developer Console → Create App
3. Copy your Client ID and generate an Access Token

### Step 4 — Run the Backend
```bash
python app.py
```
You should see:
```
AlgoForge Backend Starting
Open browser: http://127.0.0.1:8000
```

### Step 5 — Open the UI
Go to: **http://localhost:8000**

---

## HOW TO USE

### Backtesting
1. Set your date range (default: 5 years)
2. Edit indicator parameters via ✏ Edit buttons
3. Click **Submit Backtest**
4. Results page shows equity curve, stats, monthly heatmap, all trades

### Live Trading
1. Go to **Live Trading** tab
2. Select Paper Testing (safe) or Auto Trading (real orders)
3. Click **▶ Start Live Engine**
4. Watch the log for signals and order confirmations
5. Click Stop to halt

---

## ARCHITECTURE

```
Browser (strategy.html)
    │
    │ HTTP POST /api/backtest
    │ HTTP POST /api/live/start
    │ WebSocket /ws (real-time updates)
    │
FastAPI (app.py :8000)
    │
    ├── engine/backtest.py  ← Processes historical OHLCV, returns trades
    ├── engine/indicators.py ← SuperTrend, EMA, CPR, RSI computed in pandas
    ├── engine/live.py       ← Async loop: polls Dhan, checks conditions, places orders
    └── broker/dhan.py      ← Dhan REST API calls (historical data + orders)
```

---

## WITHOUT DHAN CREDENTIALS
Backtesting still works — it falls back to **Yahoo Finance** (yfinance) for NIFTY data.
Live trading requires Dhan credentials.

---

## ADDING MORE CONDITIONS
Edit the `DEFAULT_ENTRY_CONDITIONS` list in `engine/backtest.py`:
```python
{"left": "rsi", "operator": "is_below", "right": 30, "connector": "AND"}
```

Available operators: `is_above`, `is_below`, `is`, `is_not`, `crosses_above`, `crosses_below`
Available fields: `current_close`, `rsi`, `cpr_is_narrow`, `time_of_day`, `prev_candle`
Available values: `ema`, `supertrend`, `yesterday_high`, `yesterday_low`, `pivot`, `bc`, `tc`
