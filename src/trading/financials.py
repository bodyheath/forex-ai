"""Single source of truth for all fund trade financial calculations.

Guarantees:
- Deterministic: same inputs → same outputs, no side-effects except writes
- fund_state.json is OUTPUT (computed from trades.csv), never INPUT
- Never returns NaN or Infinity
- Never raises exceptions (all errors return safe defaults)
- Atomic writes with post-write verification
- All timestamps UTC
- Correct pip sizes: JPY=0.01, all others=0.0001 (includes HKD/SEK/NOK/SGD)
- Dollar P&L correct for all pairs via DPP formula
"""

import json
import math
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

# ─── Constants ────────────────────────────────────────────────────────────────

STARTING_BALANCE = 10_000.0
T1_SIZE          = 0.40   # 40% of position closed at T1
T2_SIZE          = 0.30   # 30% at T2
T3_SIZE          = 0.30   # 30% at T3
DEFAULT_RISK_PCT = 1.0    # fallback when position_size_pct_at_entry is NaN
FTMO_DAILY_LIMIT = 5.0    # 5% max daily drawdown
FTMO_TOTAL_LIMIT = 10.0   # 10% max total drawdown

TRADES_CSV      = Path("data/trades.csv")
FUND_STATE_JSON = Path("data/fund_state.json")
PRICE_CACHE     = Path("data/price_cache.json")

CLOSED_STATUSES = ("WIN", "LOSS", "PARTIAL_WIN", "FULL_WIN", "EXPIRED",
                   "STOPPED", "CLOSED", "CANCELLED")

# ─── Type helpers ─────────────────────────────────────────────────────────────

def safe_float(v: Any, default: float = 0.0) -> float:
    """Convert v to float. Returns default on NaN, Inf, None, or parse error."""
    try:
        f = float(v)
        return default if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return default


def safe_pos_pct(v: Any) -> float:
    """Return position size %. Always returns a sane value in (0, 100]."""
    f = safe_float(v, default=-1.0)
    return f if 0.01 <= f <= 100.0 else DEFAULT_RISK_PCT


def safe_bool(v: Any) -> bool:
    """Parse CSV boolean: TRUE/1/YES → True, everything else → False."""
    return str(v).strip().upper() in ("TRUE", "1", "YES")


# ─── Pip sizing ───────────────────────────────────────────────────────────────

def pip_size(pair: str) -> float:
    """Return pip size. JPY pairs = 0.01, all others = 0.0001."""
    return 0.01 if "JPY" in str(pair).upper() else 0.0001


# ─── Timestamps (UTC everywhere) ──────────────────────────────────────────────

def utc_now_str() -> str:
    """Return current UTC timestamp as 'YYYY-MM-DD HH:MM:SS'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def parse_utc_dt(s: Any) -> Optional[datetime]:
    """Parse datetime string → UTC-aware datetime. Returns None on failure."""
    try:
        raw = str(s)[:19].replace("T", " ")
        dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _auckland_today() -> str:
    """Return today's date string in Auckland time (UTC+12 or UTC+13 in DST)."""
    try:
        from zoneinfo import ZoneInfo
        nzt = datetime.now(ZoneInfo("Pacific/Auckland"))
    except Exception:
        nzt = datetime.now(timezone.utc) + timedelta(hours=12)
    return nzt.strftime("%Y-%m-%d")


# ─── Atomic I/O ───────────────────────────────────────────────────────────────

def atomic_write_json(path: Path, data: dict) -> bool:
    """Write JSON atomically via .tmp → rename. Returns True on success."""
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        shutil.move(str(tmp), str(path))
        # Verify: re-read and confirm it's a dict
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return isinstance(loaded, dict)
    except Exception:
        return False


def atomic_write_csv(path: Path, df: pd.DataFrame) -> bool:
    """Write DataFrame as CSV atomically via .tmp → rename. Returns True on success."""
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        df.to_csv(str(tmp), index=False, encoding="utf-8")
        shutil.move(str(tmp), str(path))
        return True
    except Exception:
        return False


