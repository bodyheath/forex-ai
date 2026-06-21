"""Per-pair MFE statistics for adaptive cascade target learning.

Reads historical research_trades.csv to compute per-pair MFE distributions,
then derives optimal T1/T2/T3 multipliers from the 25th/50th/75th percentiles.

Results stored in data/pair_statistics.json (append-only — existing EV fields
written by other modules are preserved).
"""
import json

import config

_PAIR_STATS_FILE = config.DATA_DIR / "pair_statistics.json"

T1_MIN, T1_MAX = 0.3, 0.6
T2_MIN, T2_MAX = 0.5, 1.0
T3_MIN, T3_MAX = 0.7, 1.5
MIN_TRADES = 10

T1_STD = 0.4
T2_STD = 0.7
T3_STD = 1.0


def _pip_size(pair: str) -> float:
    cleaned = (pair or "").upper().replace("/", "").replace("-", "")
    if len(cleaned) >= 6 and cleaned[3:6] == "JPY":
        return 0.01
    if "JPY" in (pair or "").upper():
        return 0.01
    return 0.0001


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def mfe_atr_multiple(row: dict):
    """Return MFE as ATR multiple for one trade row, or None if data missing."""
    mfe = _to_float(row.get("mfe_pips"))
    if mfe is None or mfe <= 0:
        return None
    entry = _to_float(row.get("entry"))
    stop  = _to_float(row.get("stop_loss"))
    if entry is None or stop is None:
        return None
    atr = abs(entry - stop)
    if atr <= 0:
        return None
    pip = _pip_size(row.get("pair", ""))
    atr_pips = atr / pip
    if atr_pips <= 0:
        return None
    return mfe / atr_pips


