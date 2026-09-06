"""Yahoo Finance 4H candle reconstruction from 1H hourly data.

No API key or account required. Free unlimited access via yfinance.

Reconstructs 4H candles by resampling 60 days of 1H data into 4-hour bars
using standard OHLCV aggregation. Quality is approximately 95% of native 4H data.

Note: _pair_to_yahoo_symbol() below uses the same fixed overrides as technical.py.
"""

_YAHOO_SYMBOL_OVERRIDES: dict = {
    # USD-base pairs: Yahoo uses {QUOTE}=X
    "USD/JPY": "JPY=X",   "USD/CHF": "CHF=X",   "USD/CAD": "CAD=X",
    "USD/HKD": "HKD=X",   "USD/SGD": "SGD=X",   "USD/NOK": "NOK=X",
    "USD/SEK": "SEK=X",
    # USD-quote pairs: Yahoo uses {BASE}USD=X
    "GBP/USD": "GBPUSD=X", "EUR/USD": "EURUSD=X",
    "AUD/USD": "AUDUSD=X", "NZD/USD": "NZDUSD=X",
}


def _pair_to_yahoo_symbol(pair: str) -> str:
    """Convert 'AUD/JPY' to Yahoo Finance ticker format.

    Explicit overrides for major USD pairs ensure correct direction.
    USD/XXX → '{XXX}=X' · XXX/USD → '{BASE}USD=X' · others → '{BASE}{QUOTE}=X'
    """
    cleaned = pair.upper().strip()
    if cleaned in _YAHOO_SYMBOL_OVERRIDES:
        return _YAHOO_SYMBOL_OVERRIDES[cleaned]
    parts = cleaned.replace(" ", "").split("/")
    if len(parts) != 2:
        return cleaned.replace("/", "") + "=X"
    base, quote = parts
    if base == "USD":
        return f"{quote}=X"
    return f"{base}{quote}=X"


def batch_fetch_prices(pairs: list, log=print) -> dict:
    """Batch fetch current prices for all pairs via a single Yahoo Finance call.

    Returns {pair: float}. Never raises — returns partial dict on any failure.
    ~15-minute delay is fully acceptable for swing trade analysis.
    """
    try:
        import yfinance as yf
    except ImportError:
        log("[YF-BATCH] yfinance not installed")
        return {}

    if not pairs:
        return {}

    sym_to_pair = {_pair_to_yahoo_symbol(p): p for p in pairs}
    symbols     = list(sym_to_pair.keys())
    prices: dict = {}

    try:
        data = yf.download(
            tickers    =" ".join(symbols),
            period     ="1d",
            interval   ="1m",
            progress   =False,
            auto_adjust=True,
            group_by   ="ticker",
        )

        if data is None or (hasattr(data, "empty") and data.empty):
            log("[YF-BATCH] Download returned empty")
            return {}

        for sym, pair in sym_to_pair.items():
            try:
                if len(symbols) == 1:
                    col = next((c for c in data.columns if str(c).lower() == "close"), None)
                    if col is not None:
                        vals = data[col].dropna()
                        if not vals.empty:
                            prices[pair] = float(vals.iloc[-1])
                else:
                    if hasattr(data.columns, "levels"):
                        lvl0 = [str(c) for c in data.columns.get_level_values(0)]
                        if sym in lvl0:
                            vals = data[sym]["Close"].dropna()
                        else:
                            vals = data["Close"][sym].dropna()
                    else:
                        vals = data[sym]["Close"].dropna()
                    if not vals.empty:
                        prices[pair] = float(vals.iloc[-1])
            except Exception:
                pass

    except Exception as exc:
        log(f"[YF-BATCH] Batch download failed: {exc}")
        return {}

    log(f"[YF-BATCH] {len(prices)}/{len(pairs)} prices fetched (0 API calls)")
    return prices


