"""
broker/dhan.py — Dhan HQ API Wrapper
Handles: historical data, order placement, order status, positions,
         scrip master download & option security ID lookup.
Docs: https://dhanhq.co/docs/latest/
"""

import requests
import pandas as pd
import csv
import io
import json
from datetime import datetime, timedelta, date as date_type
from typing import Optional, Dict, List
from pathlib import Path
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config


# ══════════════════════════════════════════════════════════════
#  SCRIP MASTER — Option Security ID Lookup
# ══════════════════════════════════════════════════════════════
SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
SCRIP_CACHE_DIR  = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".scrip_cache")

# Map frontend instrument ID → underlying symbol name in scrip master
UNDERLYING_MAP = {
    "26000": "NIFTY",
    "26009": "BANKNIFTY",
    "26037": "FINNIFTY",
    "26041": "MIDCPNIFTY",
    "1":     "SENSEX",
    "26017": "NIFTY FIN SERVICE",
}


class ScripMaster:
    """
    Download, cache, and lookup Dhan instrument security IDs for option contracts.
    Downloads the full scrip master CSV (~150MB), filters for index options only,
    and caches the result to a local JSON file (~2-5MB).
    """

    _options_cache: Dict[str, str] = {}   # key -> security_id
    _expiry_cache: Dict[str, list] = {}   # symbol -> [expiry_dates]
    _lot_cache: Dict[str, int] = {}       # key -> lot_size
    _loaded_date: Optional[str] = None
    _loading = False

    @classmethod
    def ensure_loaded(cls) -> bool:
        """Ensure scrip master is loaded for today. Returns True if loaded."""
        today = datetime.now().strftime("%Y-%m-%d")
        if cls._loaded_date == today and cls._options_cache:
            return True

        # Try disk cache first
        cache_file = os.path.join(SCRIP_CACHE_DIR, f"options_{today}.json")
        if os.path.exists(cache_file):
            try:
                with open(cache_file) as f:
                    data = json.load(f)
                cls._options_cache = data.get("options", {})
                cls._expiry_cache = data.get("expiries", {})
                cls._lot_cache = data.get("lots", {})
                cls._loaded_date = today
                print(f"[SCRIP] ✅ Loaded {len(cls._options_cache)} options from disk cache")
                return True
            except Exception as e:
                print(f"[SCRIP] Cache read error: {e}")

        # Download fresh
        return cls._download_and_filter(cache_file, today)

    @classmethod
    def _download_and_filter(cls, cache_file: str, today: str) -> bool:
        """Download scrip master CSV and extract option contracts."""
        if cls._loading:
            print("[SCRIP] Download already in progress...")
            return False
        cls._loading = True

        print("[SCRIP] 📥 Downloading Dhan scrip master (this may take 30-60 seconds)...")
        os.makedirs(SCRIP_CACHE_DIR, exist_ok=True)

        try:
            resp = requests.get(SCRIP_MASTER_URL, stream=True, timeout=180)
            resp.raise_for_status()
        except Exception as e:
            print(f"[SCRIP] ❌ Download failed: {e}")
            cls._loading = False
            return False

        options = {}
        expiries: Dict[str, set] = {}
        lots = {}
        # Map trading symbol prefix → canonical name used as key
        supported_prefixes = {
            "NIFTY": "NIFTY", "BANKNIFTY": "BANKNIFTY",
            "FINNIFTY": "FINNIFTY", "MIDCPNIFTY": "MIDCPNIFTY",
            "SENSEX": "SENSEX",
        }
        count = 0

        try:
            # Stream line-by-line to keep memory low
            # Force decode to str if bytes are returned
            def _lines():
                for raw in resp.iter_lines():
                    if isinstance(raw, bytes):
                        yield raw.decode("utf-8", errors="replace")
                    else:
                        yield raw

            lines_iter = _lines()
            header_line = next(lines_iter)
            # Parse header with proper CSV handling
            header = next(csv.reader([header_line]))
            col_map = {name.strip(): i for i, name in enumerate(header)}

            idx_sec   = col_map.get("SEM_SMST_SECURITY_ID", -1)
            idx_inst  = col_map.get("SEM_INSTRUMENT_NAME", -1)
            idx_stk   = col_map.get("SEM_STRIKE_PRICE", -1)
            idx_exp   = col_map.get("SEM_EXPIRY_DATE", -1)
            idx_opt   = col_map.get("SEM_OPTION_TYPE", -1)
            idx_exch  = col_map.get("SEM_EXM_EXCH_ID", -1)
            idx_lot   = col_map.get("SEM_LOT_UNITS", -1)
            idx_tsym  = col_map.get("SEM_TRADING_SYMBOL", -1)

            max_idx = max(idx_sec, idx_inst, idx_stk, idx_exp, idx_opt, idx_tsym)

            for line_text in lines_iter:
                if not line_text or not line_text.strip():
                    continue
                try:
                    row = next(csv.reader([line_text]))
                except:
                    continue
                if len(row) <= max_idx:
                    continue

                inst_name = row[idx_inst].strip() if idx_inst >= 0 else ""
                if inst_name != "OPTIDX":
                    continue

                # Extract symbol from SEM_TRADING_SYMBOL (e.g., "NIFTY-Mar2026-24000-CE")
                trading_sym = row[idx_tsym].strip() if idx_tsym >= 0 else ""
                sym_prefix = trading_sym.split("-")[0] if "-" in trading_sym else ""
                symbol = supported_prefixes.get(sym_prefix, "")
                if not symbol:
                    continue

                # Only NSE for most, BSE for SENSEX
                exch = row[idx_exch].strip() if idx_exch >= 0 else ""
                if symbol == "SENSEX" and exch != "BSE":
                    continue
                if symbol != "SENSEX" and exch != "NSE":
                    continue

                sec_id = row[idx_sec].strip() if idx_sec >= 0 else ""
                strike_raw = row[idx_stk].strip() if idx_stk >= 0 else ""
                expiry_raw = row[idx_exp].strip() if idx_exp >= 0 else ""
                opt_type = row[idx_opt].strip() if idx_opt >= 0 else ""
                lot_raw = row[idx_lot].strip() if idx_lot >= 0 else ""

                if not sec_id or not strike_raw or not opt_type:
                    continue

                # Normalize strike (remove trailing .00000)
                try:
                    strike_f = float(strike_raw)
                    strike_key = str(int(strike_f)) if strike_f == int(strike_f) else strike_raw
                except ValueError:
                    continue

                # Normalize expiry: extract date part from "YYYY-MM-DD HH:MM:SS"
                expiry = expiry_raw.strip().split(" ")[0] if " " in expiry_raw else expiry_raw.strip()

                key = f"{symbol}_{strike_key}_{expiry}_{opt_type}"
                options[key] = sec_id

                # Lot size (convert "25.0" → 25)
                try:
                    lot_val = int(float(lot_raw))
                    lots[f"{symbol}_{expiry}"] = lot_val
                except:
                    pass

                # Expiry tracking
                if symbol not in expiries:
                    expiries[symbol] = set()
                expiries[symbol].add(expiry)

                count += 1

        except StopIteration:
            pass
        except Exception as e:
            print(f"[SCRIP] Parse error: {e}")

        # Convert sets → sorted lists for JSON
        expiries_serial = {k: sorted(list(v)) for k, v in expiries.items()}

        cls._options_cache = options
        cls._expiry_cache = expiries_serial
        cls._lot_cache = lots
        cls._loaded_date = today
        cls._loading = False

        # Save to disk cache
        try:
            with open(cache_file, "w") as f:
                json.dump({"options": options, "expiries": expiries_serial, "lots": lots}, f)
            print(f"[SCRIP] ✅ Cached {count} option contracts for {list(expiries_serial.keys())}")
        except Exception as e:
            print(f"[SCRIP] Cache write error: {e}")

        # Clean up old cache files
        try:
            for old_file in Path(SCRIP_CACHE_DIR).glob("options_*.json"):
                if old_file.name != f"options_{today}.json":
                    old_file.unlink()
        except:
            pass

        return count > 0

    @classmethod
    def lookup(cls, symbol: str, strike: int, expiry: str, option_type: str) -> str:
        """Look up security ID for a specific option contract."""
        cls.ensure_loaded()
        key = f"{symbol}_{strike}_{expiry}_{option_type}"
        sec_id = cls._options_cache.get(key, "")
        if not sec_id:
            # Try with trailing .0 in strike (some scrip masters store decimals)
            alt_key = f"{symbol}_{strike}.0_{expiry}_{option_type}"
            sec_id = cls._options_cache.get(alt_key, "")
        if not sec_id:
            print(f"[SCRIP] ⚠ Security ID not found: {key}")
        return sec_id

    @classmethod
    def get_nearest_expiry(cls, symbol: str, from_date: str = None) -> str:
        """Get nearest weekly expiry for a symbol (>= today)."""
        cls.ensure_loaded()
        expiries = cls._expiry_cache.get(symbol, [])
        ref_date = from_date or datetime.now().strftime("%Y-%m-%d")
        future = [e for e in expiries if e >= ref_date]
        return future[0] if future else ""

    @classmethod
    def get_expiries(cls, symbol: str) -> list:
        """Get all available expiry dates for a symbol."""
        cls.ensure_loaded()
        return cls._expiry_cache.get(symbol, [])

    @classmethod
    def get_lot_size(cls, symbol: str, expiry: str) -> int:
        """Get lot size from scrip master."""
        cls.ensure_loaded()
        lot = cls._lot_cache.get(f"{symbol}_{expiry}", 0)
        if lot > 0:
            return lot
        # Fallback defaults
        defaults = {"NIFTY": 75, "BANKNIFTY": 15, "FINNIFTY": 25,
                    "MIDCPNIFTY": 50, "SENSEX": 10}
        return defaults.get(symbol, 75)

    @classmethod
    def instrument_to_symbol(cls, instrument_id: str) -> str:
        """Convert frontend instrument ID to underlying symbol."""
        return UNDERLYING_MAP.get(instrument_id, "NIFTY")


