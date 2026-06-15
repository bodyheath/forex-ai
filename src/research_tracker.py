"""Research trade tracker — paper trades for all pairs scoring confidence >= 5.

Tracks at 0.01 lots for reference only (not real money). Outcome checking runs
automatically like real trades: WIN when target hit, LOSS when stop hit, EXPIRED
after 14 days.

Source values:
  'sonnet'     — Sonnet-confirmed setup; entry/stop/target from Claude's analysis.
  'indicative' — Haiku-scored pair (conf 5-6) with ATR-derived price levels
                 computed by _calc_indicative_levels. Fully trackable for outcomes.
  'haiku'      — Legacy: Haiku-only with no price levels at all (NO_PRICE_LEVELS).
                 No longer written by current code but may exist in historical data.

Used by research_analyst.py for the 30-day threshold study: were conf-6 pairs
profitable enough to warrant lowering the live-trade threshold from 7 to 6?
"""

import csv
from datetime import datetime

import config

RESEARCH_TRADES_CSV = config.DATA_DIR / "research_trades.csv"
RESEARCH_LOTS = 0.01

FIELDS = [
    "id", "date", "pair", "direction", "confidence",
    "entry", "stop_loss", "target", "lots",
    "status", "close_price", "pips", "net_pips", "r_multiple", "closed_at",
    "source", "scan_mode",
]

OUTCOME_STATUSES = {"WIN", "LOSS", "BREAKEVEN", "EXPIRED"}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load() -> list:
    if not RESEARCH_TRADES_CSV.exists():
        return []
    with RESEARCH_TRADES_CSV.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _write_all(rows: list) -> None:
    with RESEARCH_TRADES_CSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in FIELDS})


def _next_id(rows: list) -> int:
    ids = [int(r["id"]) for r in rows if str(r.get("id", "")).isdigit()]
    return (max(ids) + 1) if ids else 1


def _pip_size(pair: str) -> float:
    quote = pair.split("/")[-1].upper() if "/" in pair else pair[-3:].upper()
    return 0.01 if quote == "JPY" else 0.0001


def _to_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def log_research_trade(pair: str, parsed: dict, source: str, scan_mode: str) -> int:
    """Append one research trade; return its assigned id.

    source: 'sonnet'     — Sonnet-confirmed, entry/stop/target from Claude.
            'indicative' — Haiku-scored with ATR-derived levels; status = OPEN.
            'haiku'      — Legacy haiku-only with no levels; status = NO_PRICE_LEVELS.
    """
    rows  = load()
    rec_id = _next_id(rows)

    entry = parsed.get("entry")
    stop  = parsed.get("stop_loss")
    tgt   = parsed.get("target")

    has_levels = all(_to_float(v) is not None for v in (entry, stop, tgt))
    status = "OPEN" if has_levels else "NO_PRICE_LEVELS"

    rows.append({
        "id":          rec_id,
        "date":        _now()[:10],
        "pair":        pair,
        "direction":   (parsed.get("direction") or "").upper(),
        "confidence":  parsed.get("confidence") or "",
        "entry":       entry or "",
        "stop_loss":   stop or "",
        "target":      tgt or "",
        "lots":        RESEARCH_LOTS,
        "status":      status,
        "close_price": "",
        "pips":        "",
        "net_pips":    "",
        "r_multiple":  "",
        "closed_at":   "",
        "source":      source,
        "scan_mode":   scan_mode,
    })
    _write_all(rows)
    return rec_id


def _compute_result(row: dict, status: str, close_price) -> tuple:
    """Return (r_multiple, pips) for a closed research trade."""
    entry     = _to_float(row.get("entry"))
    stop      = _to_float(row.get("stop_loss"))
    target    = _to_float(row.get("target"))
    direction = (row.get("direction") or "").upper()

    if status == "BREAKEVEN":
        return 0.0, 0.0
    if status == "EXPIRED" and _to_float(close_price) is None:
        return "", ""

    close_price = _to_float(close_price)
    if close_price is None:
        close_price = target if status == "WIN" else stop
    if entry is None or stop is None or close_price is None or direction not in ("BUY", "SELL"):
        return "", ""

    risk = abs(entry - stop)
    if risk == 0:
        return "", ""
    profit    = (close_price - entry) if direction == "BUY" else (entry - close_price)
    r_multiple = round(profit / risk, 2)
    pips       = round(profit / _pip_size(row.get("pair", "")), 1)
    return r_multiple, pips


def update_outcome(rec_id: int, status: str, close_price=None) -> dict:
    status = status.upper()
    if status not in OUTCOME_STATUSES:
        raise ValueError(f"status must be one of {sorted(OUTCOME_STATUSES)}")

    rows = load()
    target = next((r for r in rows if str(r.get("id")) == str(rec_id)), None)
    if target is None:
        raise ValueError(f"no research trade with id {rec_id}")

    r_mult, pips = _compute_result(target, status, close_price)

    net_pips = pips
    if pips not in ("", None):
        try:
            from src import trade_costs as _tc
            lots = _to_float(target.get("lots")) or RESEARCH_LOTS
            opened = datetime.strptime(target.get("date", "")[:10], "%Y-%m-%d")
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

    target["status"]      = status
    target["close_price"] = close_price if close_price is not None else ""
    target["r_multiple"]  = r_mult
    target["pips"]        = pips
    target["net_pips"]    = net_pips
    target["closed_at"]   = _now()
    _write_all(rows)
    return target
