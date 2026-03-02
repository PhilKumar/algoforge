"""
engine/backtest.py — AlgoForge Backtest Engine v3
- Accurate NIFTY lot sizes (75 before Jan 2026, 65 from Jan 2026)
- Entry earliest at 09:20 (skip only first candle for warmup)
- P&L starts from 0 (not initial capital)
- Strike computed as nearest 50 for NIFTY, nearest 100 for BANKNIFTY
- Day of week / Time of day indicators
"""
import pandas as pd
import numpy as np
from datetime import datetime, time, date
from typing import List
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from engine.indicators import compute_dynamic_indicators
import config

# ── Lot Size Lookup (accurate) ────────────────────────────────────
LOT_SIZES = {
    "NIFTY":     [(date(2026, 1, 1), 65), (date(2000, 1, 1), 75)],
    "BANKNIFTY": [(date(2026, 1, 1), 30), (date(2000, 1, 1), 25)],
    "FINNIFTY":  [(date(2026, 1, 1), 65), (date(2000, 1, 1), 40)],
    "SENSEX":    [(date(2026, 1, 1), 20), (date(2000, 1, 1), 10)],
}

def get_lot_size(instrument, trade_date):
    """Get correct lot size for instrument on a given date"""
    name = "NIFTY"
    if "26009" in str(instrument) or "BANK" in str(instrument).upper():
        name = "BANKNIFTY"
    elif "26017" in str(instrument) or "FIN" in str(instrument).upper():
        name = "FINNIFTY"
    elif "1" == str(instrument) or "SENSEX" in str(instrument).upper():
        name = "SENSEX"
    
    for cutoff, ls in LOT_SIZES.get(name, [(date(2000,1,1), 75)]):
        if trade_date >= cutoff:
            return ls
    return 75

def get_strike_step(instrument):
    """ATM strike rounding: 50 for NIFTY, 100 for BANKNIFTY/SENSEX"""
    if "26009" in str(instrument) or "BANK" in str(instrument).upper():
        return 100
    elif "1" == str(instrument) or "SENSEX" in str(instrument).upper():
        return 100
    return 50

# ── Time Parser ────────────────────────────────────────────────────
def _parse_time(val):
    if isinstance(val, time): return val
    if not isinstance(val, str): return time(9, 15)
    s = val.strip().upper()
    pm = "PM" in s; am = "AM" in s
    s = s.replace("AM","").replace("PM","").strip()
    parts = s.split(":")
    h = int(parts[0]); m = int(parts[1]) if len(parts) > 1 else 0
    if pm and h < 12: h += 12
    elif am and h == 12: h = 0
    return time(h, m)

# ── Condition Evaluator ────────────────────────────────────────────
def eval_condition(row, cond):
    left = cond["left"]
    op = cond["operator"]
    
    # Special: Time Of Day — compare candle time vs HH:MM or HH:MM:SS
    if left == "Time_Of_Day":
        ts = row.name if hasattr(row, 'name') else None
        if ts is None: return False
        cur_minutes = ts.hour * 60 + ts.minute
        rhs = cond.get("right_time", cond.get("right", "09:15"))
        parts = str(rhs).split(":")
        rhs_minutes = int(parts[0]) * 60 + int(parts[1]) if len(parts) >= 2 else 0
        if op in ("is_below","crosses_below","<"): return cur_minutes < rhs_minutes
        elif op in ("is_above","crosses_above",">"): return cur_minutes > rhs_minutes
        elif op in (">=","=="): return cur_minutes >= rhs_minutes
        elif op == "<=": return cur_minutes <= rhs_minutes
        return False
    
    # Special: Day Of Week — check if current day is in selected days
    if left == "Day_Of_Week":
        ts = row.name if hasattr(row, 'name') else None
        if ts is None: return False
        day_name = ts.strftime("%A")  # Monday, Tuesday, etc.
        if op == "contains":
            selected = cond.get("right_days", [])
            if isinstance(selected, str): selected = [selected]
            return day_name in selected
        elif op == "not_contains":
            selected = cond.get("right_days", [])
            if isinstance(selected, str): selected = [selected]
            return day_name not in selected
        return False
    
    # Standard indicator conditions
    lv = row.get("close") if left == "current_close" else row.get(left)
    r = cond["right"]
    if r == "current_close": rv = row.get("close")
    elif r == "number": rv = float(cond.get("right_number_value", 0))
    elif r in ("true","false"): rv = r == "true"
    else: rv = row.get(r)
    try:
        if lv is None or rv is None: return False
        if isinstance(lv, float) and pd.isna(lv): return False
        if not isinstance(rv, bool) and isinstance(rv, float) and pd.isna(rv): return False
    except: return False
    
    lv_f = float(lv)
    rv_f = float(rv)
    
    # Note: "crosses_above" and "crosses_below" require historical context not available in this function
    # For now, implement them the same as is_above/is_below. In a full implementation,
    # you would track previous candle values in the main backtest loop.
    if op in ("is_above","crosses_above"): return lv_f > rv_f
    elif op in ("is_below","crosses_below"): return lv_f < rv_f
    elif op == "==": return bool(lv)==rv if isinstance(rv,bool) else lv_f==rv_f
    elif op == ">=": return lv_f >= rv_f
    elif op == "<=": return lv_f <= rv_f
    elif op == "is_true": return bool(lv)
    elif op == "is_false": return not bool(lv)
    return False