class DhanClient:
    def __init__(self, client_id: str = None, access_token: str = None):
        self.client_id    = client_id or config.DHAN_CLIENT_ID
        self.access_token = access_token or config.DHAN_ACCESS_TOKEN
        self.base_url     = config.DHAN_BASE_URL
        self.data_url     = config.DHAN_DATA_URL
        self.headers = {
            "access-token": self.access_token,
            "client-id":    self.client_id,
            "Content-Type": "application/json",
            "Accept":       "application/json",
        }

    def _is_configured(self) -> bool:
        return (self.client_id != "YOUR_CLIENT_ID_HERE" and
                self.access_token != "YOUR_ACCESS_TOKEN_HERE" and
                self.access_token != "PASTE_YOUR_NEW_TOKEN_HERE" and
                len(self.access_token) > 20)

    # ──────────────────────────────────────────────────────────
    # Historical Data
    # ──────────────────────────────────────────────────────────
    def get_historical_data(
        self,
        security_id: str,
        exchange_segment: str,   # IDX_I, NSE_EQ, BSE_EQ, NSE_FNO
        instrument_type: str,    # INDEX, EQUITY, FUTIDX, OPTIDX
        expiry_code: int = 0,    # 0=current week, 1=next week, 2=next month
        from_date: str = None,
        to_date: str = None,
        candle_type: str = "1",  # Supported: 1, 5, 15, 25, 60 (minutes), D (daily) - NO 3 or 30!
    ) -> pd.DataFrame:
        """
        Fetch OHLCV candles from Dhan Charts API v2
        
        Supported candle intervals:
        - Intraday: 1, 5, 15, 25, 60 minutes
        - Daily: D
        
        Note: 3-minute and 30-minute candles are NOT supported by Dhan API!
        """

        if not self._is_configured():
            raise ConnectionError(
                "Dhan credentials not set. Edit config.py with your client_id and access_token."
            )
        
        # Validate candle_type
        valid_intervals = ["1", "5", "15", "25", "60", "D"]
        if candle_type not in valid_intervals:
            raise ValueError(
                f"Invalid candle_type '{candle_type}'. Dhan API only supports: {', '.join(valid_intervals)}"
            )

        if not from_date:
            from_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
        if not to_date:
            to_date = datetime.now().strftime("%Y-%m-%d")

        print(f"[DHAN] Fetching: secId={security_id}, seg={exchange_segment}, "
              f"type={instrument_type}, candle={candle_type}, {from_date} → {to_date}")

        if candle_type == "D":
            # ── Daily / Historical candles ──
            endpoint = f"{self.data_url}/charts/historical"
            payload = {
                "securityId":       str(security_id),
                "exchangeSegment":  exchange_segment,
                "instrument":       instrument_type,
                "expiryCode":       expiry_code,
                "fromDate":         from_date,
                "toDate":           to_date,
            }
        else:
            # ── Intraday candles (1m, 5m, 15m, 25m, 60m) ──
            endpoint = f"{self.data_url}/charts/intraday"
            payload = {
                "securityId":       str(security_id),
                "exchangeSegment":  exchange_segment,
                "instrument":       instrument_type,
                "interval":         str(candle_type),
                "fromDate":         from_date,
                "toDate":           to_date,
            }

        print(f"[DHAN] POST {endpoint}")
        print(f"[DHAN] Payload: {payload}")

        resp = requests.post(endpoint, json=payload, headers=self.headers, timeout=30)
        
        print(f"[DHAN] Response: {resp.status_code}")
        
        if resp.status_code != 200:
            error_text = resp.text[:500]
            print(f"[DHAN] Error body: {error_text}")
            raise Exception(f"Dhan API error {resp.status_code}: {error_text}")

        data = resp.json()
        
        # Dhan v2 returns data directly as arrays (not wrapped in "data" key always)
        # Handle both formats
        if "open" in data:
            ohlcv = data
        elif "data" in data:
            ohlcv = data["data"]
        else:
            raise Exception(f"Unexpected Dhan response format: {list(data.keys())}")

        # Handle timestamp format — could be epoch seconds or milliseconds
        timestamps = ohlcv.get("start_Time", ohlcv.get("timestamp", ohlcv.get("start_time", [])))
        if not timestamps:
            raise Exception(f"No timestamp field found in response. Keys: {list(ohlcv.keys())}")
        
        # Auto-detect epoch format (seconds vs milliseconds)
        first_ts = timestamps[0] if timestamps else 0
        unit = "ms" if first_ts > 1e12 else "s"
        
        df = pd.DataFrame({
            "timestamp": pd.to_datetime(timestamps, unit=unit) + pd.Timedelta(hours=5, minutes=30),
            "open":   [float(x) for x in ohlcv["open"]],
            "high":   [float(x) for x in ohlcv["high"]],
            "low":    [float(x) for x in ohlcv["low"]],
            "close":  [float(x) for x in ohlcv["close"]],
            "volume": [int(x) for x in ohlcv.get("volume", [0]*len(ohlcv["open"]))],
        })
        df.set_index("timestamp", inplace=True)
        df.sort_index(inplace=True)
        
        print(f"[DHAN] ✅ Got {len(df)} candles: {df.index[0]} → {df.index[-1]}")
        return df

    def get_nifty_daily(self, from_date: str, to_date: str) -> pd.DataFrame:
        """Convenience: NIFTY daily OHLCV — uses security_id=13 for NIFTY 50"""
        return self.get_historical_data(
            security_id="13",
            exchange_segment="IDX_I",
            instrument_type="INDEX",
            from_date=from_date,
            to_date=to_date,
            candle_type="D",
        )

    def get_nifty_intraday(self, from_date: str, to_date: str, interval: str = "5") -> pd.DataFrame:
        """Convenience: NIFTY 5-min intraday candles"""
        return self.get_historical_data(
            security_id="13",
            exchange_segment="IDX_I",
            instrument_type="INDEX",
            from_date=from_date,
            to_date=to_date,
            candle_type=interval,
        )

    # ──────────────────────────────────────────────────────────
    # Order Management
    # ──────────────────────────────────────────────────────────
    def place_order(
        self,
        security_id: str,
        exchange_segment: str,
        transaction_type: str,   # BUY or SELL
        quantity: int,
        order_type: str = "MARKET",
        product_type: str = "INTRADAY",
        price: float = 0,
        trigger_price: float = 0,
        validity: str = "DAY",
        tag: str = "AlgoForge",
    ) -> dict:
        """Place an order via Dhan API"""
        if not self._is_configured():
            raise ConnectionError("Dhan credentials not set. Edit config.py first.")

        payload = {
            "dhanClientId":    self.client_id,
            "transactionType": transaction_type,
            "exchangeSegment": exchange_segment,
            "productType":     product_type,
            "orderType":       order_type,
            "validity":        validity,
            "securityId":      security_id,
            "quantity":        quantity,
            "price":           price,
            "triggerPrice":    trigger_price,
            "correlationId":   tag,
        }

        resp = requests.post(
            f"{self.base_url}/v2/orders",
            json=payload,
            headers=self.headers,
            timeout=10,
        )
        if resp.status_code not in (200, 201):
            raise Exception(f"Order placement failed {resp.status_code}: {resp.text}")

        return resp.json()

    def place_option_order(
        self,
        underlying: str,           # "NIFTY", "BANKNIFTY", etc.
        strike_price: int,
        option_type: str,          # CE or PE
        expiry: str,               # "2026-02-20"
        transaction_type: str,     # BUY or SELL
        quantity: int,
        order_type: str = "MARKET",
        product_type: str = "INTRADAY",
        price: float = 0,
        trigger_price: float = 0,
        tag: str = "AlgoForge",
    ) -> dict:
        """
        Place an options order using real security_id from scrip master.
        Looks up the correct Dhan security ID for the specific
        strike + expiry + option_type contract.
        """
        # Look up real security_id from scrip master
        security_id = ScripMaster.lookup(underlying, strike_price, expiry, option_type)
        if not security_id:
            raise Exception(
                f"Cannot find security ID for {underlying} {strike_price}{option_type} "
                f"expiry {expiry}. Scrip master may not be loaded or contract doesn't exist."
            )

        print(f"[DHAN] Option order: {transaction_type} {underlying} "
              f"{strike_price}{option_type} exp={expiry} qty={quantity} "
              f"type={order_type} product={product_type} secId={security_id}")

        exchange_seg = "BSE_FNO" if underlying == "SENSEX" else "NSE_FNO"

        return self.place_order(
            security_id=security_id,
            exchange_segment=exchange_seg,
            transaction_type=transaction_type,
            quantity=quantity,
            order_type=order_type,
            product_type=product_type,
            price=price,
            trigger_price=trigger_price,
            tag=tag,
        )

    def place_sl_order(
        self,
        underlying: str,
        strike_price: int,
        option_type: str,
        expiry: str,
        transaction_type: str,     # opposite of entry: BUY→SELL, SELL→BUY
        quantity: int,
        trigger_price: float,
        price: float = 0,
        product_type: str = "INTRADAY",
        order_type: str = "SL",    # SL or SL-M
        tag: str = "AlgoForge_SL",
    ) -> dict:
        """
        Place a stop-loss order for an option position.
        SL = Stop Loss Limit (needs both trigger and limit price)
        SL-M = Stop Loss Market (only trigger, market execution)
        """
        security_id = ScripMaster.lookup(underlying, strike_price, expiry, option_type)
        if not security_id:
            raise Exception(f"Cannot find security ID for SL order: {underlying} {strike_price}{option_type}")

        exchange_seg = "BSE_FNO" if underlying == "SENSEX" else "NSE_FNO"

        # For SL-M, price should be 0 (market execution on trigger)
        if order_type == "SL-M":
            price = 0

        print(f"[DHAN] SL Order: {transaction_type} {underlying} {strike_price}{option_type} "
              f"trigger=₹{trigger_price} price=₹{price} type={order_type}")

        return self.place_order(
            security_id=security_id,
            exchange_segment=exchange_seg,
            transaction_type=transaction_type,
            quantity=quantity,
            order_type=order_type,
            product_type=product_type,
            price=price,
            trigger_price=trigger_price,
            tag=tag,
        )

    def get_order_book(self) -> list:
        """Get all orders for the day"""
        resp = requests.get(
            f"{self.base_url}/v2/orders",
            headers=self.headers,
            timeout=10,
        )
        if resp.status_code != 200:
            raise Exception(f"Order book failed: {resp.text}")
        return resp.json().get("data", [])

    def get_trades(self, from_date: str = None, to_date: str = None) -> list:
        """Get all executed trades (trade book) 
        
        Note: Dhan API may only support current day trades by default.
        For historical trades across multiple days, you may need to call this 
        endpoint multiple times or check Dhan's reports/statements API.
        """
        if not self._is_configured():
            raise ConnectionError("Dhan credentials not configured")
        
        try:
            # Dhan /v2/trades endpoint gets today's trades by default
            # For historical data, we might need additional parameters
            resp = requests.get(
                f"{self.base_url}/v2/trades",
                headers=self.headers,
                timeout=10,
            )
            
            print(f"[DHAN] get_trades status: {resp.status_code}")
            
            if resp.status_code == 401:
                raise Exception("Unauthorized - Invalid access token or client ID")
            elif resp.status_code == 403:
                raise Exception("Forbidden - Access token may have expired")
            elif resp.status_code != 200:
                raise Exception(f"Trade book API returned {resp.status_code}: {resp.text[:200]}")
            
            data = resp.json()
            print(f"[DHAN] get_trades response keys: {list(data.keys())}")
            
            # Return trades array - handle both possible response formats
            if "data" in data:
                trades = data["data"]
            else:
                trades = data if isinstance(data, list) else []
            
            print(f"[DHAN] ✅ Retrieved {len(trades)} trades")
            return trades
                
        except requests.exceptions.Timeout:
            raise Exception("Connection timeout - please check your internet")
        except requests.exceptions.ConnectionError:
            raise Exception("Connection error - unable to reach Dhan servers")

    def cancel_order(self, order_id: str) -> dict:
        resp = requests.delete(
            f"{self.base_url}/v2/orders/{order_id}",
            headers=self.headers,
            timeout=10,
        )
        return resp.json()

    def get_positions(self) -> list:
        """Get current open positions"""
        resp = requests.get(
            f"{self.base_url}/v2/positions",
            headers=self.headers,
            timeout=10,
        )
        if resp.status_code != 200:
            raise Exception(f"Positions fetch failed: {resp.text}")
        return resp.json().get("data", [])

    def get_funds(self) -> dict:
        """Get available margin/funds"""
        if not self._is_configured():
            raise ConnectionError("Dhan credentials not configured")
        
        try:
            resp = requests.get(
                f"{self.base_url}/fundlimit",
                headers=self.headers,
                timeout=10,
            )
            
            print(f"[DHAN] get_funds status: {resp.status_code}")
            
            if resp.status_code == 401:
                raise Exception("Unauthorized - Invalid access token or client ID")
            elif resp.status_code == 403:
                raise Exception("Forbidden - Access token may have expired")
            elif resp.status_code != 200:
                raise Exception(f"API returned {resp.status_code}: {resp.text[:200]}")
            
            data = resp.json()
            print(f"[DHAN] get_funds response keys: {list(data.keys())}")
            
            # Dhan API returns different formats - handle both
            if "data" in data:
                return data["data"]
            elif "availabelBalance" in data or "sodLimit" in data:
                return data
            else:
                print(f"[DHAN] Unexpected response format: {data}")
                return data
                
        except requests.exceptions.Timeout:
            raise Exception("Connection timeout - please check your internet")
        except requests.exceptions.ConnectionError:
            raise Exception("Connection error - unable to reach Dhan servers")

    def get_ltp(self, security_ids: list, exchange_segment: str = "NSE_EQ") -> dict:
        """Get Last Traded Price for given securities"""
        payload = {
            "NSE_EQ": security_ids if exchange_segment == "NSE_EQ" else [],
            "NSE_FNO": security_ids if exchange_segment == "NSE_FNO" else [],
            "BSE_FNO": security_ids if exchange_segment == "BSE_FNO" else [],
            "IDX_I": security_ids if exchange_segment == "IDX_I" else [],
        }
        resp = requests.post(
            f"{self.base_url}/v2/marketfeed/ltp",
            json=payload,
            headers=self.headers,
            timeout=10,
        )
        if resp.status_code != 200:
            raise Exception(f"LTP fetch failed: {resp.text}")
        return resp.json().get("data", {})

    def get_option_ltp(self, underlying: str, strike: int, expiry: str,
                       option_type: str) -> float:
        """
        Get LTP for a specific option contract.
        Returns the last traded price, or 0 if not found.
        """
        security_id = ScripMaster.lookup(underlying, strike, expiry, option_type)
        if not security_id:
            print(f"[DHAN] Cannot get LTP — no security ID for {underlying} {strike}{option_type}")
            return 0.0

        exchange_seg = "BSE_FNO" if underlying == "SENSEX" else "NSE_FNO"
        try:
            data = self.get_ltp([security_id], exchange_segment=exchange_seg)
            # Dhan LTP response format varies; handle common structures
            if isinstance(data, dict):
                for key, val in data.items():
                    if isinstance(val, dict):
                        return float(val.get("last_price", val.get("ltp", 0)))
                    elif isinstance(val, (int, float)):
                        return float(val)
            return 0.0
        except Exception as e:
            print(f"[DHAN] Option LTP fetch failed: {e}")
            return 0.0

    def modify_order(self, order_id: str, order_type: str = None,
                     quantity: int = None, price: float = None,
                     trigger_price: float = None) -> dict:
        """Modify an existing order"""
        payload = {"dhanClientId": self.client_id, "orderId": order_id}
        if order_type:
            payload["orderType"] = order_type
        if quantity:
            payload["quantity"] = quantity
        if price is not None:
            payload["price"] = price
        if trigger_price is not None:
            payload["triggerPrice"] = trigger_price

        resp = requests.put(
            f"{self.base_url}/v2/orders/{order_id}",
            json=payload,
            headers=self.headers,
            timeout=10,
        )
        if resp.status_code not in (200, 201):
            raise Exception(f"Order modify failed: {resp.text}")
        return resp.json()

    def get_order_status(self, order_id: str) -> dict:
        """Get status of a specific order"""
        try:
            resp = requests.get(
                f"{self.base_url}/v2/orders/{order_id}",
                headers=self.headers,
                timeout=10,
            )
            if resp.status_code != 200:
                return {"orderStatus": "UNKNOWN"}
            return resp.json()
        except:
            return {"orderStatus": "UNKNOWN"}
