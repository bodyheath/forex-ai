"""Stooq free forex daily candle backup source.

No API key or account required. Unlimited free tier.
Uses Stooq's direct CSV download URL via requests — no pandas-datareader needed.

Symbol format: AUD/JPY -> audjpy (lowercase, slash removed).

Note: Stooq may return a JavaScript challenge page for residential/VPN IPs.
GitHub Actions datacenter IPs typically bypass this challenge.
The function always returns None gracefully if Stooq is unavailable.
"""
import io
from datetime import datetime, timedelta

import requests

_STOOQ_URL = "https://stooq.com/q/d/l/"
_TIMEOUT   = 20


def _pair_to_stooq_symbol(pair: str) -> str:
    """Convert 'AUD/JPY' to Stooq symbol 'audjpy'."""
    return pair.replace("/", "").replace(" ", "").lower()


def fetch_candles(pair: str, n_candles: int, log=print) -> dict | None:
    """Fetch OHLCV daily candles from Stooq for pair.

    Returns dict in Twelve Data time_series format on success, None on any failure.
    Never raises — always returns None gracefully on errors.
    """
    try:
        import pandas as pd
    except ImportError:
        log("[Stooq] pandas not installed — Stooq fallback unavailable")
        return None

    symbol   = _pair_to_stooq_symbol(pair)
    end_dt   = datetime.utcnow()
    days_req = max(int(n_candles * 1.6) + 30, 365)
    start_dt = end_dt - timedelta(days=days_req)

    try:
        resp = requests.get(
            _STOOQ_URL,
            params={
                "s":  symbol,
                "d1": start_dt.strftime("%Y%m%d"),
                "d2": end_dt.strftime("%Y%m%d"),
                "i":  "d",
            },
            headers={"User-Agent": "Mozilla/5.0 (forex-ai stooq)"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
    except Exception as exc:
        log(f"[Stooq] HTTP request failed for {pair} ({symbol}): {exc}")
        return None

    content = resp.text.strip()
    if not content or len(content) < 20:
        log(f"[Stooq] No data returned for {pair} ({symbol})")
        return None
    if content.lstrip().startswith("<") or "javascript" in content[:200].lower():
        log(f"[Stooq] Bot protection detected for {pair} — datacenter IPs bypass this challenge")
        return None
    if "No data found" in content:
        log(f"[Stooq] No data found for {pair} ({symbol})")
        return None

    try:
        df = pd.read_csv(io.StringIO(content))
    except Exception as exc:
        log(f"[Stooq] CSV parse failed for {pair}: {exc}")
        return None

    df.columns = [str(c).strip().lower() for c in df.columns]

    for col in ("open", "high", "low", "close"):
        if col not in df.columns:
            log(f"[Stooq] Missing column '{col}' for {pair} — columns: {list(df.columns)}")
            return None

    date_col = next((c for c in df.columns if "date" in c), None)
    if date_col is None:
        log(f"[Stooq] No date column found for {pair} — columns: {list(df.columns)}")
        return None

    df = df[[date_col, "open", "high", "low", "close"]].dropna()

    if df.empty:
        log(f"[Stooq] All candles NaN for {pair}")
        return None

    # Parse date and sort chronologically (Stooq returns newest-first)
    try:
        df[date_col] = pd.to_datetime(df[date_col])
    except Exception as exc:
        log(f"[Stooq] Date parse failed for {pair}: {exc}")
        return None

    df = df.sort_values(date_col).tail(n_candles)

    # Build values list newest-first (Twelve Data format)
    values = []
    for _, row in df.iloc[::-1].iterrows():
        values.append({
            "datetime": row[date_col].strftime("%Y-%m-%d"),
            "open":     str(round(float(row["open"]),  6)),
            "high":     str(round(float(row["high"]),  6)),
            "low":      str(round(float(row["low"]),   6)),
            "close":    str(round(float(row["close"]), 6)),
        })

    return {
        "meta":   {"symbol": pair, "source": "Stooq"},
        "values": values,
        "status": "ok",
    }