def eval_condition_group(row, conditions):
    if not conditions: return False
    result = eval_condition(row, conditions[0])
    for c in conditions[1:]:
        v = eval_condition(row, c)
        conn = c.get("logic", c.get("connector","AND")).upper()
        if conn in ("AND","IF"): result = result and v
        elif conn == "OR": result = result or v
    return result

DEFAULT_ENTRY_CONDITIONS = [{"left":"current_close","operator":"is_above","right":"EMA_20_5m","connector":"AND"}]
DEFAULT_EXIT_CONDITIONS = [{"left":"current_close","operator":"is_below","right":"EMA_20_5m","connector":"AND"}]

# ── Option Helpers ─────────────────────────────────────────────────
def _est_prem(ci, ei, ep, ot, atm_prem=None):
    """Estimate current option premium given index move. Uses dynamic delta."""
    if atm_prem and atm_prem > 0 and ep > 0:
        # Dynamic delta: ITM options have higher delta
        d = min(0.95, ep / (ep + atm_prem))
    else:
        d = 0.5  # fallback ATM delta
    if ot == "PE": d = -d
    return max(0.05, ep + (ci - ei) * d)

def _opt_pnl(ep, xp, lots, ls, txn):
    d = xp - ep; 
    if txn == "SELL": d = -d
    return d * lots * ls

def _idx_pnl(e, x, lots, ls):
    return (x - e) * lots * ls

def _mk(id_, et, xt, ep, xp, pnl, reason, cum, ot=None, strike=None, qty=0, txn=None):
    return {"id":id_,"entry_time":str(et)[:16],"exit_time":str(xt)[:16],
            "entry_price":round(ep,2),"exit_price":round(xp,2),
            "pnl":round(pnl,2),"exit_reason":reason,"cumulative":round(cum,2),
            "option_type":ot,"strike":strike or "","qty":qty,"txn_type":txn or ""}

