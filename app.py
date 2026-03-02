"""
app.py — AlgoForge FastAPI Backend
Fixed:
  - Bug 4: yfinance MultiIndex columns flattened correctly
  - Bug 5: live engine uses asyncio.create_task (not background_tasks)
  - Added /logo.jpg route for the frontend
"""

import asyncio, json, os, sys, inspect, time
from datetime import datetime
from typing import List, Optional

import pandas as pd
import numpy as np

# ── Guaranteed path fix ───────────────────────────────────────────
# inspect.getfile() works even when uvicorn reload corrupts __file__
_HERE = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
os.chdir(_HERE)
# ─────────────────────────────────────────────────────────────────

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
from broker.dhan     import DhanClient, ScripMaster
from engine.backtest import run_backtest, DEFAULT_ENTRY_CONDITIONS, DEFAULT_EXIT_CONDITIONS
from engine.live     import LiveEngine
from engine.paper_trading import PaperTradingEngine

# Initialize FastAPI app
app = FastAPI(title="AlgoForge", version="1.0.0")
app.add_middleware(CORSMiddleware,
                   allow_origins=["https://philipalgo.github.io",
                                  "http://philipalgoforge.local",
                                  "http://65.1.213.207",
                                  "http://127.0.0.1:8000",
                                  "http://localhost:8000"],
                   allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

# Initialize custom client ONCE and pass to engine
dhan        = DhanClient()
live_engine = LiveEngine(dhan)
paper_engine = PaperTradingEngine(dhan)

ws_clients: List[WebSocket] = []
_live_task  = None   # track the asyncio task
_paper_task = None   # track paper trading task


# ── Models ────────────────────────────────────────────────────────
class BacktestRequest(BaseModel):
    from_date:        str   = config.DEFAULT_FROM
    to_date:          str   = config.DEFAULT_TO
    symbol:           str   = "NIFTY"
    initial_capital:  float = config.DEFAULT_CAPITAL
    entry_conditions: Optional[List[dict]] = None
    exit_conditions:  Optional[List[dict]] = None
    strategy_config:  Optional[dict]       = None

class LiveStartRequest(BaseModel):
    entry_conditions: Optional[List[dict]] = None
    exit_conditions:  Optional[List[dict]] = None
    strategy_config:  Optional[dict]       = None
    # Full strategy fields (used when deploying from modal)
    run_name:         str   = ""
    instrument:       str   = ""
    indicators:       List[str]            = []
    legs:             Optional[List[dict]] = None
    deploy_config:    Optional[dict]       = None
    max_trades_per_day: int = 1
    market_open:      str   = "09:15"
    market_close:     str   = "15:25"
    max_daily_loss:   float = 0
    lots:             int   = 1
    stoploss_pct:     float = 10.0

class OrderRequest(BaseModel):
    security_id:      str
    exchange_segment: str   = "NSE_EQ"
    transaction_type: str
    quantity:         int
    order_type:       str   = "MARKET"
    product_type:     str   = "INTRADAY"
    price:            float = 0
    
class StrategyPayload(BaseModel):
    run_name:         str   = ""
    folder:           str   = "Intraday"
    segment:          str   = "indices"
    instrument:       str   = "26000"
    from_date:        str   = config.DEFAULT_FROM
    to_date:          str   = config.DEFAULT_TO
    initial_capital:  float = 500000.0
    lots:             int   = 4
    lot_size:         int   = 25
    stoploss_pct:     float = 10.0
    target_profit_pct: float = 20.0
    market_open:      str   = "09:15"
    market_close:     str   = "15:25"
    max_trades_per_day: int = 1
    indicators:       List[str]            = []
    entry_conditions: Optional[List[dict]] = None
    exit_conditions:  Optional[List[dict]] = None
    legs:             Optional[List[dict]] = None
    deploy_config:    Optional[dict]       = None
    combined_sl_rupees:     float = 0
    combined_target_rupees: float = 0
    combined_sqoff_time:    str   = "15:20"


# ── Serve Frontend ────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "strategy.html")
    if os.path.exists(html_path):
        with open(html_path, encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h2>strategy.html not found. Place it beside app.py</h2>")

@app.get("/logo.jpg")
async def serve_logo():
    """Serves the main application logo."""
    # This specifically fixes the 404 error you were seeing in the terminal!
    return FileResponse("logo.jpg")


# ── Health ────────────────────────────────────────────────────────
@app.get("/api/health")
async def health():
    return {
        "status":          "ok",
        "time":            str(datetime.now()),
        "dhan_configured": (config.DHAN_CLIENT_ID  != "YOUR_CLIENT_ID_HERE" and
                            config.DHAN_ACCESS_TOKEN != "YOUR_ACCESS_TOKEN_HERE"),
        "live_running":    live_engine.running,
    }

@app.get("/api/token-status")
async def token_status():
    """Check Dhan API token expiry"""
    return config.get_token_expiry()


# ── Broker Connection Validation ──────────────────────────────────
@app.post("/api/broker/check")
async def check_broker():
    """Check if broker connection is active and valid"""
    try:
        # Check if credentials are configured (not default placeholders)
        if (config.DHAN_CLIENT_ID == "YOUR_CLIENT_ID_HERE" or 
            config.DHAN_ACCESS_TOKEN == "YOUR_ACCESS_TOKEN_HERE"):
            return {
                "status": "not_configured",
                "broker": "Dhan",
                "message": "Dhan API credentials not configured. Please update .env file."
            }
        
        # Test connection by fetching account funds
        funds = dhan.get_funds()
        
        if funds and isinstance(funds, dict):
            # Valid response - connection is working
            available_balance = (
                float(funds.get("availabelBalance", 0) or 0)
            )
            return {
                "status": "connected",
                "broker": "Dhan",
                "message": "Broker connection active",
                "available_balance": available_balance,
                "funds": funds,
            }
        else:
            # No data returned
            return {
                "status": "error",
                "broker": "Dhan",
                "message": "Invalid response from broker API"
            }
            
    except Exception as e:
        error_msg = str(e)
        if "401" in error_msg or "Unauthorized" in error_msg:
            return {
                "status": "error",
                "broker": "Dhan",
                "message": "Invalid API credentials (401 Unauthorized)"
            }
        elif "403" in error_msg or "Forbidden" in error_msg:
            return {
                "status": "error",
                "broker": "Dhan",
                "message": "Access forbidden - check API permissions (403)"
            }
        elif "timeout" in error_msg.lower():
            return {
                "status": "error",
                "broker": "Dhan",
                "message": "Connection timeout - network issue"
            }
        else:
            return {
                "status": "error",
                "broker": "Dhan",
                "message": f"Connection error: {error_msg[:100]}"
            }


@app.get("/api/broker/trades")
async def get_broker_trades():
    """Fetch executed trades from Dhan broker account"""
    try:
        # Check if credentials are configured
        if (config.DHAN_CLIENT_ID == "YOUR_CLIENT_ID_HERE" or 
            config.DHAN_ACCESS_TOKEN == "YOUR_ACCESS_TOKEN_HERE"):
            return {
                "status": "not_configured",
                "message": "Dhan API credentials not configured",
                "trades": []
            }
        
        # Fetch trades from Dhan API
        trades = dhan.get_trades()
        
        return {
            "status": "success",
            "broker": "Dhan",
            "count": len(trades),
            "trades": trades
        }
            
    except Exception as e:
        error_msg = str(e)
        return {
            "status": "error",
            "broker": "Dhan",
            "message": f"Failed to fetch trades: {error_msg[:100]}",
            "trades": []
        }


@app.post("/api/broker/connect")
async def connect_broker():
    """Establish and validate broker connection"""
    try:
        # Check if credentials are configured (not default placeholders)
        if (config.DHAN_CLIENT_ID == "YOUR_CLIENT_ID_HERE" or 
            config.DHAN_ACCESS_TOKEN == "YOUR_ACCESS_TOKEN_HERE"):
            return {
                "status": "not_configured",
                "broker": "Dhan",
                "message": "Dhan API credentials not configured. Please add them to .env file."
            }
        
        # Test connection by attempting to fetch account funds
        funds = dhan.get_funds()
        
        if funds and isinstance(funds, dict):
            # Successfully connected and validated
            return {
                "status": "connected",
                "broker": "Dhan",
                "message": "Successfully connected to Dhan broker",
                "available_balance": funds.get("availabelBalance", 0),
                "client_id": config.DHAN_CLIENT_ID
            }
        else:
            # Connection made but no valid data
            return {
                "status": "error",
                "broker": "Dhan",
                "message": "Broker returned empty or invalid response"
            }
            
    except Exception as e:
        error_msg = str(e)
        
        # Provide specific error messages based on error type
        if "401" in error_msg or "Unauthorized" in error_msg:
            return {
                "status": "error",
                "broker": "Dhan",
                "message": "Invalid API credentials. Please check your Client ID and Access Token."
            }
        elif "403" in error_msg or "Forbidden" in error_msg:
            return {
                "status": "error",
                "broker": "Dhan",
                "message": "Access forbidden. Your API token may have expired or lacks permissions."
            }
        elif "timeout" in error_msg.lower() or "timed out" in error_msg.lower():
            return {
                "status": "error",
                "broker": "Dhan",
                "message": "Connection timeout. Please check your internet connection."
            }
        elif "connection" in error_msg.lower():
            return {
                "status": "error",
                "broker": "Dhan",
                "message": "Network error. Unable to reach Dhan API servers."
            }
        else:
            return {
                "status": "error",
                "broker": "Dhan",
                "message": f"Connection failed: {error_msg[:100]}"
            }


# ── Instrument Mapping ────────────────────────────────────────────
# Maps frontend values to Dhan API params
# IMPORTANT: Dhan security IDs for indices are DIFFERENT from scrip IDs
# Use Dhan's scrip master CSV to find correct security IDs
INSTRUMENT_MAP = {
    # Indices — Dhan security IDs (from Dhan scrip master)
    "26000": {"name": "NIFTY 50",      "dhan_id": "13",   "dhan_seg": "IDX_I", "dhan_type": "INDEX"},
    "26009": {"name": "BANK NIFTY",    "dhan_id": "25",   "dhan_seg": "IDX_I", "dhan_type": "INDEX"},
    "1":     {"name": "SENSEX",        "dhan_id": "51",   "dhan_seg": "IDX_I", "dhan_type": "INDEX"},  # BSE SENSEX: Try ID 51 for BSE
    "26017": {"name": "NIFTY FIN SVC", "dhan_id": "27",   "dhan_seg": "IDX_I", "dhan_type": "INDEX"},
    "26037": {"name": "NIFTY MIDCAP",  "dhan_id": "49",   "dhan_seg": "IDX_I", "dhan_type": "INDEX"},
    "26074": {"name": "NIFTY NEXT 50", "dhan_id": "26",   "dhan_seg": "IDX_I", "dhan_type": "INDEX"},
    "26013": {"name": "NIFTY IT",      "dhan_id": "30",   "dhan_seg": "IDX_I", "dhan_type": "INDEX"},
    # Stocks — Dhan NSE security IDs
    "RELIANCE":   {"name": "Reliance",      "dhan_id": "2885",  "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "TCS":        {"name": "TCS",           "dhan_id": "11536", "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "HDFCBANK":   {"name": "HDFC Bank",     "dhan_id": "1333",  "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "INFY":       {"name": "Infosys",       "dhan_id": "1594",  "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "ICICIBANK":  {"name": "ICICI Bank",    "dhan_id": "4963",  "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "HINDUNILVR": {"name": "HUL",           "dhan_id": "1394",  "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "ITC":        {"name": "ITC",           "dhan_id": "1660",  "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "SBIN":       {"name": "SBI",           "dhan_id": "3045",  "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "BHARTIARTL": {"name": "Bharti Airtel", "dhan_id": "10604", "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "BAJFINANCE": {"name": "Bajaj Finance", "dhan_id": "317",   "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "KOTAKBANK":  {"name": "Kotak Bank",    "dhan_id": "1922",  "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "LT":         {"name": "L&T",           "dhan_id": "11483", "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "HCLTECH":    {"name": "HCL Tech",      "dhan_id": "7229",  "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "ASIANPAINT": {"name": "Asian Paints",  "dhan_id": "236",   "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "AXISBANK":   {"name": "Axis Bank",     "dhan_id": "5900",  "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "MARUTI":     {"name": "Maruti",        "dhan_id": "10999", "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "SUNPHARMA":  {"name": "Sun Pharma",    "dhan_id": "3351",  "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "TITAN":      {"name": "Titan",         "dhan_id": "3506",  "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "ULTRACEMCO": {"name": "UltraTech",     "dhan_id": "11532", "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "BAJAJFINSV": {"name": "Bajaj Finserv", "dhan_id": "16675", "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "WIPRO":      {"name": "Wipro",         "dhan_id": "3787",  "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "NESTLEIND":  {"name": "Nestle",        "dhan_id": "17963", "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "TATAMOTORS": {"name": "Tata Motors",   "dhan_id": "3456",  "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "M_M":        {"name": "M&M",           "dhan_id": "2031",  "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
    "POWERGRID":  {"name": "Power Grid",    "dhan_id": "14977", "dhan_seg": "NSE_EQ", "dhan_type": "EQUITY"},
}


# ── Data Fetch (Dhan only — variable timeframe via chunking) ──────────
def _fetch_data(instrument: str, from_date: str, to_date: str, segment: str = "indices", candle_interval: str = "5") -> pd.DataFrame:
    """
    Fetches intraday OHLCV candles from Dhan API at specified interval.
    For date ranges > 30 days, automatically chunks into 30-day windows
    and concatenates the results. This gives us proper intraday data
    with exact timestamps for entry/exit.
    """
    inst_info = INSTRUMENT_MAP.get(instrument)
    if not inst_info:
        raise Exception(f"Unknown instrument: {instrument}. Not found in instrument map.")
    
    from datetime import datetime as dt, timedelta
    from_dt = dt.strptime(from_date, "%Y-%m-%d")
    to_dt   = dt.strptime(to_date, "%Y-%m-%d")
    day_span = (to_dt - from_dt).days
    
    CHUNK_DAYS = 28  # Dhan intraday API limit ~30 days, use 28 for safety
    
    print(f"[DATA] Instrument={instrument} ({inst_info['name']}), DhanID={inst_info['dhan_id']}, "
          f"Segment={inst_info['dhan_seg']}, Interval={candle_interval}m, From={from_date}, To={to_date}, Span={day_span}d")
    
    all_dfs = []
    chunk_start = from_dt
    chunk_num = 0
    last_error = None
    
    while chunk_start < to_dt:
        chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS), to_dt)
        chunk_num += 1
        
        cs = chunk_start.strftime("%Y-%m-%d")
        ce = chunk_end.strftime("%Y-%m-%d")
        
        print(f"[DATA] Chunk {chunk_num}: {cs} → {ce}")
        
        try:
            df_chunk = dhan.get_historical_data(
                security_id=inst_info["dhan_id"],
                exchange_segment=inst_info["dhan_seg"],
                instrument_type=inst_info["dhan_type"],
                from_date=cs,
                to_date=ce,
                candle_type=str(candle_interval),  # Use dynamic interval
            )
            if df_chunk is not None and not df_chunk.empty:
                all_dfs.append(df_chunk)
                print(f"[DATA]   → {len(df_chunk)} candles")
            else:
                print(f"[DATA]   → 0 candles (empty or None)")
        except Exception as e:
            last_error = str(e)
            print(f"[DATA]   → Error: {last_error}")
            # Continue to next chunk — don't fail the entire backtest
        
        chunk_start = chunk_end + timedelta(days=1)
    
    if not all_dfs:
        error_detail = f"No intraday data from Dhan for {inst_info['name']}. Check API subscription and date range."
        if last_error:
            error_detail += f" Last error: {last_error}"
        raise Exception(error_detail)
    
    df = pd.concat(all_dfs).sort_index()
    # Remove duplicates (overlapping chunk boundaries)
    df = df[~df.index.duplicated(keep='first')]
    
    print(f"[DATA] ✅ Total: {len(df)} {candle_interval}-min candles across {chunk_num} chunks, "
          f"{df.index[0]} → {df.index[-1]}")
    return df


# ── Backtest ──────────────────────────────────────────────────────
@app.post("/api/backtest")
async def api_run_backtest(payload: StrategyPayload):
    try:
        from_date = payload.from_date or config.DEFAULT_FROM
        to_date   = payload.to_date   or config.DEFAULT_TO
        
        # Extract timeframe from indicators (e.g., Supertrend_10_3_3m → 3)
        # Validate against supported Dhan intervals: 1, 5, 15, 25, 60, D
        candle_interval = "5"  # default
        valid_intervals = ["1", "5", "15", "25", "60", "D"]
        if payload.indicators:
            for ind in payload.indicators:
                if "_" in ind and ind.endswith("m"):
                    parts = ind.split("_")
                    for p in parts:
                        if p.endswith("m") and p[:-1].isdigit():
                            candidate = p[:-1]
                            if candidate in valid_intervals:
                                candle_interval = candidate
                            else:
                                print(f"[BACKTEST] Unsupported timeframe {candidate}m, using 5m")
                                candle_interval = "5"
                            break
        
        print(f"\n{'='*60}")
        print(f"[BACKTEST] Run: {payload.run_name}")
        print(f"[BACKTEST] Instrument: {payload.instrument}, Segment: {payload.segment}")
        print(f"[BACKTEST] Timeframe: {candle_interval}-minute candles")
        print(f"[BACKTEST] Indicators: {payload.indicators}")
        print(f"[BACKTEST] Entry conditions: {payload.entry_conditions}")
        print(f"[BACKTEST] Exit conditions: {payload.exit_conditions}")
        print(f"[BACKTEST] Legs: {payload.legs}")
        print(f"[BACKTEST] ⚠️  Using ESTIMATED option premiums (not historical data)")
        print(f"{'='*60}")

        # 1. Fetch data with segment-aware routing + fallback
        print(f"[BACKTEST] Fetching data from {from_date} to {to_date}...")
        try:
            df_raw = _fetch_data(
                instrument=payload.instrument,
                from_date=from_date,
                to_date=to_date,
                segment=payload.segment,
                candle_interval=candle_interval,
            )
        except Exception as fetch_err:
            error_msg = f"Data fetch failed: {str(fetch_err)}"
            print(f"[BACKTEST] {error_msg}")
            return {"status": "error", "message": error_msg}

        if df_raw is None or df_raw.empty:
            error_msg = "No data returned. Check credentials and date range."
            print(f"[BACKTEST] {error_msg}")
            return {"status": "error", "message": error_msg}

        print(f"[BACKTEST] Data: {len(df_raw)} candles, {df_raw.index[0]} → {df_raw.index[-1]}")

        # 2. Build strategy_config
        strategy_config = payload.model_dump()

        # 3. Run backtest
        print(f"[BACKTEST] Running backtest engine...")
        try:
            results = run_backtest(
                df_raw=df_raw,
                entry_conditions=payload.entry_conditions or DEFAULT_ENTRY_CONDITIONS,
                exit_conditions=payload.exit_conditions   or DEFAULT_EXIT_CONDITIONS,
                strategy_config=strategy_config,
            )
        except Exception as bt_err:
            error_msg = f"Backtest execution failed: {str(bt_err)}"
            print(f"[BACKTEST] {error_msg}")
            import traceback
            traceback.print_exc()
            return {"status": "error", "message": error_msg}

        print(f"[BACKTEST] Result: {results.get('status')}, "
              f"Trades: {results.get('stats', {}).get('total_trades', 0)}")
        
        # Save the run
        if results.get("status") == "success":
            runs = _load_runs()
            # Use max ID to avoid duplicates after deletes
            max_id = max([r.get("id", 0) for r in runs], default=0)
            run_entry = {
                "id": max_id + 1,
                "run_name": payload.run_name,
                "folder": payload.folder,
                "segment": payload.segment,
                "instrument": payload.instrument,
                "from_date": from_date,
                "to_date": to_date,
                "lots": payload.lots,
                "lot_size": payload.lot_size,
                "stoploss_pct": payload.stoploss_pct,
                "indicators": payload.indicators,
                "entry_conditions": payload.entry_conditions,
                "exit_conditions": payload.exit_conditions,
                "legs": payload.legs,
                "stats": results["stats"],
                "monthly": results.get("monthly", []),
                "day_of_week": results.get("day_of_week", []),
                "yearly": results.get("yearly", []),
                "trade_count": results["stats"]["total_trades"],
                "total_pnl": results["stats"]["total_pnl"],
                "created_at": str(datetime.now()),
            }
            # Store all trades (no need to trim)
            all_trades = results.get("trades", [])
            run_entry["trades"] = all_trades
            run_entry["equity"] = results.get("equity", [])
            runs.append(run_entry)
            _save_runs(runs)
            results["run_id"] = run_entry["id"]
            print(f"[BACKTEST] Saved as Run #{run_entry['id']}")
        
        return results

    except Exception as e:
        import traceback
        error_msg = f"Backtest failed: {str(e)}"
        print(f"[BACKTEST] FATAL ERROR: {error_msg}")
        traceback.print_exc()
        return {"status": "error", "message": error_msg, "details": str(e)}


# ── Live Engine ───────────────────────────────────────────────────
@app.post("/api/live/start")
async def live_start(req: LiveStartRequest):
    """Start live auto-trading with full strategy configuration."""
    global _live_task
    if live_engine.running:
        return {"status": "already_running"}

    # Build strategy dict from the request (same structure as paper engine)
    strategy_dict = {}
    if req.strategy_config:
        strategy_dict = dict(req.strategy_config)
    else:
        strategy_dict = {
            "run_name": req.run_name or "Live Strategy",
            "instrument": req.instrument or "26000",
            "indicators": req.indicators or [],
            "max_trades_per_day": int(req.max_trades_per_day or 1),
            "market_open": req.market_open or "09:15",
            "market_close": req.market_close or "15:25",
            "legs": req.legs or [],
            "deploy_config": req.deploy_config or {},
            "max_daily_loss": float(req.max_daily_loss or 0),
            "lots": req.lots,
            "stoploss_pct": req.stoploss_pct,
            "poll_interval": 10,
        }

    deploy_config = req.deploy_config or strategy_dict.get("deploy_config", {})

    live_engine.configure(
        strategy=strategy_dict,
        entry_conditions=req.entry_conditions or DEFAULT_ENTRY_CONDITIONS,
        exit_conditions=req.exit_conditions or DEFAULT_EXIT_CONDITIONS,
        deploy_config=deploy_config,
    )

    async def broadcast(event: dict):
        for ws in ws_clients.copy():
            try:
                await ws.send_json({"source": "live", **event})
            except Exception:
                if ws in ws_clients:
                    ws_clients.remove(ws)

    _live_task = asyncio.create_task(live_engine.start(callback=broadcast))
    return {"status": "started", "message": "Auto trading started with REAL orders"}


@app.post("/api/live/stop")
async def live_stop():
    global _live_task
    live_engine.stop()
    if _live_task and not _live_task.done():
        _live_task.cancel()
        try:
            await _live_task
        except asyncio.CancelledError:
            pass
    _live_task = None
    return {"status": "stopped"}


@app.get("/api/live/status")
async def live_status():
    return live_engine.get_status()


@app.get("/api/live/trades/csv")
async def export_live_trades_csv():
    """Export live auto-trading trades to CSV"""
    import io, csv as csv_mod
    if not live_engine or not live_engine.closed_trades:
        raise HTTPException(status_code=404, detail="No live trades available")
    output = io.StringIO()
    fields = ["id","leg_num","transaction_type","option_type","strike","entry_time",
              "exit_time","entry_premium","exit_premium","lots","lot_size","pnl",
              "exit_reason","entry_order_id","exit_order_id"]
    writer = csv_mod.DictWriter(output, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    for t in live_engine.closed_trades:
        row = {k: (str(v) if k in ('entry_time','exit_time') else v) for k, v in t.items() if k in fields}
        writer.writerow(row)
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=live_trades_{datetime.now().strftime('%Y%m%d')}.csv"}
    )


# ── Paper Trading (Real Market Data) ──────────────────────────────
@app.post("/api/paper/start")
async def paper_start(payload: StrategyPayload):
    """Start paper trading with real live market data"""
    global _paper_task
    
    if paper_engine.running:
        return {"status": "already_running"}
    
    # Configure strategy
    strategy_dict = {
        "run_name": payload.run_name,
        "instrument": payload.instrument,
        "indicators": payload.indicators or [],
        "max_trades_per_day": int(payload.max_trades_per_day or 1),
        "market_open": payload.market_open or "09:15",
        "market_close": payload.market_close or "15:25",
        "legs": payload.legs or [],
        "deploy_config": payload.deploy_config or {},
        "poll_interval": 10,  # Check every 10 seconds
    }
    
    paper_engine.configure(
        strategy=strategy_dict,
        entry_conditions=payload.entry_conditions or DEFAULT_ENTRY_CONDITIONS,
        exit_conditions=payload.exit_conditions or DEFAULT_EXIT_CONDITIONS
    )
    
    # Broadcast updates to WebSocket clients
    async def broadcast(event: dict):
        for ws in ws_clients.copy():
            try:
                await ws.send_json({"source": "paper", **event})
            except Exception:
                if ws in ws_clients:
                    ws_clients.remove(ws)
    
    # Start paper trading engine
    _paper_task = asyncio.create_task(paper_engine.start(callback=broadcast))
    
    return {"status": "started", "message": "Paper trading started with LIVE market data"}


@app.post("/api/paper/stop")
async def paper_stop():
    """Stop paper trading"""
    global _paper_task
    
    paper_engine.stop()
    
    if _paper_task and not _paper_task.done():
        _paper_task.cancel()
        try:
            await _paper_task
        except asyncio.CancelledError:
            pass
    
    _paper_task = None
    return {"status": "stopped"}


@app.get("/api/paper/status")
async def paper_status():
    """Get current paper trading status"""
    return paper_engine.get_status()


# ── WebSocket ─────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    ws_clients.append(ws)
    try:
        while True:
            await ws.send_json({"type": "status", **live_engine.get_status()})
            await asyncio.sleep(5)
    except (WebSocketDisconnect, Exception):
        if ws in ws_clients:
            ws_clients.remove(ws)


# ── Orders / Positions / Funds ────────────────────────────────────
@app.post("/api/orders/place")
async def place_order(req: OrderRequest):
    try:
        return dhan.place_order(
            security_id=req.security_id, exchange_segment=req.exchange_segment,
            transaction_type=req.transaction_type, quantity=req.quantity,
            order_type=req.order_type, product_type=req.product_type, price=req.price,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/orders")
async def get_orders():
    try: return dhan.get_order_book()
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/positions")
async def get_positions():
    try: return dhan.get_positions()
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/funds")
async def get_funds():
    try: return dhan.get_funds()
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/orders/{order_id}")
async def cancel_order(order_id: str):
    try: return dhan.cancel_order(order_id)
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))


# ── Strategy CRUD ─────────────────────────────────────────────────
STRAT_FILE = "strategies.json"
RUNS_FILE  = "runs.json"

def _load(): 
    if os.path.exists(STRAT_FILE):
        try:
            with open(STRAT_FILE, 'r') as f:
                return json.load(f)
        except:
            return []
    return []

def _save(d):
    with open(STRAT_FILE, 'w') as f:
        json.dump(d, f, indent=2)

def _load_runs():
    if os.path.exists(RUNS_FILE):
        try:
            with open(RUNS_FILE, 'r') as f:
                return json.load(f)
        except:
            return []
    return []

def _save_runs(d):
    with open(RUNS_FILE, 'w') as f:
        json.dump(d, f, indent=2)

@app.get("/api/strategies")
async def get_strategies():
    return _load()

@app.post("/api/strategies")
async def save_strategy(strategy: dict):
    strats = _load()
    # Use max ID to avoid duplicates after deletes
    max_id = max([s.get("id", 0) for s in strats], default=0)
    strategy.update({"id": max_id + 1, "created_at": str(datetime.now())})
    strats.append(strategy)
    _save(strats)
    return strategy

@app.delete("/api/strategies/{sid}")
async def delete_strategy(sid: int):
    _save([s for s in _load() if s.get("id") != sid])
    return {"deleted": sid}

@app.put("/api/strategies/{sid}")
async def update_strategy(sid: int, updates: dict):
    strats = _load()
    for s in strats:
        if s.get("id") == sid:
            s.update(updates)
            break
    _save(strats)
    return {"updated": sid}

# ── Backtest Runs CRUD ────────────────────────────────────────────
@app.get("/api/runs")
async def get_runs():
    runs = _load_runs()
    # Return summary without full trade lists for dashboard listing
    return [{ k: v for k, v in r.items() if k not in ("trades", "equity") } for r in runs]

@app.get("/api/runs/{rid}")
async def get_run(rid: int):
    runs = _load_runs()
    for r in runs:
        if r.get("id") == rid:
            return r
    raise HTTPException(status_code=404, detail="Run not found")

@app.delete("/api/runs/{rid}")
async def delete_run(rid: int):
    runs = _load_runs()
    _save_runs([r for r in runs if r.get("id") != rid])
    return {"deleted": rid}

@app.get("/api/runs/{rid}/csv")
async def export_run_csv(rid: int):
    """Export backtest trades to CSV"""
    import io, csv
    runs = _load_runs()
    run = None
    for r in runs:
        if r.get("id") == rid:
            run = r; break
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    trades = run.get("trades", [])
    if not trades:
        raise HTTPException(status_code=404, detail="No trades in this run")
    output = io.StringIO()
    fields = ["id","entry_time","exit_time","entry_price","exit_price","pnl","cumulative","exit_reason","option_type","strike","qty","txn_type"]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    for t in trades:
        writer.writerow(t)
    output.seek(0)
    name = run.get("run_name", f"run_{rid}").replace(" ", "_")
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={name}_trades.csv"}
    )

@app.get("/api/paper/trades/csv")
async def export_paper_trades_csv():
    """Export paper trading trades to CSV"""
    import io, csv
    if not paper_engine or not paper_engine.closed_trades:
        raise HTTPException(status_code=404, detail="No paper trades available")
    output = io.StringIO()
    fields = ["id","leg_num","transaction_type","option_type","strike","entry_time","exit_time","entry_premium","exit_premium","lots","lot_size","pnl","exit_reason"]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    for t in paper_engine.closed_trades:
        row = {k: (str(v) if k in ('entry_time','exit_time') else v) for k, v in t.items() if k in fields}
        writer.writerow(row)
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=paper_trades_{datetime.now().strftime('%Y%m%d')}.csv"}
    )

# ── Live Ticker (Dhan LTP) ───────────────────────────────────────


# Ticker caching
_ticker_cache = {"data": None, "timestamp": 0, "ttl": 60}  # Cache for 60 seconds

@app.get("/api/ticker")
async def get_ticker():
    """Fetch live index prices from yfinance (real market data)"""
    global _ticker_cache
    
    # Return cached data if still valid
    if _ticker_cache["data"] and (time.time() - _ticker_cache["timestamp"]) < _ticker_cache["ttl"]:
        age = time.time() - _ticker_cache["timestamp"]
        print(f"[TICKER] Returning cached data (age: {age:.1f}s)")
        return _ticker_cache["data"]
    
    try:
        import yfinance as yf
        
        print("[TICKER] Fetching live data from yfinance...")
        
        def _last_close_and_change(symbol: str):
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="2d")
            if hist.empty:
                return 0.0, 0.0, 0.0
            close = float(hist["Close"].iloc[-1])
            prev = float(hist["Close"].iloc[-2]) if len(hist) > 1 else close
            change = close - prev
            pct = (change / prev * 100) if prev else 0.0
            return close, change, pct

        nifty_price, nifty_chg, nifty_pct = _last_close_and_change("^NSEI")
        sensex_price, sensex_chg, sensex_pct = _last_close_and_change("^BSESN")
        vix_price, vix_chg, vix_pct = _last_close_and_change("^INDIAVIX")

        if nifty_price > 0:
            # Fetch real ATM CE/PE option prices from Dhan
            atm_ce_data = {"price": 0, "change": 0, "pct": 0}
            atm_pe_data = {"price": 0, "change": 0, "pct": 0}
            try:
                if dhan._is_configured():
                    ScripMaster.ensure_loaded()
                    expiry = ScripMaster.get_nearest_expiry("NIFTY")
                    if expiry:
                        atm_strike = round(nifty_price / 50) * 50
                        # Fetch both CE and PE in a single API call to avoid rate limits
                        ce_sid = ScripMaster.lookup("NIFTY", atm_strike, expiry, "CE")
                        pe_sid = ScripMaster.lookup("NIFTY", atm_strike, expiry, "PE")
                        if ce_sid and pe_sid:
                            both_ids = [int(ce_sid), int(pe_sid)]
                            data = dhan.get_ltp(both_ids, exchange_segment="NSE_FNO")
                            fno = data.get("NSE_FNO", {})
                            ce_ltp = float((fno.get(str(ce_sid), {}) or {}).get("last_price", 0))
                            pe_ltp = float((fno.get(str(pe_sid), {}) or {}).get("last_price", 0))
                            if ce_ltp > 0:
                                atm_ce_data = {"price": round(ce_ltp, 2), "change": 0, "pct": 0}
                            if pe_ltp > 0:
                                atm_pe_data = {"price": round(pe_ltp, 2), "change": 0, "pct": 0}
                            print(f"[TICKER] ATM {atm_strike}: CE={ce_ltp}, PE={pe_ltp}")
            except Exception as opt_err:
                print(f"[TICKER] ATM CE/PE fetch failed: {opt_err}")

            result = {
                "status": "ok",
                "nifty":  {"price": round(nifty_price, 2), "change": round(nifty_chg, 2), "pct": round(nifty_pct, 2)},
                "sensex": {"price": round(sensex_price, 2), "change": round(sensex_chg, 2), "pct": round(sensex_pct, 2)},
                "vix":    {"price": round(vix_price, 2), "change": round(vix_chg, 2), "pct": round(vix_pct, 2)},
                "atmCE":  atm_ce_data,
                "atmPE":  atm_pe_data,
            }
            
            _ticker_cache["data"] = result
            _ticker_cache["timestamp"] = time.time()
            print(f"[TICKER] Data cached: NIFTY={result['nifty']['price']}, SENSEX={result['sensex']['price']}")
            return result
        
        print("[TICKER] yfinance returned no price data")
        return {"status": "error", "msg": "No price data available"}
        
    except Exception as e:
        print(f"[TICKER] Exception: {type(e).__name__}: {str(e)[:100]}")
        # Try Dhan as fallback
        if dhan._is_configured():
            try:
                print("[TICKER] Trying Dhan API as fallback...")
                # Dhan get_ltp uses segment-specific security ID lists
                # Security IDs: NIFTY=13, SENSEX=1
                ltp_data = dhan.get_ltp(
                    security_ids=["13", "1"],  # NIFTY & SENSEX
                    exchange_segment="IDX_I"
                )
                
                # Extract prices from response
                nifty_ltp = 0
                sensex_ltp = 0
                
                if isinstance(ltp_data, dict):
                    # Check for nested structure
                    nifty_info = ltp_data.get("13", ltp_data.get("NIFTY", {}))
                    sensex_info = ltp_data.get("1", ltp_data.get("SENSEX", {}))
                    
                    if isinstance(nifty_info, dict):
                        nifty_ltp = float(nifty_info.get("last_price", nifty_info.get("ltp", 0)))
                    elif isinstance(nifty_info, (int, float)):
                        nifty_ltp = float(nifty_info)
                    
                    if isinstance(sensex_info, dict):
                        sensex_ltp = float(sensex_info.get("last_price", sensex_info.get("ltp", 0)))
                    elif isinstance(sensex_info, (int, float)):
                        sensex_ltp = float(sensex_info)
                
                if nifty_ltp > 0:
                    # Try real ATM CE/PE from Dhan option LTP
                    atm_ce_fb = {"price": 0, "change": 0, "pct": 0}
                    atm_pe_fb = {"price": 0, "change": 0, "pct": 0}
                    try:
                        ScripMaster.ensure_loaded()
                        fb_exp = ScripMaster.get_nearest_expiry("NIFTY")
                        if fb_exp:
                            atm_s = round(nifty_ltp / 50) * 50
                            ce_s = ScripMaster.lookup("NIFTY", atm_s, fb_exp, "CE")
                            pe_s = ScripMaster.lookup("NIFTY", atm_s, fb_exp, "PE")
                            if ce_s and pe_s:
                                both = dhan.get_ltp([int(ce_s), int(pe_s)], exchange_segment="NSE_FNO")
                                fno = both.get("NSE_FNO", {})
                                ce_p = float((fno.get(str(ce_s), {}) or {}).get("last_price", 0))
                                pe_p = float((fno.get(str(pe_s), {}) or {}).get("last_price", 0))
                                if ce_p > 0: atm_ce_fb["price"] = round(ce_p, 2)
                                if pe_p > 0: atm_pe_fb["price"] = round(pe_p, 2)
                    except: pass

                    result = {
                        "status": "ok",
                        "nifty":  {"price": nifty_ltp, "change": 0, "pct": 0},
                        "sensex": {"price": sensex_ltp, "change": 0, "pct": 0},
                        "vix":    {"price": 0, "change": 0, "pct": 0},
                        "atmCE":  atm_ce_fb,
                        "atmPE":  atm_pe_fb,
                    }
                    _ticker_cache["data"] = result
                    _ticker_cache["timestamp"] = time.time()
                    return result
            except Exception as ex:
                print(f"[TICKER] Dhan fallback failed: {ex}")
        
        return {"status": "error", "msg": str(e)[:100]}


# ── Expiry Dates ──────────────────────────────────────────────────
@app.get("/api/expiry-dates")
async def get_expiry_dates():
    """Return nearest expiry dates for NIFTY, BANKNIFTY, SENSEX"""
    try:
        ScripMaster.ensure_loaded()
        nifty_exp = ScripMaster.get_nearest_expiry("NIFTY") or ""
        bn_exp = ScripMaster.get_nearest_expiry("BANKNIFTY") or ""
        sensex_exp = ScripMaster.get_nearest_expiry("SENSEX") or ""
        return {
            "status": "ok",
            "nifty": nifty_exp,
            "banknifty": bn_exp,
            "sensex": sensex_exp,
        }
    except Exception as e:
        return {"status": "error", "msg": str(e)}


# ── Run ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    print("=" * 60)
    print("  AlgoForge — Starting Backend")
    print(f"  Open: http://{config.APP_HOST}:{config.APP_PORT}")
    print("=" * 60)
    uvicorn.run("app:app", host=config.APP_HOST, port=config.APP_PORT,
                reload=False, log_level="info")