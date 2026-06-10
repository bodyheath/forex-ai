"""API cost tracker — daily and monthly running totals for scan cost display.

Persists to data/api_costs.json. Resets daily total at Auckland midnight,
monthly total on the 1st of each Auckland month.
NZD conversion uses a fixed rate of 1 USD = 1.68 NZD.
"""

import json
from datetime import datetime

import config

COST_FILE  = config.DATA_DIR / "api_costs.json"
USD_TO_NZD = 1.68


def _load() -> dict:
    try:
        return json.loads(COST_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(data: dict) -> None:
    COST_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def record_and_get_lines(scan_usd: float, now_ak: datetime) -> list:
    """Record scan_usd in daily/monthly totals and return 3 formatted Telegram lines.

    Resets daily total when the Auckland date changes, monthly total when the
    Auckland year-month changes.
    """
    today_str = now_ak.strftime("%Y-%m-%d")
    month_str = now_ak.strftime("%Y-%m")

    data = _load()

    # Daily total — reset if date changed
    daily = data.get("daily", {})
    if daily.get("date") != today_str:
        daily = {"date": today_str, "total_usd": 0.0, "scan_count": 0}
    daily["total_usd"]  = round(float(daily.get("total_usd", 0.0)) + scan_usd, 6)
    daily["scan_count"] = int(daily.get("scan_count", 0)) + 1

    # Monthly total — reset if month changed
    monthly = data.get("monthly", {})
    if monthly.get("month") != month_str:
        monthly = {"month": month_str, "total_usd": 0.0}
    monthly["total_usd"] = round(float(monthly.get("total_usd", 0.0)) + scan_usd, 6)

    data["daily"]   = daily
    data["monthly"] = monthly
    _save(data)

    daily_usd   = daily["total_usd"]
    daily_count = daily["scan_count"]
    month_usd   = monthly["total_usd"]

    scan_nzd  = round(scan_usd  * USD_TO_NZD, 3)
    daily_nzd = round(daily_usd * USD_TO_NZD, 3)
    month_nzd = round(month_usd * USD_TO_NZD, 3)

    scans_word = "scan" if daily_count == 1 else "scans"

    return [
        f"💰 This scan cost: ${scan_usd:.3f} USD (~${scan_nzd:.3f} NZD)",
        f"📊 Today so far: ${daily_usd:.3f} USD across {daily_count} {scans_word} (~${daily_nzd:.3f} NZD)",
        f"📅 This month: ${month_usd:.3f} USD (~${month_nzd:.3f} NZD)",
    ]
