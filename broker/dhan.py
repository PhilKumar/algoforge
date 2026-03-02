"""
broker/dhan.py — Dhan HQ API Wrapper
Handles: historical data, order placement, order status, positions
Docs: https://dhanhq.co/docs/latest/
"""

import requests
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config


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
        strike_price: float,
        option_type: str,          # CE or PE
        expiry: str,               # "2026-02-20"
        transaction_type: str,     # BUY or SELL
        lots: int,
        lot_size: int = 25,        # NIFTY lot size
        order_type: str = "MARKET",
        product_type: str = "INTRADAY",
        price: float = 0,
        stoploss_pct: float = 10,
    ) -> dict:
        """Place NIFTY options order with optional bracket stoploss"""
        quantity = lots * lot_size
        # NOTE: For real options trading you need the actual security_id
        # of the specific strike+expiry contract from Dhan's scrip master
        # This is a simplified version — full implementation needs scrip lookup
        result = self.place_order(
            security_id=f"OPT_{strike_price}_{option_type}_{expiry}",  # placeholder
            exchange_segment="NSE_FNO",
            transaction_type=transaction_type,
            quantity=quantity,
            order_type=order_type,
            product_type=product_type,
            price=price,
            tag=f"AlgoForge_{option_type}_{strike_price}",
        )
        return result

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
