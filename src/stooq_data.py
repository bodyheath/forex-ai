"""Stooq free forex daily candle backup source.

No API key or account required. Unlimited free tier.
Uses pandas_datareader with data_source='stooq'.

Stooq accepts slash-format forex pairs directly (AUD/JPY, EUR/USD, etc.).
"""
from datetime import datetime, timedelta


def fetch_candles(pair: str, n_candles: int, log=print) -> dict | None:
    """Fetch OHLCV daily candles from Stooq for pair.

    Returns dict in Twelve Data time_series format on success, None on any failure.
    Never raises — always returns None gracefully on errors.
    """
    try:
        from pandas_datareader import data as pdr
    except ImportError:
        log("[Stooq] pandas-datareader not installed — Stooq fallback unavailable")
        return None

    try:
        end_dt   = datetime.utcnow()
        days_req = max(int(n_candles * 1.6) + 30, 365)
        start_dt = end_dt - timedelta(days=days_req)

        df = pdr.DataReader(
            pair,
            data_source="stooq",
            start=start_dt.strftime("%Y-%m-%d"),
            end=end_dt.strftime("%Y-%m-%d"),
        )
    except Exception as exc:
        log(f"[Stooq] DataReader failed for {pair}: {exc}")
        return None

    if df is None or df.empty:
        log(f"[Stooq] No data returned for {pair}")
        return None

    df.columns = [str(c).lower() for c in df.columns]

    for col in ("open", "high", "low", "close"):
        if col not in df.columns:
            log(f"[Stooq] Missing column '{col}' for {pair}")
            return None

    df = df[["open", "high", "low", "close"]].dropna()

    if df.empty:
        log(f"[Stooq] All candles NaN for {pair}")
        return None

    # Stooq returns newest-first — reverse to chronological order if needed
    if len(df) > 1 and df.index[0] > df.index[1]:
        df = df.iloc[::-1]

    df = df.tail(n_candles)

    # Strip timezone for uniform downstream handling
    if hasattr(df.index, "tz") and df.index.tz is not None:
        df.index = df.index.tz_localize(None)

    # Build values newest-first (Twelve Data format)
    values = []
    for dt, row in df.iloc[::-1].iterrows():
        values.append({
            "datetime": dt.strftime("%Y-%m-%d"),
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