def _percentile(data: list, pct: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    n = len(s)
    idx = pct / 100 * (n - 1)
    lo  = int(idx)
    hi  = min(lo + 1, n - 1)
    return s[lo] + (idx - lo) * (s[hi] - s[lo])


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _adaptive_mults(p25: float, p50: float, p75: float) -> dict:
    return {
        "t1_mult": round(_clamp(p25 * 0.80, T1_MIN, T1_MAX), 3),
        "t2_mult": round(_clamp(p50 * 0.80, T2_MIN, T2_MAX), 3),
        "t3_mult": round(_clamp(p75 * 0.80, T3_MIN, T3_MAX), 3),
    }


def _compute_pair_stats(trades: list):
    """Compute MFE stats for trades from the same pair. Returns None if insufficient."""
    mfe_multiples = [m for t in trades if (m := mfe_atr_multiple(t)) is not None]
    if len(mfe_multiples) < MIN_TRADES:
        return None, len(mfe_multiples)
    n   = len(mfe_multiples)
    avg = sum(mfe_multiples) / n
    p25 = _percentile(mfe_multiples, 25)
    p50 = _percentile(mfe_multiples, 50)
    p75 = _percentile(mfe_multiples, 75)
    mults = _adaptive_mults(p25, p50, p75)
    stats = {
        "n_trades_with_mfe": n,
        "average_mfe_atr":   round(avg, 3),
        "mfe_percentile_25": round(p25, 3),
        "mfe_percentile_50": round(p50, 3),
        "mfe_percentile_75": round(p75, 3),
        **mults,
        "adaptive_active": True,
    }
    return stats, n


def load() -> dict:
    """Load pair_statistics.json; return empty dict if missing or corrupt."""
    if not _PAIR_STATS_FILE.exists():
        return {}
    try:
        return json.loads(_PAIR_STATS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(stats: dict) -> None:
    _PAIR_STATS_FILE.write_text(json.dumps(stats, indent=2), encoding="utf-8")


def update(research_trades: list = None) -> dict:
    """Recalculate per-pair MFE stats and merge into pair_statistics.json (append-only).

    Returns dict mapping pair -> new_stats (only pairs that were updated/activated).
    """
    if research_trades is None:
        try:
            from src import research_tracker as _rt
            research_trades = _rt.load()
        except Exception:
            return {}

    CLOSED = {"WIN", "LOSS", "PARTIAL_WIN", "FULL_WIN", "EXPIRED", "BREAKEVEN"}
    by_pair: dict = {}
    for row in research_trades:
        if (row.get("status") or "").upper() not in CLOSED:
            continue
        pair = (row.get("pair") or "").upper()
        if not pair:
            continue
        by_pair.setdefault(pair, []).append(row)

    existing = load()
    new_stats: dict = {}

    for pair, trades in by_pair.items():
        stats, n_mfe = _compute_pair_stats(trades)
        entry = existing.get(pair, {})

        # Record how many MFE samples we have (even below threshold)
        entry["n_trades_with_mfe"] = n_mfe

        if stats:
            # Preserve previous multipliers for change detection in Monday report
            if entry.get("adaptive_active"):
                entry["_prev_t1_mult"] = entry.get("t1_mult", T1_STD)
                entry["_prev_t3_mult"] = entry.get("t3_mult", T3_STD)
            # Merge — append only (preserves any existing EV fields)
            for k, v in stats.items():
                entry[k] = v
            new_stats[pair] = stats

        existing[pair] = entry

    _save(existing)
    return new_stats


def get_adaptive_targets(pair: str, stats: dict = None):
    """Return adaptive target multipliers for a pair, or None if insufficient data.

    Pass `stats` (already-loaded dict) to avoid re-reading the file on every call.
    """
    if stats is None:
        stats = load()
    entry = stats.get((pair or "").upper(), {})
    if not entry.get("adaptive_active"):
        return None
    if entry.get("n_trades_with_mfe", 0) < MIN_TRADES:
        return None
    return {
        "t1_mult":  entry.get("t1_mult", T1_STD),
        "t2_mult":  entry.get("t2_mult", T2_STD),
        "t3_mult":  entry.get("t3_mult", T3_STD),
        "n_trades": entry.get("n_trades_with_mfe", 0),
        "p25":      entry.get("mfe_percentile_25"),
        "p50":      entry.get("mfe_percentile_50"),
        "p75":      entry.get("mfe_percentile_75"),
    }


def count_needed(pair: str, stats: dict = None) -> int:
    """How many more closed trades needed before adaptive activates."""
    if stats is None:
        stats = load()
    entry = stats.get((pair or "").upper(), {})
    return max(0, MIN_TRADES - entry.get("n_trades_with_mfe", 0))


def build_monday_report_lines() -> list:
    """Build adaptive target section for the Monday learning report."""
    all_stats = load()
    active = {p: s for p, s in all_stats.items()
              if not p.startswith("_") and s.get("adaptive_active")}

    if not active:
        return []

    lines = ["", "<b>9. ADAPTIVE TARGET SYSTEM</b>"]
    lines.append(f"Pairs with adaptive targets active: {len(active)}")

    # Newly activated = was not adaptive before (no _prev_t1_mult means first run)
    newly = [p for p, s in active.items() if "_prev_t1_mult" not in s]
    if newly:
        lines.append(f"Newly activated this week: {len(newly)} — {', '.join(sorted(newly))}")

    # Notable deviations from standard
    notable_t1 = [(p, s["t1_mult"], s.get("_prev_t1_mult"), s["n_trades_with_mfe"])
                  for p, s in active.items()
                  if abs(s.get("t1_mult", T1_STD) - T1_STD) >= 0.05]
    notable_t3 = [(p, s["t3_mult"], s.get("_prev_t3_mult"), s["n_trades_with_mfe"])
                  for p, s in active.items()
                  if abs(s.get("t3_mult", T3_STD) - T3_STD) >= 0.1]

    if notable_t1 or notable_t3:
        lines.append("")
        lines.append("Notable changes from standard:")
        for p, t1, prev_t1, n in sorted(notable_t1, key=lambda x: -abs(x[1] - T1_STD))[:3]:
            if prev_t1 is not None:
                lines.append(f"{p} T1: {prev_t1:.2f}x ->{t1:.2f}x ATR ({n} trades)")
            else:
                lines.append(f"{p} T1: {T1_STD:.2f}x ->{t1:.2f}x ATR ({n} trades)")
        for p, t3, prev_t3, n in sorted(notable_t3, key=lambda x: -abs(x[1] - T3_STD))[:3]:
            if prev_t3 is not None:
                lines.append(f"{p} T3: {prev_t3:.2f}x ->{t3:.2f}x ATR ({n} trades)")
            else:
                lines.append(f"{p} T3: {T3_STD:.2f}x ->{t3:.2f}x ATR ({n} trades)")

    # Retroactive analysis (one-time)
    retro = _retroactive_analysis(all_stats)
    if retro:
        lines.extend(retro)

    return lines


def _retroactive_analysis(all_stats: dict = None) -> list:
    """Compare adaptive vs standard T1 hit rates on closed historical trades.

    Runs once; result stored in pair_statistics.json via _retroactive_done flag.
    """
    if all_stats is None:
        all_stats = load()
    if all_stats.get("_retroactive_done"):
        return []

    try:
        from src import research_tracker as _rt
        trades = _rt.load()
    except Exception:
        return []

    CLOSED = {"WIN", "LOSS", "PARTIAL_WIN", "FULL_WIN", "EXPIRED"}
    active_pairs = {p for p, s in all_stats.items()
                    if not p.startswith("_") and s.get("adaptive_active")}
    if not active_pairs:
        return []

    std_hits = adp_hits = analysed = 0
    for row in trades:
        if (row.get("status") or "").upper() not in CLOSED:
            continue
        pair = (row.get("pair") or "").upper()
        if pair not in active_pairs:
            continue
        mfe_m = mfe_atr_multiple(row)
        if mfe_m is None:
            continue
        if mfe_m >= T1_STD:
            std_hits += 1
        t1_m = all_stats[pair].get("t1_mult", T1_STD)
        if mfe_m >= t1_m:
            adp_hits += 1
        analysed += 1

    if analysed == 0:
        return []

    std_pct = int(round(std_hits / analysed * 100))
    adp_pct = int(round(adp_hits / analysed * 100))
    diff    = adp_pct - std_pct

    all_stats["_retroactive_done"] = True
    _save(all_stats)

    sign = "+" if diff >= 0 else ""
    return [
        "",
        "📊 <b>RETROACTIVE ADAPTIVE TARGET ANALYSIS</b>",
        f"Closed trades analysed: {analysed}",
        f"Pairs with sufficient history: {len(active_pairs)}",
        f"T1 hit rate with adaptive: {adp_pct}%",
        f"T1 hit rate with standard: {std_pct}%",
        f"Estimated improvement: {sign}{diff} percentage points",
        f"Adaptive targets active for: {', '.join(sorted(active_pairs))}",
    ]
