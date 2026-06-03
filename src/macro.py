"""Macro-context layer: gold, oil, bond yields and the VIX, used by the model to
judge risk-on vs risk-off and cross-asset correlation signals."""

import config
from src import fred


def analyse() -> dict:
    readings = {}
    for label, series in config.MACRO_SERIES.items():
        value, date = fred.latest(series)
        if value is None:
            readings[label] = {"status": "UNAVAILABLE", "series": series}
        else:
            readings[label] = {
                "value": round(value, 3),
                "as_of": date,
                "trend": fred.trend(series) or "unknown",
            }

    # Yield-curve spread as a quick recession / risk signal, if both legs exist.
    ten = readings.get("US 10Y Treasury yield (%)", {}).get("value")
    two = readings.get("US 2Y Treasury yield (%)", {}).get("value")
    if isinstance(ten, (int, float)) and isinstance(two, (int, float)):
        spread = round(ten - two, 3)
        readings["US 2s10s curve (10Y-2Y, %)"] = {
            "value": spread,
            "note": "inverted (<0) historically signals risk-off / recession odds",
        }

    return {"status": "ok", "signals": readings}