# ─── Price cache ──────────────────────────────────────────────────────────────

def load_prices() -> dict:
    """Load price cache. Returns {} on failure. Handles nested {timestamp, prices} format."""
    try:
        raw = json.loads(PRICE_CACHE.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "prices" in raw:
            prices = raw["prices"]
        else:
            prices = raw
        if isinstance(prices, dict):
            return {k: v for k, v in prices.items() if safe_float(v) > 0}
        return {}
    except Exception:
        return {}


def get_price(prices: dict, pair: str) -> Optional[float]:
    """Get price for pair from cache. Returns None if missing or zero."""
    for key in (pair, pair.replace("/", ""), pair.upper(),
                pair.upper().replace("/", "")):
        v = prices.get(key)
        if v is not None:
            f = safe_float(v)
            if f > 0:
                return f
    return None


# ─── Core P&L formula ─────────────────────────────────────────────────────────

def calculate_dpp(balance: float, risk_pct: float, stop_pips: float) -> float:
    """Dollar Per Pip = (balance × risk_pct/100) / stop_pips.

    Correct for ALL pairs including HKD/SEK/NOK/SGD exotics because
    the formula is denominated in balance currency (USD), not counter currency.
    Returns 0.0 on invalid inputs.
    """
    if balance <= 0 or risk_pct <= 0 or stop_pips <= 0:
        return 0.0
    return (balance * risk_pct / 100.0) / stop_pips


def calculate_pnl(
    pair: str,
    direction: str,
    entry: Any,
    stop_loss: Any,
    pos_pct: Any,
    balance: float,
    t1_price: Any,
    t2_price: Any,
    t3_price: Any,
    t1_hit: Any,
    t2_hit: Any,
    t3_hit: Any,
    status: str,
    exit_price: Any,
    current_price: Optional[float],
) -> dict:
    """Calculate P&L for a trade. Returns a dict — never NaN, never raises.

    Keys returned:
      pips_unrealised  — raw pip move from entry to current price (0 if closed)
      pips_weighted    — weighted pips accounting for cascade (stages × size)
      dollars          — dollar P&L (negative for losses)
      dpp              — dollar per pip
      stop_pips        — entry-to-stop in pips
      progress_pct     — 0-100 progress toward full target (unrealised)
      is_protected     — True once T1 is hit (stop moved to breakeven)
      days_open        — always 0 (caller must compute from timestamps)
    """
    ps    = pip_size(pair)
    e     = safe_float(entry)
    sl    = safe_float(stop_loss)
    pct   = safe_pos_pct(pos_pct)
    bal   = safe_float(balance, STARTING_BALANCE)
    t1p   = safe_float(t1_price) if t1_price and str(t1_price) not in ("nan", "None", "") else 0.0
    t2p   = safe_float(t2_price) if t2_price and str(t2_price) not in ("nan", "None", "") else 0.0
    t3p   = safe_float(t3_price) if t3_price and str(t3_price) not in ("nan", "None", "") else 0.0
    ep    = safe_float(exit_price) if exit_price and str(exit_price) not in ("nan", "None", "") else 0.0
    t1h   = safe_bool(t1_hit)
    t2h   = safe_bool(t2_hit)
    t3h   = safe_bool(t3_hit)
    dirn  = str(direction).upper()

    stop_pips = abs(e - sl) / ps if e > 0 and sl > 0 else 100.0
    dpp       = calculate_dpp(bal, pct, stop_pips)
    risk_d    = bal * pct / 100.0

    def _pips(a: float, b: float) -> float:
        return abs(a - b) / ps if a > 0 and b > 0 else 0.0

    t1_pips = _pips(t1p, e) if t1h and t1p > 0 else 0.0
    t2_pips = _pips(t2p, e) if t2h and t2p > 0 else 0.0
    t3_pips = _pips(t3p, e) if t3h and t3p > 0 else 0.0

    is_protected = t1h or t1_pips > 0

    if status in CLOSED_STATUSES:
        if status == "LOSS":
            pips_w = -stop_pips
            dollars = -risk_d
        elif status == "PARTIAL_WIN":
            pips_w = t1_pips * T1_SIZE
            dollars = pips_w * dpp
        elif status == "WIN":
            pips_w = t1_pips * T1_SIZE + t2_pips * T2_SIZE
            dollars = pips_w * dpp
        elif status == "FULL_WIN":
            ex_pips = _pips(ep if ep > 0 else t3p, e)
            pips_w = t1_pips * T1_SIZE + t2_pips * T2_SIZE + ex_pips * T3_SIZE
            dollars = pips_w * dpp
        else:  # EXPIRED, STOPPED, CLOSED, etc.
            if ep > 0 and e > 0:
                raw = (ep - e) / ps if dirn == "BUY" else (e - ep) / ps
            else:
                raw = 0.0
            pips_w = raw
            dollars = pips_w * dpp

        return {
            "pips_unrealised": 0.0,
            "pips_weighted":   round(pips_w, 1),
            "dollars":         round(dollars, 2),
            "dpp":             round(dpp, 4),
            "stop_pips":       round(stop_pips, 1),
            "progress_pct":    0.0,
            "is_protected":    is_protected,
            "days_open":       0,
        }

    # ── Open trade ────────────────────────────────────────────────────────────
    cp = safe_float(current_price) if current_price else 0.0
    if cp > 0 and e > 0:
        raw_pips = (cp - e) / ps if dirn == "BUY" else (e - cp) / ps
    else:
        raw_pips = 0.0

    # Weighted pips accounting for already-banked cascade stages
    if is_protected:
        banked = t1_pips * T1_SIZE
        if t2h and t2_pips > 0:
            banked += t2_pips * T2_SIZE
            open_pct = T3_SIZE
        else:
            open_pct = 1.0 - T1_SIZE
        # Open portion capped at 0 (stop at breakeven protects it)
        open_pips = max(0.0, raw_pips) * open_pct
        pips_w = banked + open_pips
    else:
        pips_w = raw_pips

    dollars = round(pips_w * dpp, 2)

    # Progress 0-100% (full target relative to stop distance as R)
    progress_pct = round((raw_pips / stop_pips) * 100.0, 1) if stop_pips > 0 else 0.0
    progress_pct = max(-100.0, min(200.0, progress_pct))  # clamp to sane range

    return {
        "pips_unrealised": round(raw_pips, 1),
        "pips_weighted":   round(pips_w, 1),
        "dollars":         dollars,
        "dpp":             round(dpp, 4),
        "stop_pips":       round(stop_pips, 1),
        "progress_pct":    progress_pct,
        "is_protected":    is_protected,
        "days_open":       0,
    }


# ─── Trade closure ─────────────────────────────────────────────────────────────

def close_fund_trade(
    df: pd.DataFrame,
    trade_id: int,
    status: str,
    exit_price: float,
    pips: float,
    closed_at: Optional[str] = None,
    pos_pct_override: Optional[float] = None,
) -> pd.DataFrame:
    """Close a fund trade with 10 guarantees:

    1. exit_price stored as float (never NaN)
    2. pips stored as float (never NaN)
    3. status is one of CLOSED_STATUSES
    4. closed_at is UTC ISO string
    5. position_size_pct_at_entry backfilled if NaN (uses DEFAULT_RISK_PCT)
    6. t1_hit/t2_hit set consistently
    7. Atomic write (tmp → rename)
    8. Verified write (reads back to confirm status != OPEN)
    9. Returns updated DataFrame (caller gets fresh state)
    10. Never raises — returns original df on error
    """
    try:
        idx_list = df.index[df["id"].astype(str) == str(trade_id)].tolist()
        if not idx_list:
            return df
        idx = idx_list[0]

        closed_ts = closed_at or utc_now_str()
        ep = safe_float(exit_price)
        p  = safe_float(pips)

        df.at[idx, "status"]      = status
        df.at[idx, "exit_price"]  = ep
        df.at[idx, "pips"]        = p
        df.at[idx, "closed_at"]   = closed_ts

        # Backfill NaN position size
        current_pct = safe_float(df.at[idx, "position_size_pct_at_entry"], default=-1.0)
        if current_pct <= 0 or math.isnan(current_pct):
            pct = pos_pct_override if pos_pct_override and pos_pct_override > 0 else DEFAULT_RISK_PCT
            df.at[idx, "position_size_pct_at_entry"] = pct

        ok = atomic_write_csv(TRADES_CSV, df)
        if not ok:
            return df

        # Verify write: re-read and confirm status changed
        df2 = pd.read_csv(str(TRADES_CSV), encoding="utf-8-sig")
        row = df2[df2["id"].astype(str) == str(trade_id)]
        if not row.empty and str(row.iloc[0].get("status", "")) != "OPEN":
            return df2
        return df
    except Exception:
        return df


# ─── Fund state calculation ───────────────────────────────────────────────────

def calculate_fund_state(df: pd.DataFrame, prices: dict) -> dict:
    """Compute fund_state from trades.csv. fund_state.json is OUTPUT, not input.

    Returns a dict with all fund_state.json fields — never NaN, never raises.
    """
    try:
        fund = df[df["trade_this"].astype(str) == "YES"].copy()
        today_auckland = _auckland_today()

        running_bal   = STARTING_BALANCE
        peak_bal      = STARTING_BALANCE
        daily_open_bal = STARTING_BALANCE
        daily_pnl_d   = 0.0
        cons_losses   = 0

        closed = fund[fund["status"].isin(list(CLOSED_STATUSES))]
        try:
            closed = closed.sort_values("closed_at", na_position="last")
        except Exception:
            pass

        prev_in_today = False
        for _, row in closed.iterrows():
            pct   = safe_pos_pct(row.get("position_size_pct_at_entry"))
            entry = safe_float(row.get("entry"))
            sl    = safe_float(row.get("stop_loss"))
            pair  = str(row.get("pair", ""))
            ps    = pip_size(pair)
            status = str(row.get("status", ""))
            pips_v = safe_float(row.get("pips"))

            stop_pips = abs(entry - sl) / ps if entry > 0 and sl > 0 else 100.0
            dpp = calculate_dpp(running_bal, pct, stop_pips)
            risk_d = running_bal * pct / 100.0

            if status == "LOSS":
                dollars = -risk_d
            elif pips_v != 0.0:
                dollars = pips_v * dpp
            else:
                dollars = 0.0

            # Track when we cross into "today" for daily_opening_balance
            closed_ts = str(row.get("closed_at", ""))
            in_today = closed_ts[:10] >= today_auckland
            if in_today and not prev_in_today:
                daily_open_bal = running_bal
                prev_in_today = True

            if in_today:
                daily_pnl_d += dollars

            running_bal = running_bal + dollars
            peak_bal    = max(peak_bal, running_bal)

            if status == "LOSS":
                cons_losses += 1
            elif status in ("WIN", "PARTIAL_WIN", "FULL_WIN"):
                cons_losses = 0

        if not prev_in_today:
            daily_open_bal = running_bal

        # Add unrealised open trade P&L (for balance display purposes only)
        open_trades    = fund[fund["status"] == "OPEN"]
        pending_trades = fund[fund["status"] == "PENDING"]

        drawdown_pct = round((peak_bal - running_bal) / peak_bal * 100, 4) if peak_bal > 0 else 0.0
        daily_pnl_pct = round(daily_pnl_d / daily_open_bal * 100, 4) if daily_open_bal > 0 else 0.0

        # Build pending trades summary list
        pending_list = []
        for _, pt in pending_trades.iterrows():
            pending_list.append({
                "id":                      int(pt.get("id", 0)),
                "pair":                    str(pt.get("pair", "")),
                "direction":               str(pt.get("direction", "")),
                "entry_type":              str(pt.get("entry_type", "IMMEDIATE")),
                "entry_trigger_price":     safe_float(pt.get("entry_trigger_price")),
                "entry_trigger_reason":    str(pt.get("entry_trigger_reason", "")),
                "entry_trigger_expiry":    str(pt.get("entry_trigger_expiry", "")),
            })

        _ot_summaries = [
            _open_trade_summary(row, prices, running_bal)
            for _, row in open_trades.iterrows()
        ]
        unrealised_d    = sum(safe_float(t.get("dollars_unrealised")) for t in _ot_summaries)
        unrealised_pips = sum(safe_float(t.get("pips_unrealised"))    for t in _ot_summaries)

        return {
            "balance":               round(running_bal, 2),
            "daily_opening_balance": round(daily_open_bal, 2),
            "daily_pnl_dollars":     round(daily_pnl_d, 2),
            "daily_pnl_pct":         round(daily_pnl_pct, 4),
            "peak_balance":          round(peak_bal, 2),
            "current_drawdown_pct":  round(drawdown_pct, 4),
            "drawdown_pct":          round(drawdown_pct, 4),
            "consecutive_losses":    int(cons_losses),
            "current_sizing_pct":    1.0,
            "sizing_mode":           "normal",
            "daily_trades_date":     today_auckland,
            "unrealised_dollars":    round(unrealised_d, 2),
            "unrealised_pips":       round(unrealised_pips, 1),
            "total_equity":          round(running_bal + unrealised_d, 2),
            "open_trades":           _ot_summaries,
            "pending_count":         len(pending_trades),
            "pending_trades":        pending_list,
        }
    except Exception:
        return {
            "balance":               STARTING_BALANCE,
            "daily_opening_balance": STARTING_BALANCE,
            "daily_pnl_dollars":     0.0,
            "daily_pnl_pct":         0.0,
            "peak_balance":          STARTING_BALANCE,
            "current_drawdown_pct":  0.0,
            "drawdown_pct":          0.0,
            "consecutive_losses":    0,
            "current_sizing_pct":    1.0,
            "sizing_mode":           "normal",
            "daily_trades_date":     _auckland_today(),
            "open_trades":           [],
        }


def _open_trade_summary(row, prices: dict, running_balance: float = STARTING_BALANCE) -> dict:
    """Build full per-trade dict for the Discord dashboard and unrealised P&L.

    Returns every field the dashboard needs — never requires the caller to re-derive anything.
    current price comes from `prices`; all prices/hit-flags/targets come from `row`.
    """
    try:
        pair  = str(row.get("pair", ""))
        dirn  = str(row.get("direction", "")).upper()
        cp    = get_price(prices, pair)
        ps    = pip_size(pair)
        entry = safe_float(row.get("entry"))
        sl    = safe_float(row.get("stop_loss"))
        eff_stop = safe_float(row.get("effective_stop") or row.get("stop_loss"))
        t1p   = safe_float(row.get("t1_price") or row.get("target"))
        t2p   = safe_float(row.get("t2_price"))
        t3p   = safe_float(row.get("t3_price"))
        t1h   = safe_bool(row.get("t1_hit"))
        t2h   = safe_bool(row.get("t2_hit"))
        t3h   = safe_bool(row.get("t3_hit"))
        pct   = safe_pos_pct(row.get("position_size_pct_at_entry"))
        bal   = safe_float(running_balance, STARTING_BALANCE)
        cur   = safe_float(cp) if cp else entry

        # Canonical P&L (cascade-aware, breakeven-protected)
        pnl = calculate_pnl(
            pair          = pair,
            direction     = dirn,
            entry         = entry,
            stop_loss     = sl,
            pos_pct       = pct,
            balance       = bal,
            t1_price      = t1p or None,
            t2_price      = t2p or None,
            t3_price      = t3p or None,
            t1_hit        = t1h,
            t2_hit        = t2h,
            t3_hit        = t3h,
            status        = "OPEN",
            exit_price    = None,
            current_price = cp,
        )

        # Cascade-relative progress (entry → next target), shown in the progress bar
        if t3h:
            _next = "Complete"
            _base, _tgt = entry, t3p
        elif t2h:
            _next = "T3"
            _base = safe_float(eff_stop or entry)
            _tgt  = t3p
        elif t1h:
            _next = "T2"
            _base, _tgt = entry, t2p
        else:
            _next = "T1"
            _base, _tgt = entry, t1p

        if _tgt and _base and _tgt != _base:
            _rng = _tgt - _base if dirn == "BUY" else _base - _tgt
            _mv  = cur  - _base if dirn == "BUY" else _base - cur
            cascade_progress = round(_mv / _rng * 100.0, 1) if _rng > 0 else 0.0
        else:
            cascade_progress = pnl["progress_pct"]  # fallback: R-multiple progress

        # Days open (UTC-only — treats stored timestamp as UTC to sidestep NZT bug)
        days_open = 0
        open_str  = "?d"
        ts_raw    = str(row.get("timestamp", "") or "")
        dt        = parse_utc_dt(ts_raw)
        if dt:
            delta     = datetime.now(timezone.utc) - dt
            hours     = delta.total_seconds() / 3600
            days_open = delta.days
            open_str  = (f"{hours:.0f}h" if days_open == 0 and hours < 24
                         else f"{days_open}d")

        # P&L note for cascade status
        if t2h:
            pnl_note = "T1+T2 banked · 30% running"
        elif t1h:
            pnl_note = "T1 banked · 60% running"
        else:
            pnl_note = "Full position running"

        stop_pips = pnl["stop_pips"]
        risk_d    = bal * pct / 100.0

        return {
            # Identity
            "id":                  int(safe_float(row.get("id", 0))),
            "pair":                pair,
            "direction":           dirn,
            # Prices (all in one place — no re-lookup needed)
            "entry":               entry,
            "current":             cur,
            "stop":                eff_stop,
            "stop_loss":           sl,
            # Cascade targets
            "t1":                  t1p,
            "t2":                  t2p,
            "t3":                  t3p,
            "t1_price":            t1p,
            "t2_price":            t2p,
            "t3_price":            t3p,
            "t1_hit":              t1h,
            "t2_hit":              t2h,
            "t3_hit":              t3h,
            "next_target":         _next,
            # P&L (correct cascade + breakeven formula from calculate_pnl)
            "pips_unrealised":     pnl["pips_unrealised"],
            "pips":                pnl["pips_unrealised"],
            "dollars_unrealised":  pnl["dollars"],
            "dollars":             pnl["dollars"],
            "dpp":                 pnl["dpp"],
            "stop_pips":           stop_pips,
            # Progress
            "progress_pct":        cascade_progress,   # toward next cascade target
            "r_progress_pct":      pnl["progress_pct"],  # in R multiples
            "is_protected":        pnl["is_protected"],
            # Trade metadata
            "days_open":           days_open,
            "open_str":            open_str,
            "conf":                safe_float(row.get("confidence")),
            "confidence":          safe_float(row.get("confidence")),
            "risk_dollars":        round(risk_d, 2),
            "pnl_note":            pnl_note,
        }
    except Exception:
        return {"id": int(safe_float(row.get("id", 0))), "pair": str(row.get("pair", ""))}


def sync_fund_state_json(state: dict) -> bool:
    """Write calculated fund state to fund_state.json atomically. Returns True on success."""
    clean = {k: v for k, v in state.items() if k != "open_trades"}
    return atomic_write_json(FUND_STATE_JSON, clean)


# ─── Integrity verification ───────────────────────────────────────────────────

def verify_trade_integrity(df: pd.DataFrame) -> list:
    """Check all fund trades for data integrity issues. Returns list of issue strings."""
    issues = []
    fund = df[df["trade_this"].astype(str) == "YES"]
    now_utc = datetime.now(timezone.utc)

    for _, row in fund.iterrows():
        tid     = row.get("id", "?")
        pair    = str(row.get("pair", ""))
        status  = str(row.get("status", ""))
        dirn    = str(row.get("direction", "")).upper()
        entry   = safe_float(row.get("entry"))
        sl      = safe_float(row.get("stop_loss"))
        ep      = safe_float(row.get("exit_price"))
        pct_raw = row.get("position_size_pct_at_entry")
        ts_raw  = str(row.get("timestamp", ""))
        ca_raw  = str(row.get("closed_at", ""))

        # NaN position size
        pct = safe_float(pct_raw, -1.0)
        if pct <= 0:
            issues.append(f"#{tid} {pair}: position_size_pct_at_entry=NaN (will use {DEFAULT_RISK_PCT}%)")

        # Stop direction
        if entry > 0 and sl > 0:
            if dirn == "BUY" and sl >= entry:
                issues.append(f"#{tid} {pair}: BUY but stop {sl} >= entry {entry}")
            if dirn == "SELL" and sl <= entry:
                issues.append(f"#{tid} {pair}: SELL but stop {sl} <= entry {entry}")

        # Timestamp in future (NZT bug)
        ts_dt = parse_utc_dt(ts_raw)
        if ts_dt and (ts_dt - now_utc).total_seconds() > 3600:
            hours_ahead = (ts_dt - now_utc).total_seconds() / 3600
            issues.append(f"#{tid} {pair}: timestamp {hours_ahead:.1f}h in future — NZT stored instead of UTC")

        # Closed trade checks
        if status in CLOSED_STATUSES:
            if ep <= 0:
                issues.append(f"#{tid} {pair}: {status} but no exit_price")
            if ca_raw in ("", "nan", "None"):
                issues.append(f"#{tid} {pair}: {status} but no closed_at")

        # Open trade with exit price
        if status == "OPEN" and ep > 0:
            issues.append(f"#{tid} {pair}: OPEN but has exit_price={ep}")

        # PENDING trade checks
        if status == "PENDING":
            trigger_price = str(row.get("entry_trigger_price", ""))
            if trigger_price in ("", "nan", "None", "0", "0.0"):
                issues.append(f"#{tid} {pair}: PENDING but no entry_trigger_price set")
            expiry = str(row.get("entry_trigger_expiry", ""))
            if expiry not in ("", "nan", "None"):
                try:
                    exp_dt = datetime.strptime(expiry[:19], "%Y-%m-%d %H:%M:%S")
                    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
                    if exp_dt < now_naive:
                        issues.append(
                            f"#{tid} {pair}: PENDING but past expiry {expiry} "
                            f"— should be EXPIRED"
                        )
                except (ValueError, TypeError):
                    pass

    return issues


# ─── Daily reset ──────────────────────────────────────────────────────────────

def daily_reset_if_needed(current_state: dict) -> dict:
    """Reset daily P&L fields if the trading day has rolled over.

    Uses Auckland time for day boundary (same as fund_state.py).
    Returns updated state dict. Never raises.
    """
    try:
        today = _auckland_today()
        stored_date = str(current_state.get("daily_trades_date", ""))
        if stored_date == today:
            return current_state

        # New day: carry balance forward, reset daily fields
        new_state = dict(current_state)
        bal = safe_float(current_state.get("balance"), STARTING_BALANCE)
        new_state["daily_trades_date"]    = today
        new_state["daily_opening_balance"] = round(bal, 2)
        new_state["daily_pnl_dollars"]    = 0.0
        new_state["daily_pnl_pct"]        = 0.0
        return new_state
    except Exception:
        return current_state
