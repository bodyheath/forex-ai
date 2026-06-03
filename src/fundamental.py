"""Fundamental layer: central-bank policy rates and the interest-rate
differential between the two currencies in the pair."""

import config
from src import fred


def _rate_for(ccy: str) -> dict:
    meta = config.CURRENCIES.get(ccy, {})
    series = meta.get("rate_fred")
    if not series:
        return {"currency": ccy, "status": "UNAVAILABLE", "reason": "no series mapped"}
    value, date = fred.latest(series)
    if value is None:
        return {
            "currency": ccy,
            "central_bank": meta.get("cb", "?"),
            "status": "UNAVAILABLE",
            "reason": f"FRED series {series} returned no data (verify the id)",
        }
    return {
        "currency": ccy,
        "central_bank": meta.get("cb", "?"),
        "policy_rate_pct": round(value, 3),
        "as_of": date,
        "rate_trend": fred.trend(series) or "unknown",
        "fred_series": series,
        "status": "ok",
    }


def analyse(base: str, quote: str) -> dict:
    base_rate = _rate_for(base)
    quote_rate = _rate_for(quote)

    result = {"base": base_rate, "quote": quote_rate, "status": "ok"}

    if base_rate["status"] == "ok" and quote_rate["status"] == "ok":
        diff = base_rate["policy_rate_pct"] - quote_rate["policy_rate_pct"]
        result["rate_differential_pct"] = round(diff, 3)
        result["carry_note"] = (
            f"Holding {base} earns {diff:+.2f}% carry vs {quote} per year (positive "
            f"favours long {base}{quote}, negative favours short)."
        )
    else:
        result["status"] = "PARTIAL"
        result["rate_differential_pct"] = None
        result["carry_note"] = "Rate differential unavailable - one or both policy rates missing."
    return result