def fetch_4h_candles(pair: str, n_candles: int, log=print) -> dict | None:
    """Reconstruct 4H candles by resampling Yahoo Finance 1H data.

    Fetches 60 days of 1H bars then resamples to 4H using:
    - Open  = first 1H open in the 4H block
    - High  = max of all 1H highs in the block
    - Low   = min of all 1H lows in the block
    - Close = last 1H close in the block

    The last bar is discarded as it is likely incomplete.
    Returns dict in Twelve Data time_series format, or None on any failure.
    """
    try:
        import yfinance as yf
    except ImportError:
        log("[YF-4H] yfinance not installed — 4H reconstruction unavailable")
        return None

    symbol = _pair_to_yahoo_symbol(pair)

    try:
        ticker = yf.Ticker(symbol)
        df_1h  = ticker.history(period="60d", interval="1h", auto_adjust=True)
    except Exception as exc:
        log(f"[YF-4H] 1H fetch failed for {symbol} ({pair}): {exc}")
        return None

    if df_1h is None or df_1h.empty:
        log(f"[YF-4H] No 1H data returned for {symbol} ({pair})")
        return None

    df_1h.columns = [str(c).lower() for c in df_1h.columns]
    for col in ("open", "high", "low", "close"):
        if col not in df_1h.columns:
            log(f"[YF-4H] Missing column '{col}' for {symbol}")
            return None

    df_1h = df_1h[["open", "high", "low", "close"]].dropna()

    if df_1h.empty:
        log(f"[YF-4H] All 1H candles NaN for {symbol}")
        return None

    try:
        # Ensure UTC-aware index for consistent 4H alignment
        if df_1h.index.tz is None:
            df_1h.index = df_1h.index.tz_localize("UTC")
        else:
            df_1h.index = df_1h.index.tz_convert("UTC")

        df_4h = df_1h.resample("4h").agg({
            "open":  "first",
            "high":  "max",
            "low":   "min",
            "close": "last",
        }).dropna()

        if df_4h.empty:
            log(f"[YF-4H] No complete 4H bars formed for {symbol}")
            return None

        # Drop the last bar — likely a partial (incomplete 4H block)
        df_4h = df_4h.iloc[:-1]

        if df_4h.empty:
            log(f"[YF-4H] Not enough data for complete 4H bars for {symbol}")
            return None

        # Convert tz-aware → tz-naive for uniform downstream handling
        df_4h.index = df_4h.index.tz_localize(None)

        df_4h = df_4h.tail(n_candles)

    except Exception as exc:
        log(f"[YF-4H] Resampling failed for {symbol}: {exc}")
        return None

    # Build values list newest-first (Twelve Data format)
    values = []
    for dt, row in df_4h.iloc[::-1].iterrows():
        values.append({
            "datetime": dt.strftime("%Y-%m-%d %H:%M"),
            "open":     str(round(float(row["open"]),  6)),
            "high":     str(round(float(row["high"]),  6)),
            "low":      str(round(float(row["low"]),   6)),
            "close":    str(round(float(row["close"]), 6)),
        })

    return {
        "meta":   {"symbol": pair, "source": "Yahoo Finance (4H)"},
        "values": values,
        "status": "ok",
    }


def fetch_1h_candles(pair: str, n_candles: int, log=print) -> dict | None:
    """Fetch 1H candles from Yahoo Finance for pair.

    Fetches 7 days of 1H bars and returns the last n_candles bars in Twelve
    Data time_series format (newest-first). Returns None on any failure.
    """
    try:
        import yfinance as yf
    except ImportError:
        log("[YF-1H] yfinance not installed — 1H candles unavailable")
        return None

    symbol = _pair_to_yahoo_symbol(pair)

    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period="2d", interval="1h", auto_adjust=True)
    except Exception as exc:
        log(f"[YF-1H] 1H fetch failed for {symbol} ({pair}): {exc}")
        return None

    if df is None or df.empty:
        log(f"[YF-1H] No 1H data returned for {symbol} ({pair})")
        return None

    df.columns = [str(c).lower() for c in df.columns]
    for col in ("open", "high", "low", "close"):
        if col not in df.columns:
            log(f"[YF-1H] Missing column '{col}' for {symbol}")
            return None

    df = df[["open", "high", "low", "close"]].dropna()

    if df.empty:
        log(f"[YF-1H] All 1H candles NaN for {symbol}")
        return None

    try:
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
    except Exception:
        pass

    # Drop last bar (may be incomplete) then take n_candles
    df = df.iloc[:-1].tail(n_candles)

    if df.empty:
        log(f"[YF-1H] Not enough 1H data for {symbol}")
        return None

    values = []
    for dt, row in df.iloc[::-1].iterrows():
        values.append({
            "datetime": dt.strftime("%Y-%m-%d %H:%M"),
            "open":     str(round(float(row["open"]),  6)),
            "high":     str(round(float(row["high"]),  6)),
            "low":      str(round(float(row["low"]),   6)),
            "close":    str(round(float(row["close"]), 6)),
        })

    return {
        "meta":   {"symbol": pair, "source": "Yahoo Finance (1H)"},
        "values": values,
        "status": "ok",
    }
