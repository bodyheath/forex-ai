"""Trade outcome tracker.

Every analysis is appended as a row to data/trades.csv (a spreadsheet you can open
in Excel/Google Sheets). Actionable calls (TRADE_THIS: YES) start life as OPEN;
non-actionable ones are logged as NO_TRADE for the record and for learning stats.
Outcomes are recorded later with `update_outcome`, which computes the realised
R-multiple and pips from entry/stop/exit.
"""

import csv
from datetime import datetime

import config

FIELDS = [
    "id", "timestamp", "pair", "direction",
    "confidence", "technical", "fundamental", "sentiment", "positioning", "macro",
    "entry", "target", "stop_loss", "reward_risk", "trade_this", "data_sources",
    "status", "exit_price", "r_multiple", "pips", "net_pips", "closed_at", "notes",
    "report_file", "key_thesis", "best_entry_time",
]

# status values: NO_TRADE | OPEN | WIN | LOSS | BREAKEVEN | SKIPPED | EXPIRED
OUTCOME_STATUSES = {"WIN", "LOSS", "BREAKEVEN", "SKIPPED", "EXPIRED"}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load() -> list:
    if not config.TRADES_CSV.exists():
        return []
    with config.TRADES_CSV.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _write_all(rows: list) -> None:
    with config.TRADES_CSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in FIELDS})


def _next_id(rows: list) -> int:
    ids = [int(r["id"]) for r in rows if str(r.get("id", "")).isdigit()]
    return (max(ids) + 1) if ids else 1


def _pip_size(pair: str) -> float:
    """0.01 for JPY-quoted pairs, else 0.0001."""
    quote = pair.split("/")[-1].upper() if "/" in pair else pair[-3:].upper()
    return 0.01 if quote == "JPY" else 0.0001


def _save_report_file(rec_id: int, pair: str, report: str) -> str:
    safe = pair.replace("/", "")
    path = config.REPORTS_DIR / f"{rec_id:04d}_{safe}.txt"
    path.write_text(report, encoding="utf-8")
    return path.name


def log_recommendation(pair: str, parsed: dict, data_sources, report: str) -> int:
    """Append one recommendation; return its assigned id."""
    rows = load()
    rec_id = _next_id(rows)
    report_file = _save_report_file(rec_id, pair, report)
    status = "OPEN" if parsed.get("trade_this") == "YES" else "NO_TRADE"

    rows.append({
        "id": rec_id,
        "timestamp": _now(),
        "pair": pair,
        "direction": parsed.get("direction"),
        "confidence": parsed.get("confidence"),
        "technical": parsed.get("technical_score"),
        "fundamental": parsed.get("fundamental_score"),
        "sentiment": parsed.get("sentiment_score"),
        "positioning": parsed.get("positioning_score"),
        "macro": parsed.get("macro_score"),
        "entry": parsed.get("entry"),
        "target": parsed.get("target"),
        "stop_loss": parsed.get("stop_loss"),
        "reward_risk": parsed.get("reward_risk"),
        "trade_this": parsed.get("trade_this"),
        "data_sources": data_sources,
        "status": status,
        "exit_price": "", "r_multiple": "", "pips": "", "net_pips": "",
        "closed_at": "", "notes": "",
        "report_file": report_file,
        "key_thesis": parsed.get("key_thesis") or "",
        "best_entry_time": parsed.get("best_entry_time") or "",
    })
    _write_all(rows)
    return rec_id


def _to_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _compute_result(row: dict, status: str, exit_price):
    """Return (r_multiple, pips) from entry/stop/exit and direction.

    If no exit price is given, assume the planned outcome: WIN -> target hit,
    LOSS -> stop hit. BREAKEVEN/SKIPPED/EXPIRED -> 0 / no result.
    """
    entry = _to_float(row.get("entry"))
    stop = _to_float(row.get("stop_loss"))
    target = _to_float(row.get("target"))
    direction = (row.get("direction") or "").upper()

    if status == "SKIPPED":
        return "", ""
    if status == "EXPIRED" and _to_float(exit_price) is None:
        return "", ""
    if status == "BREAKEVEN":
        return 0.0, 0.0

    exit_price = _to_float(exit_price)
    if exit_price is None:
        exit_price = target if status == "WIN" else stop
    if entry is None or stop is None or exit_price is None or direction not in ("BUY", "SELL"):
        return "", ""

    risk = abs(entry - stop)
    if risk == 0:
        return "", ""
    profit = (exit_price - entry) if direction == "BUY" else (entry - exit_price)
    r_multiple = round(profit / risk, 2)
    pips = round(profit / _pip_size(row.get("pair", "")), 1)
    return r_multiple, pips


def update_outcome(rec_id: int, status: str, exit_price=None, notes: str = "") -> dict:
    status = status.upper()
    if status not in OUTCOME_STATUSES:
        raise ValueError(f"status must be one of {sorted(OUTCOME_STATUSES)}")

    rows = load()
    target = next((r for r in rows if str(r.get("id")) == str(rec_id)), None)
    if target is None:
        raise ValueError(f"no recommendation with id {rec_id}")

    r_mult, pips = _compute_result(target, status, exit_price)

    net_pips = pips
    if pips not in ("", None):
        try:
            from src import trade_costs as _tc
            opened = datetime.strptime(target.get("timestamp", "")[:19], "%Y-%m-%d %H:%M:%S")
            days_held = max(0.0, (datetime.now() - opened).total_seconds() / 86400)
            net_pips = _tc.net_pips_for_closed_trade(
                target.get("pair", ""),
                target.get("direction", ""),
                _to_float(target.get("entry")) or 1.0,
                float(pips),
                days_held,
            )
        except Exception:
            pass

    target["status"] = status
    target["exit_price"] = exit_price if exit_price is not None else ""
    target["r_multiple"] = r_mult
    target["pips"] = pips
    target["net_pips"] = net_pips
    target["closed_at"] = _now()
    if notes:
        target["notes"] = notes
    _write_all(rows)
    return target
