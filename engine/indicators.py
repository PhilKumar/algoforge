"""
engine/indicators.py — Technical Indicators
Fixed:
  - SuperTrend now uses numpy arrays (no pandas .iloc chained assignment)
  - CPR/Yesterday handle both daily AND intraday DataFrames correctly
  - Added: SMA, MACD, Bollinger Bands, VWAP, ATR, Stochastic RSI, ADX
"""

import numpy as np
import pandas as pd


def _clean(s):
    """Replace ±Inf with NaN so they never propagate into condition evaluation."""
    if isinstance(s, pd.DataFrame):
        return s.replace([np.inf, -np.inf], np.nan)
    return s.replace([np.inf, -np.inf], np.nan)


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return _clean(100 - (100 / (1 + rs)))


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """MACD indicator returning line, signal, histogram."""
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return pd.DataFrame(
        {
            "macd_line": macd_line,
            "macd_signal": signal_line,
            "macd_histogram": histogram,
        },
        index=series.index,
    )


def bollinger_bands(series: pd.Series, period: int = 20, std_dev: float = 2.0) -> pd.DataFrame:
    """Bollinger Bands: upper, middle, lower."""
    middle = series.rolling(window=period).mean()
    std = series.rolling(window=period).std()
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    width = (upper - lower) / middle.replace(0, np.nan) * 100
    return _clean(
        pd.DataFrame(
            {
                "bb_upper": upper,
                "bb_middle": middle,
                "bb_lower": lower,
                "bb_width": width,
            },
            index=series.index,
        )
    )


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range."""
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def vwap(df: pd.DataFrame) -> pd.Series:
    """Volume Weighted Average Price — resets daily for intraday data."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    if "volume" not in df.columns:
        return typical  # fallback if no volume
    tp_vol = typical * df["volume"]
    # Reset cumsum daily for intraday data
    if _is_intraday(df):
        groups = df.index.date
        cum_tp_vol = tp_vol.groupby(groups).cumsum()
        cum_vol = df["volume"].groupby(groups).cumsum()
    else:
        cum_tp_vol = tp_vol.cumsum()
        cum_vol = df["volume"].cumsum()
    return _clean(cum_tp_vol / cum_vol.replace(0, np.nan))


def stochastic_rsi(
    series: pd.Series, rsi_period: int = 14, stoch_period: int = 14, k_smooth: int = 3, d_smooth: int = 3
) -> pd.DataFrame:
    """Stochastic RSI."""
    rsi_val = rsi(series, rsi_period)
    min_rsi = rsi_val.rolling(window=stoch_period).min()
    max_rsi = rsi_val.rolling(window=stoch_period).max()
    denom = (max_rsi - min_rsi).replace(0, np.nan)
    stoch_k = 100 * (rsi_val - min_rsi) / denom
    stoch_k = stoch_k.rolling(window=k_smooth).mean()
    stoch_d = stoch_k.rolling(window=d_smooth).mean()
    return _clean(pd.DataFrame({"stoch_rsi_k": stoch_k, "stoch_rsi_d": stoch_d}, index=series.index))


def adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Average Directional Index (ADX) with +DI / -DI."""
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    up_move = high - prev_high
    down_move = prev_low - low
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)

    atr_val = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    atr_safe = atr_val.replace(0, np.nan)
    plus_di = 100 * plus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr_safe
    minus_di = 100 * minus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr_safe
    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / di_sum
    adx_val = dx.ewm(alpha=1.0 / period, adjust=False).mean()

    return _clean(
        pd.DataFrame(
            {
                "ADX": adx_val,
                "ADX_plus_di": plus_di,
                "ADX_minus_di": minus_di,
            },
            index=df.index,
        )
    )


def supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 2.7) -> pd.DataFrame:
    """Bug 1 fixed: pure numpy — no pandas .iloc chained assignment."""
    high = df["high"].values.astype(float)
    low = df["low"].values.astype(float)
    close = df["close"].values.astype(float)
    n = len(close)

    # True Range
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))

    # ATR via RMA (Wilder's smoothing — matches TradingView)
    alpha = 1.0 / period
    atr = np.zeros(n)
    atr[0] = tr[0]
    for i in range(1, n):
        atr[i] = alpha * tr[i] + (1 - alpha) * atr[i - 1]

    hl2 = (high + low) / 2.0
    upper_raw = hl2 + multiplier * atr
    lower_raw = hl2 - multiplier * atr

    upper = upper_raw.copy()
    lower = lower_raw.copy()
    st = np.zeros(n)
    st_dir = np.zeros(n, dtype=int)

    st[0] = lower[0]
    st_dir[0] = 1

    for i in range(1, n):
        lower[i] = lower_raw[i] if (lower_raw[i] > lower[i - 1] or close[i - 1] < lower[i - 1]) else lower[i - 1]
        upper[i] = upper_raw[i] if (upper_raw[i] < upper[i - 1] or close[i - 1] > upper[i - 1]) else upper[i - 1]

        if st[i - 1] == upper[i - 1]:
            if close[i] > upper[i]:
                st[i] = lower[i]
                st_dir[i] = 1
            else:
                st[i] = upper[i]
                st_dir[i] = -1
        else:
            if close[i] < lower[i]:
                st[i] = upper[i]
                st_dir[i] = -1
            else:
                st[i] = lower[i]
                st_dir[i] = 1

    result = df.copy()
    result["supertrend"] = st
    result["supertrend_dir"] = st_dir
    return result


def _is_intraday(df: pd.DataFrame) -> bool:
    if len(df) < 2:
        return False
    return (df.index[1] - df.index[0]).total_seconds() < 86400


def cpr(df: pd.DataFrame, narrow_pct: float = 0.2, moderate_pct: float = 0.5, wide_pct: float = 0.5) -> pd.DataFrame:
    """Bug 2 fixed: handles both intraday and daily DataFrames."""
    intraday = _is_intraday(df)
    daily = (
        df.resample("D").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
        if intraday
        else df.copy()
    )

    daily["pivot"] = (daily["high"] + daily["low"] + daily["close"]) / 3
    daily["bc"] = (daily["high"] + daily["low"]) / 2
    daily["tc"] = daily["pivot"] * 2 - daily["bc"]
    daily["cpr_range"] = (daily["tc"] - daily["bc"]).abs()
    daily["cpr_width_pct"] = daily["cpr_range"] / daily["close"].replace(0, np.nan) * 100

    # Floor Pivot Support & Resistance Levels
    daily["R1"] = daily["pivot"] * 2 - daily["low"]
    daily["S1"] = daily["pivot"] * 2 - daily["high"]
    daily["R2"] = daily["pivot"] + (daily["high"] - daily["low"])
    daily["S2"] = daily["pivot"] - (daily["high"] - daily["low"])
    daily["R3"] = daily["high"] + 2 * (daily["pivot"] - daily["low"])
    daily["S3"] = daily["low"] - 2 * (daily["high"] - daily["pivot"])
    daily["R4"] = daily["R3"] + (daily["high"] - daily["low"])
    daily["S4"] = daily["S3"] - (daily["high"] - daily["low"])
    daily["R5"] = daily["R4"] + (daily["high"] - daily["low"])
    daily["S5"] = daily["S4"] - (daily["high"] - daily["low"])

    daily["cpr_type"] = daily["cpr_width_pct"].apply(
        lambda w: "narrow" if w <= narrow_pct else ("moderate" if w <= moderate_pct else "wide")
    )

    pivot_cols = [
        "pivot",
        "bc",
        "tc",
        "cpr_width_pct",
        "cpr_type",
        "R1",
        "R2",
        "R3",
        "R4",
        "R5",
        "S1",
        "S2",
        "S3",
        "S4",
        "S5",
    ]
    shifted = daily[pivot_cols].shift(1)

    result = df.copy()
    if intraday:
        result = result.join(shifted.reindex(result.index, method="ffill"))
    else:
        for col in pivot_cols:
            result[col] = shifted[col].reindex(result.index, method="ffill")

    result["cpr_is_narrow"] = result["cpr_type"] == "narrow"
    return result


def yesterday_candle(df: pd.DataFrame) -> pd.DataFrame:
    """Bug 2 fixed: handles both intraday and daily DataFrames."""
    intraday = _is_intraday(df)
    daily = (
        df.resample("D").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
        if intraday
        else df.copy()
    )

    daily["yesterday_high"] = daily["high"].shift(1)
    daily["yesterday_low"] = daily["low"].shift(1)
    daily["yesterday_close"] = daily["close"].shift(1)
    daily["yesterday_open"] = daily["open"].shift(1)

    yest_cols = ["yesterday_high", "yesterday_low", "yesterday_close", "yesterday_open"]
    result = df.copy()
    if intraday:
        result = result.join(daily[yest_cols].reindex(result.index, method="ffill"))
    else:
        for col in yest_cols:
            result[col] = daily[col].reindex(result.index, method="ffill")

    return result


def orb(df: pd.DataFrame, window_minutes: int = 15, market_open_str: str = "09:15") -> pd.DataFrame:
    """Opening Range Breakout — computes ORB high/low from the first N minutes of each day."""
    intraday = _is_intraday(df)
    if not intraday:
        # For daily data, ORB doesn't apply — return empty columns
        result = df.copy()
        result["ORB_High"] = np.nan
        result["ORB_Low"] = np.nan
        result["ORB_Range"] = np.nan
        return result

    result = df.copy()
    result["ORB_High"] = np.nan
    result["ORB_Low"] = np.nan

    # Group by date, find high/low of candles within the opening window
    from datetime import time as dtime
    from datetime import timedelta

    mo_h, mo_m = map(int, market_open_str.split(":"))
    market_open = dtime(mo_h, mo_m)
    orb_end = (pd.Timestamp(f"2000-01-01 {market_open_str}") + timedelta(minutes=window_minutes)).time()

    for date, group in result.groupby(result.index.date):
        orb_mask = (group.index.time >= market_open) & (group.index.time < orb_end)
        orb_candles = group[orb_mask]
        if len(orb_candles) > 0:
            orb_high = orb_candles["high"].max()
            orb_low = orb_candles["low"].min()
            # Only fill AFTER opening range closes (prevent look-ahead)
            post_orb = (result.index.date == date) & (result.index.time >= orb_end)
            result.loc[post_orb, "ORB_High"] = orb_high
            result.loc[post_orb, "ORB_Low"] = orb_low

    result["ORB_Range"] = result["ORB_High"] - result["ORB_Low"]
    result["ORB_is_breakout_up"] = result["close"] > result["ORB_High"]
    result["ORB_is_breakout_down"] = result["close"] < result["ORB_Low"]
    result["ORB_is_inside"] = (~result["ORB_is_breakout_up"]) & (~result["ORB_is_breakout_down"])
    return result


def compute_dynamic_indicators(df: pd.DataFrame, ui_indicators: list) -> pd.DataFrame:
    """
    Takes the raw Dhan DataFrame and the list of indicators from the UI
    (e.g., ['EMA_14_5m', 'Supertrend_10_3.0_5m']) and computes them dynamically.
    """
    df = df.copy()

    # 1. Always calculate basic candle data so 'current_close' works
    df = yesterday_candle(df)
    df["time_of_day"] = df.index.time

    # Always expose current candle OHLC columns
    df["current_open"] = df["open"]
    df["current_high"] = df["high"]
    df["current_low"] = df["low"]
    df["current_close"] = df["close"]

    # Always expose Previous Day columns (from yesterday_candle)
    df["Yesterday_Open"] = df["yesterday_open"]
    df["Yesterday_High"] = df["yesterday_high"]
    df["Yesterday_Low"] = df["yesterday_low"]
    df["Yesterday_Close"] = df["yesterday_close"]

    # 2. Loop through whatever the user selected in the UI
    for ind_string in ui_indicators:
        parts = ind_string.split("_")
        name = parts[0]

        # Calculate EMA
        if name == "EMA":
            period = int(parts[1])
            df[ind_string] = ema(df["close"], period)

        # Calculate SMA
        elif name == "SMA":
            period = int(parts[1])
            df[ind_string] = sma(df["close"], period)

        # Calculate RSI
        elif name == "RSI":
            period = int(parts[1])
            df[ind_string] = rsi(df["close"], period)

        # Calculate MACD
        elif name == "MACD":
            fast = int(parts[1]) if len(parts) > 1 else 12
            slow = int(parts[2]) if len(parts) > 2 else 26
            sig = int(parts[3]) if len(parts) > 3 else 9
            macd_df = macd(df["close"], fast, slow, sig)
            df["MACD_line"] = macd_df["macd_line"]
            df["MACD_signal"] = macd_df["macd_signal"]
            df["MACD_histogram"] = macd_df["macd_histogram"]

        # Calculate Bollinger Bands
        elif name == "BB":
            period = int(parts[1]) if len(parts) > 1 else 20
            std = float(parts[2]) if len(parts) > 2 else 2.0
            bb_df = bollinger_bands(df["close"], period, std)
            df["BB_upper"] = bb_df["bb_upper"]
            df["BB_middle"] = bb_df["bb_middle"]
            df["BB_lower"] = bb_df["bb_lower"]
            df["BB_width"] = bb_df["bb_width"]

        # Calculate VWAP
        elif name == "VWAP":
            df["VWAP"] = vwap(df)

        # Calculate ATR
        elif name == "ATR":
            period = int(parts[1]) if len(parts) > 1 else 14
            df[ind_string] = atr(df, period)

        # Calculate Stochastic RSI
        elif name == "StochRSI":
            period = int(parts[1]) if len(parts) > 1 else 14
            srsi = stochastic_rsi(df["close"], period)
            df["StochRSI_K"] = srsi["stoch_rsi_k"]
            df["StochRSI_D"] = srsi["stoch_rsi_d"]

        # Calculate ADX
        elif name == "ADX":
            period = int(parts[1]) if len(parts) > 1 else 14
            adx_df = adx(df, period)
            df["ADX"] = adx_df["ADX"]
            df["ADX_plus_di"] = adx_df["ADX_plus_di"]
            df["ADX_minus_di"] = adx_df["ADX_minus_di"]

        # Calculate Supertrend
        elif name == "Supertrend":
            period = int(parts[1])
            multiplier = float(parts[2])
            st_df = supertrend(df, period=period, multiplier=multiplier)
            df[ind_string] = st_df["supertrend"]

        # Calculate CPR
        elif name == "CPR":
            narrow_pct = float(parts[1]) if len(parts) > 1 else 0.2
            moderate_pct = float(parts[2]) if len(parts) > 2 else 0.5

            df = cpr(df, narrow_pct=narrow_pct, moderate_pct=moderate_pct, wide_pct=moderate_pct)

            df["CPR_Pivot"] = df["pivot"]
            df["CPR_TC"] = df["tc"]
            df["CPR_BC"] = df["bc"]
            df["CPR_width_pct"] = df["cpr_width_pct"]
            df["CPR_is_narrow"] = df["cpr_type"] == "narrow"
            df["CPR_is_moderate"] = df["cpr_type"] == "moderate"
            df["CPR_is_wide"] = df["cpr_type"] == "wide"
            # Support & Resistance levels
            for lvl in ["R1", "R2", "R3", "R4", "R5", "S1", "S2", "S3", "S4", "S5"]:
                df[f"CPR_{lvl}"] = df[lvl]
            df[ind_string] = df["pivot"]

        # Calculate ORB (Opening Range Breakout)
        elif name == "ORB":
            # Parse: ORB_15min → window_minutes=15
            window_str = parts[1] if len(parts) > 1 else "15min"
            window_minutes = int(window_str.replace("min", ""))
            df = orb(df, window_minutes=window_minutes)

        # Current Candle & Previous Day — already computed above, just skip
        elif name in ("Current", "Previous"):
            pass

    return df