# ── Backtest Runner ────────────────────────────────────────────────
def run_backtest(df_raw, entry_conditions=None, exit_conditions=None, strategy_config=None):
    if entry_conditions is None: entry_conditions = DEFAULT_ENTRY_CONDITIONS
    if exit_conditions is None:  exit_conditions = DEFAULT_EXIT_CONDITIONS
    sc = strategy_config or {}

    mkt_open  = _parse_time(sc.get("market_open", "09:15"))
    mkt_close = _parse_time(sc.get("market_close", "15:25"))
    lots      = int(sc.get("lots", 1))
    sl_pct    = float(sc.get("stoploss_pct", 10))
    max_tpd   = int(sc.get("max_trades_per_day", config.MAX_TRADES_PER_DAY))
    indicators = sc.get("indicators", []) or []
    legs      = sc.get("legs", []) or []
    instrument = sc.get("instrument", "26000")
    strike_step = get_strike_step(instrument)

    # Option leg
    has_opt=False; ot=None; ltxn="BUY"; lsl=0; ltgt=0; sqoff=mkt_close
    strike_type="atm"; strike_value=0
    if legs and isinstance(legs, list) and len(legs) > 0:
        leg = legs[0]
        if leg.get("option_type") in ("CE","PE"):
            has_opt=True; ot=leg["option_type"]; ltxn=leg.get("transaction_type","BUY")
            lsl=float(leg.get("sl_pct",0) or 0); ltgt=float(leg.get("target_pct",0) or 0)
            lots=int(leg.get("lots",1) or 1)
            strike_type=leg.get("strike_type","atm") or "atm"
            strike_value=float(leg.get("strike_value",0) or 0)
            if leg.get("sqoff_time"): sqoff=_parse_time(leg["sqoff_time"])

    # Add day-of-week and time-of-day columns to df
    df_raw = df_raw.copy()
    df_raw["Day_of_Week"] = df_raw.index.dayofweek  # 0=Mon ... 4=Fri
    df_raw["Day_Name"] = df_raw.index.strftime("%A")
    df_raw["Hour"] = df_raw.index.hour
    df_raw["Minute"] = df_raw.index.minute
    df_raw["Time_HHMM"] = df_raw.index.strftime("%H:%M")
    # Boolean indicators for conditions
    df_raw["Is_Monday"] = (df_raw.index.dayofweek == 0).astype(float)
    df_raw["Is_Tuesday"] = (df_raw.index.dayofweek == 1).astype(float)
    df_raw["Is_Wednesday"] = (df_raw.index.dayofweek == 2).astype(float)
    df_raw["Is_Thursday"] = (df_raw.index.dayofweek == 3).astype(float)
    df_raw["Is_Friday"] = (df_raw.index.dayofweek == 4).astype(float)

    df = compute_dynamic_indicators(df_raw, indicators)
    is_daily = len(df)>=2 and (df.index[1]-df.index[0]).total_seconds()>=86400

    # Entry earliest: 09:20 = skip just the first candle (09:15) for indicator warmup
    ENTRY_EARLIEST = time(9, 20)

    # P&L starts from 0, not from initial capital
    total_pnl = 0.0
    trades=[]; equity=[]; in_trade=False
    ei=0.0; ep=0.0; et=None; slp=0.0; tgtp=0.0; td=0; ld=None
    strike_name=""; trade_qty=0; lot_size=75; atm_prem_ref=0

    print(f"[BT] open={mkt_open} close={mkt_close} lots={lots} sl={sl_pct}% sqoff={sqoff}")
    print(f"[BT] opt={has_opt} type={ot} txn={ltxn} sl%={lsl} tgt%={ltgt}")

    for ts, row in df.iterrows():
        ct=ts.time(); cd=ts.date()

        if cd != ld:
            if in_trade and ld is not None and not is_daily:
                o=float(row["open"])
                xp=_est_prem(o,ei,ep,ot,atm_prem_ref) if has_opt else o
                pnl=_opt_pnl(ep,xp,lots,lot_size,ltxn) if has_opt else _idx_pnl(ei,o,lots,lot_size)
                total_pnl+=pnl; trades.append(_mk(len(trades)+1,et,ts,ep if has_opt else ei,xp if has_opt else o,pnl,"EOD",total_pnl,ot,strike_name,trade_qty,ltxn))
                in_trade=False
            td=0; ld=cd
            # Update lot size for this date
            lot_size = get_lot_size(instrument, cd)
            trade_qty = lots * lot_size

        if not is_daily:
            if in_trade and ct>=sqoff:
                c=float(row["close"])
                xp=_est_prem(c,ei,ep,ot,atm_prem_ref) if has_opt else c
                pnl=_opt_pnl(ep,xp,lots,lot_size,ltxn) if has_opt else _idx_pnl(ei,c,lots,lot_size)
                total_pnl+=pnl; trades.append(_mk(len(trades)+1,et,ts,ep if has_opt else ei,xp if has_opt else c,pnl,"EOD",total_pnl,ot,strike_name,trade_qty,ltxn))
                in_trade=False; td+=1
            if ct<mkt_open or ct>=mkt_close:
                equity.append({"time":str(ts)[:16],"equity":round(total_pnl,2)}); continue

        if in_trade:
            c=float(row["close"])
            cp=_est_prem(c,ei,ep,ot,atm_prem_ref) if has_opt else c
            sh=False; th=False
            if has_opt and lsl>0: sh=(ltxn=="BUY" and cp<=slp) or (ltxn=="SELL" and cp>=slp)
            elif not has_opt and sl_pct>0: sh=c<=ei*(1-sl_pct/100)
            if has_opt and ltgt>0: th=(ltxn=="BUY" and cp>=tgtp) or (ltxn=="SELL" and cp<=tgtp)

            if sh:
                xp=slp if has_opt else ei*(1-sl_pct/100)
                pnl=_opt_pnl(ep,xp,lots,lot_size,ltxn) if has_opt else _idx_pnl(ei,xp,lots,lot_size)
                total_pnl+=pnl; trades.append(_mk(len(trades)+1,et,ts,ep if has_opt else ei,xp,pnl,"StopLoss",total_pnl,ot,strike_name,trade_qty,ltxn))
                in_trade=False; td+=1
            elif th:
                xp=tgtp if has_opt else c
                pnl=_opt_pnl(ep,xp,lots,lot_size,ltxn) if has_opt else _idx_pnl(ei,c,lots,lot_size)
                total_pnl+=pnl; trades.append(_mk(len(trades)+1,et,ts,ep if has_opt else ei,xp,pnl,"Target",total_pnl,ot,strike_name,trade_qty,ltxn))
                in_trade=False; td+=1
            elif eval_condition_group(row, exit_conditions):
                pnl=_opt_pnl(ep,cp,lots,lot_size,ltxn) if has_opt else _idx_pnl(ei,c,lots,lot_size)
                total_pnl+=pnl; trades.append(_mk(len(trades)+1,et,ts,ep if has_opt else ei,cp if has_opt else c,pnl,"Signal",total_pnl,ot,strike_name,trade_qty,ltxn))
                in_trade=False; td+=1
        else:
            if td>=max_tpd: equity.append({"time":str(ts)[:16],"equity":round(total_pnl,2)}); continue
            # Only skip the very first 5min candle (09:15) — enter from 09:20
            if not is_daily and ct<ENTRY_EARLIEST: equity.append({"time":str(ts)[:16],"equity":round(total_pnl,2)}); continue

            if eval_condition_group(row, entry_conditions):
                in_trade=True; ei=float(row["close"]); et=ts
                lot_size = get_lot_size(instrument, cd)
                trade_qty = lots * lot_size
                atm = round(ei / strike_step) * strike_step
                # Determine correct instrument label
                if "26009" in str(instrument) or "BANK" in str(instrument).upper():
                    inst_label = "BANKNIFTY"
                elif "1" == str(instrument) or "SENSEX" in str(instrument).upper():
                    inst_label = "SENSEX"
                elif "26017" in str(instrument) or "FIN" in str(instrument).upper():
                    inst_label = "NIFTY FINSVC"
                elif "26037" in str(instrument) or "MIDCAP" in str(instrument).upper():
                    inst_label = "NIFTY MIDCAP"
                else:
                    inst_label = "NIFTY"
                strike_name = f"{inst_label} {int(atm)} {ot}" if has_opt else inst_label
                if has_opt:
                    # Improved ATM premium estimation using volatility-based model
                    # Base IV: 15-20% for low VIX, 25-35% for high VIX
                    spot_pct = 0.006 if "BANK" in inst_label else 0.005  # BANKNIFTY slightly higher
                    vix_factor = min(2.0, 1.0 + (df.get('vix', pd.Series([15]*len(df))).iloc[-1] - 15) / 100) if 'vix' in df.columns else 1.0
                    atm_prem = round(ei * spot_pct * vix_factor, 2)
                    
                    print(f"[BT] Entry @ {et}: Spot={ei:.2f}, ATM={atm}, ATM_Premium_Est={atm_prem}")
                    
                    # Determine entry premium based on strike selection
                    if strike_type in ("premium_near","premium_above","premium_below") and strike_value>0:
                        # Find strike whose premium is closest to target
                        target_prem = strike_value
                        
                        # Search strikes around ATM (±10 strikes)
                        best_strike = atm
                        best_diff = 999999
                        
                        for offset in range(-10, 11):
                            test_strike = atm + (offset * strike_step)
                            moneyness = (ei - test_strike) if ot == "CE" else (test_strike - ei)
                            
                            # Estimate premium for this strike (simplified Black-Scholes approximation)
                            if moneyness > 0:  # ITM
                                intrinsic = moneyness
                                extrinsic = atm_prem * 0.5 * (1 - abs(moneyness) / (ei * 0.2))
                                test_prem = max(1, intrinsic + extrinsic)
                            else:  # OTM
                                distance_pct = abs(moneyness) / ei
                                test_prem = atm_prem * max(0.05, (1 - distance_pct * 5))
                            
                            diff = abs(test_prem - target_prem)
                            if diff < best_diff:
                                best_diff = diff
                                best_strike = test_strike
                                ep = round(test_prem, 2)
                        
                        strike_used = int(best_strike)
                        strike_name = f"{inst_label} {strike_used} {ot}"
                        print(f"[BT]   Premium_Near {target_prem}: Selected {strike_used} with est premium {ep}")
                    elif strike_type == "strike_price" and strike_value>0:
                        strike_used = int(round(strike_value / strike_step) * strike_step)
                        moneyness = (ei - strike_used) if ot == "CE" else (strike_used - ei)
                        
                        if moneyness > 0:  # ITM
                            intrinsic = moneyness
                            extrinsic = atm_prem * 0.5 * (1 - abs(moneyness) / (ei * 0.2))
                            ep = max(1, round(intrinsic + extrinsic, 2))
                        else:  # OTM
                            distance_pct = abs(moneyness) / ei
                            ep = max(1, round(atm_prem * max(0.05, (1 - distance_pct * 5)), 2))
                        
                        strike_name = f"{inst_label} {int(strike_used)} {ot}"
                        print(f"[BT]   Strike_Price: {strike_used}, Est Premium={ep}")
                    elif strike_type in ("otm","itm") and strike_value>0:
                        offset = int(round(strike_value / strike_step) * strike_step)
                        if strike_type=="otm":
                            strike_used = atm + offset if ot=="CE" else atm - offset
                            distance_pct = offset / ei
                            ep = max(1, round(atm_prem * max(0.05, (1 - distance_pct * 5)), 2))
                        else:
                            strike_used = atm - offset if ot=="CE" else atm + offset
                            intrinsic = offset
                            extrinsic = atm_prem * 0.5
                            ep = max(1, round(intrinsic + extrinsic, 2))
                        strike_name = f"{inst_label} {int(strike_used)} {ot}"
                        print(f"[BT]   {strike_type.upper()}: {strike_used}, Est Premium={ep}")
                    elif strike_type == "spot_price" and strike_value != 0:
                        # Spot ± Offset: offset from current spot price
                        offset = int(round(strike_value / strike_step) * strike_step)
                        strike_used = int(ei + offset) if strike_value > 0 else int(ei - abs(offset))
                        strike_used = int(round(strike_used / strike_step) * strike_step)  # round to nearest strike
                        
                        moneyness = (ei - strike_used) if ot == "CE" else (strike_used - ei)
                        if moneyness > 0:  # ITM
                            intrinsic = moneyness
                            extrinsic = atm_prem * 0.5 * (1 - abs(moneyness) / (ei * 0.2))
                            ep = max(1, round(intrinsic + extrinsic, 2))
                        else:  # OTM
                            distance_pct = abs(moneyness) / ei
                            ep = max(1, round(atm_prem * max(0.05, (1 - distance_pct * 5)), 2))
                        
                        strike_name = f"{inst_label} {int(strike_used)} {ot}"
                        print(f"[BT]   Spot±Offset: {strike_used}, Est Premium={ep}")
                    else:
                        # Default ATM
                        strike_used = atm
                        ep = atm_prem
                        strike_name = f"{inst_label} {int(strike_used)} {ot}"
                        print(f"[BT]   ATM: {strike_used}, Est Premium={ep}")
                    slp=round(ep*(1-lsl/100),2) if lsl>0 else 0
                    tgtp=round(ep*(1+ltgt/100),2) if ltgt>0 else 0
                    atm_prem_ref=atm_prem  # store for delta estimation during trade
                else:
                    ep=ei; slp=ei*(1-sl_pct/100); tgtp=0

        equity.append({"time":str(ts)[:16],"equity":round(total_pnl,2)})

    if not trades:
        return {"status":"no_trades","message":"No trades generated.","trades":[],"equity":equity[-500:],"stats":{},"monthly":[],"day_of_week":[],"yearly":[]}

    pnls=[t["pnl"] for t in trades]; ws=[p for p in pnls if p>0]; ls=[p for p in pnls if p<=0]
    run=[t["cumulative"] for t in trades]
    # Max drawdown from cumulative P&L
    pk=run[0]; mdd=0.0; mddv=0.0
    dd_days=0; max_dd_days=0; in_dd=False; dd_start_idx=0
    for i,v in enumerate(run):
        pk=max(pk,v)
        ddv=pk-v; mddv=max(mddv,ddv)
        if pk>0: mdd=max(mdd, ddv/pk*100)
        # Track drawdown days
        if ddv > 0:
            if not in_dd:
                in_dd = True
                dd_start_idx = i
            dd_days = i - dd_start_idx + 1
            max_dd_days = max(max_dd_days, dd_days)
        else:
            in_dd = False
            dd_days = 0

    wst=0;lst=0;cw=0;cl=0
    for p in pnls:
        if p>0: cw+=1;cl=0;wst=max(wst,cw)
        else: cl+=1;cw=0;lst=max(lst,cl)

    stats={"total_trades":len(trades),"winning_trades":len(ws),"losing_trades":len(ls),
        "win_rate":round(len(ws)/len(pnls)*100,2),"total_pnl":round(sum(pnls),2),
        "avg_profit":round(float(np.mean(ws)) if ws else 0,2),"avg_loss":round(float(np.mean(ls)) if ls else 0,2),
        "max_drawdown":round(mdd,2),"max_drawdown_val":round(mddv,2),"max_drawdown_days":max_dd_days,
        "roi_pct":0,
        "profit_factor":round(sum(ws)/abs(sum(ls)) if ls and abs(sum(ls))>0 else 999.0,2),
        "max_profit":round(max(pnls),2),"max_loss":round(min(pnls),2),
        "win_streak":wst,"loss_streak":lst,
        "risk_per_trade":round(float(np.std(pnls)),2) if len(pnls)>1 else 0}

    monthly={}
    for t in trades: k=str(t["entry_time"])[:7]; monthly[k]=monthly.get(k,0)+t["pnl"]
    dmap={0:"Monday",1:"Tuesday",2:"Wednesday",3:"Thursday",4:"Friday",5:"Saturday",6:"Sunday"}
    dd={}
    for t in trades:
        try: dn=dmap[datetime.strptime(str(t["entry_time"])[:10],"%Y-%m-%d").weekday()]
        except: dn="Unknown"
        if dn not in dd: dd[dn]={"hits":0,"miss":0,"profit":0,"loss":0}
        if t["pnl"]>0: dd[dn]["hits"]+=1; dd[dn]["profit"]+=t["pnl"]
        else: dd[dn]["miss"]+=1; dd[dn]["loss"]+=t["pnl"]
    yd={}
    for t in trades:
        yr=str(t["entry_time"])[:4]
        if yr not in yd: yd[yr]={"hits":0,"miss":0,"profit":0,"loss":0}
        if t["pnl"]>0: yd[yr]["hits"]+=1; yd[yr]["profit"]+=t["pnl"]
        else: yd[yr]["miss"]+=1; yd[yr]["loss"]+=t["pnl"]
    step=max(1,len(equity)//800)
    return {"status":"success","trades":trades,"equity":equity[::step],"stats":stats,
        "monthly":[{"month":k,"pnl":round(v,2)} for k,v in sorted(monthly.items())],
        "day_of_week":[{"day":k,**v} for k,v in dd.items()],
        "yearly":[{"year":k,**v} for k,v in sorted(yd.items())]}
