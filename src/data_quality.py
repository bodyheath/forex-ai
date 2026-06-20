"""Data quality monitoring — checks every data source after each scan.

Inspects deep_results bundles to assess per-source success rates, tracks
consecutive failures across scans, and surfaces warnings in the Telegram output.

Public API
----------
assess_scan(deep_results)      -> quality dict from bundle inspection
build_scorecard(quality)       -> list[str] display lines for system health section
update_state(quality, mode)    -> persist to data/data_quality.json, return state
load_state()                   -> load historical quality data from disk
consecutive_alerts(state)      -> list[str] immediate Telegram strings (3+ streak)
high_volume_alert(quality)     -> str | None  Telegram string if >8 candle failures
missed_opportunity_note(r)     -> str | None  note for watch list items with fallbacks
weekly_report(state)           -> list[str] Monday report lines
"""
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import config

_STATE_FILE = config.DATA_DIR / "data_quality.json"
_MAX_SCANS  = 56          # ~4 weeks at 2 scans/day


# ── Helpers ────────────────────────────────────────────────────────────────────

def _eff_conf(r: dict) -> int:
    try:
        return int(float(r.get("parsed", {}).get("confidence") or 0))
    except (TypeError, ValueError):
        return 0


def _cal_cache_age_hrs() -> float:
    """Return hours since the economic calendar was last fetched (0 = just fetched)."""
    try:
        import hashlib
        digest   = hashlib.sha256("CAL:events_7d".encode()).hexdigest()[:24]
        cal_path = config.CACHE_DIR / f"{digest}.json"
        if not cal_path.exists():
            return 999.0   # never fetched
        payload  = json.loads(cal_path.read_text(encoding="utf-8"))
        cached_at = payload.get("_cached_at", 0)
        return round((time.time() - cached_at) / 3600.0, 1)
    except Exception:
        return -1.0   # unknown


# ── Core assessment ────────────────────────────────────────────────────────────

def assess_scan(deep_results: list) -> dict:
    """Inspect every bundle in deep_results and return quality metrics.

    Returns a dict with counts per source, overall quality %, and
    a list of high-confidence pairs that had missing data.
    """
    n = len(deep_results)
    if not n:
        return {
            "n_deep": 0, "tech_ok": 0, "tech_total": 0,
            "fund_ok": 0, "fund_total": 0,
            "sent_ok": 0, "sent_total": 0,
            "pos_ok":  0, "pos_total":  0,
            "overall_pct": 100,
            "cal_cache_age_hrs": _cal_cache_age_hrs(),
            "cot_max_age_days": 0,
            "high_conf_fallback": [], "high_conf_fallback_count": 0,
        }

    tech_ok = fund_ok = sent_ok = pos_ok = 0
    high_conf_fallback = []

    # Get COT data age directly from positioning module (more accurate than parsing reason strings)
    cot_max_age = 0
    try:
        from src import positioning as _pos_dq
        cot_max_age = _pos_dq.get_cot_worst_age_days()
    except Exception:
        pass

    for r in deep_results:
        bundle = r.get("bundle") or {}
        pair   = r.get("pair", "?")
        conf   = _eff_conf(r)

        t  = bundle.get("technical")   or {}
        f  = bundle.get("fundamental") or {}
        s  = bundle.get("sentiment")   or {}
        p  = bundle.get("positioning") or {}

        t_ok = t.get("status") == "ok"
        f_ok = f.get("status") in ("ok", "PARTIAL")
        s_ok = s.get("status") in ("ok", "no recent articles")

        # Positioning is {base: {status,...}, quote: {status,...}}
        pb   = p.get("base")  or {}
        pq   = p.get("quote") or {}
        p_ok = pb.get("status") == "ok" or pq.get("status") == "ok"

        # COT staleness also captured from positioning bundle reason strings (fallback)
        if cot_max_age == 0:
            for pd in [pb, pq]:
                reason = pd.get("reason") or ""
                if "stale" in reason:
                    m = re.search(r"(\d+)\s+days?\s+old", reason)
                    if m:
                        cot_max_age = max(cot_max_age, int(m.group(1)))

        if t_ok: tech_ok += 1
        if f_ok: fund_ok += 1
        if s_ok: sent_ok += 1
        if p_ok: pos_ok  += 1

        # High-confidence pairs with missing sources
        missing = []
        if not t_ok: missing.append("technical")
        if not f_ok: missing.append("fundamental")
        if not s_ok: missing.append("sentiment")
        if not p_ok: missing.append("positioning")
        if conf >= 5 and missing:
            high_conf_fallback.append({
                "pair": pair, "conf": conf, "missing": missing
            })

    # Weighted quality score: technical 40%, others 20% each
    t_pct    = tech_ok / n
    f_pct    = fund_ok / n
    s_pct    = sent_ok / n
    p_pct    = pos_ok  / n
    overall  = round((t_pct * 0.40 + f_pct * 0.20 + s_pct * 0.20 + p_pct * 0.20) * 100)

    return {
        "n_deep":                   n,
        "tech_ok":                  tech_ok,
        "tech_total":               n,
        "fund_ok":                  fund_ok,
        "fund_total":               n,
        "sent_ok":                  sent_ok,
        "sent_total":               n,
        "pos_ok":                   pos_ok,
        "pos_total":                n,
        "overall_pct":              overall,
        "cal_cache_age_hrs":        _cal_cache_age_hrs(),
        "cot_max_age_days":         cot_max_age,
        "high_conf_fallback":       high_conf_fallback,
        "high_conf_fallback_count": len(high_conf_fallback),
    }


# ── Display ────────────────────────────────────────────────────────────────────

def build_scorecard(quality: dict) -> list:
    """Return Telegram-ready display lines for the DATA QUALITY scorecard."""
    lines = ["", "━━━━━━━━━━━━━━━━━━━━━", "📊 <b>DATA QUALITY</b>"]

    n = quality.get("n_deep", 0)
    if not n:
        lines.append("No deep analysis run — data quality report unavailable")
        return lines

    # ── Twelve Data candles ───────────────────────────────────────────────────
    t_ok   = quality.get("tech_ok",   0)
    t_tot  = quality.get("tech_total", n)
    t_fail = t_tot - t_ok
    if t_fail == 0:
        lines.append(f"✅ Twelve Data candles: {t_ok}/{t_tot} pairs fetched successfully")
    elif t_fail <= 3:
        lines.append(
            f"⚠️ Twelve Data candles: {t_ok}/{t_tot} pairs — "
            f"{t_fail} using neutral T scores — analysis quality slightly reduced"
        )
    else:
        lines.append(
            f"❌ Twelve Data candles: {t_ok}/{t_tot} pairs — "
            f"technical analysis degraded — {t_fail} pairs on neutral fallback"
        )

    # ── FRED interest rates ───────────────────────────────────────────────────
    f_ok   = quality.get("fund_ok",   0)
    f_tot  = quality.get("fund_total", n)
    f_fail = f_tot - f_ok
    if f_fail == 0:
        lines.append(f"✅ FRED interest rates: {f_ok}/{f_tot} pairs with live data")
    else:
        lines.append(
            f"⚠️ FRED interest rates: {f_ok}/{f_tot} pairs — "
            f"{f_fail} using neutral F scores"
        )

    # ── NewsAPI sentiment ─────────────────────────────────────────────────────
    s_ok   = quality.get("sent_ok",   0)
    s_tot  = quality.get("sent_total", n)
    s_fail = s_tot - s_ok
    if s_fail == 0:
        lines.append(f"✅ NewsAPI sentiment: {s_ok}/{s_tot} pairs covered")
    else:
        lines.append(
            f"⚠️ NewsAPI sentiment: {s_ok}/{s_tot} pairs — "
            f"{s_fail} using neutral sentiment"
        )

    # ── CFTC COT positioning ──────────────────────────────────────────────────
    p_ok   = quality.get("pos_ok",  0)
    p_tot  = quality.get("pos_total", n)
    p_fail = p_tot - p_ok
    cot_age = quality.get("cot_max_age_days", 0)
    if p_fail == 0 and not cot_age:
        lines.append(f"✅ CFTC COT positioning: {p_ok}/{p_tot} pairs with current data")
    elif cot_age:
        age_icon = "❌" if cot_age > 14 else "⚠️"
        lines.append(
            f"{age_icon} CFTC COT positioning: {p_ok}/{p_tot} pairs — "
            f"data is {cot_age} days old — positioning scores may be outdated"
        )
    else:
        lines.append(
            f"⚠️ CFTC COT positioning: {p_ok}/{p_tot} pairs — "
            f"{p_fail} pairs unavailable"
        )

    # ── Economic calendar freshness ───────────────────────────────────────────
    cal_age = quality.get("cal_cache_age_hrs", -1.0)
    if cal_age < 0:
        lines.append("⚠️ Economic calendar: cache status unknown")
    elif cal_age < 0.1:
        lines.append("✅ Economic calendar: live data")
    elif cal_age < 3.5:
        lines.append(f"⚠️ Economic calendar: using cached data from {cal_age:.1f} hours ago")
    elif cal_age < 999:
        lines.append(f"❌ Economic calendar: cached data {cal_age:.0f} hours old — may be stale")
    else:
        lines.append("❌ Economic calendar: no data available")

    # ── Candle degradation severity ───────────────────────────────────────────
    if t_fail > 3:
        lines.append(
            f"⚠️ Technical data degraded — {t_fail} pairs using neutral T scores "
            f"— analysis quality reduced"
        )

    # ── Overall quality threshold ──────────────────────────────────────────────
    overall = quality.get("overall_pct", 100)
    if overall < 50:
        lines.append(
            f"⚠️ Data quality below normal ({overall}%) — confidence scores may be "
            f"understated — consider waiting for next scan before acting"
        )

    return lines


def missed_opportunity_note(r: dict) -> str:
    """Return a ⚠️ annotation for a watch list item with data on fallback, or ''."""
    try:
        conf   = _eff_conf(r)
        if conf < 5:
            return ""
        bundle  = r.get("bundle") or {}
        missing = []
        if (bundle.get("technical") or {}).get("status") != "ok":
            missing.append("technical")
        f_status = (bundle.get("fundamental") or {}).get("status", "")
        if f_status not in ("ok", "PARTIAL"):
            missing.append("fundamental")
        if not missing:
            return ""
        return (
            f"⚠️ Note: {' and '.join(missing)} data unavailable for this pair — "
            f"actual confidence may be higher than {conf}/10"
        )
    except Exception:
        return ""


# ── State persistence ──────────────────────────────────────────────────────────

def load_state() -> dict:
    """Load data quality state from disk. Returns empty state on first run."""
    try:
        if _STATE_FILE.exists():
            return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"scans": [], "consecutive_fail": {}}


def update_state(quality: dict, scan_mode: str = "full") -> dict:
    """Append scan metrics and update consecutive failure counters.

    Returns the updated state (used by consecutive_alerts()).
    """
    state = load_state()

    entry = {
        "ts":                       datetime.now().isoformat(timespec="seconds"),
        "mode":                     scan_mode,
        "n_deep":                   quality.get("n_deep",   0),
        "tech_ok":                  quality.get("tech_ok",  0),
        "tech_total":               quality.get("tech_total", 0),
        "fund_ok":                  quality.get("fund_ok",  0),
        "sent_ok":                  quality.get("sent_ok",  0),
        "pos_ok":                   quality.get("pos_ok",   0),
        "overall_pct":              quality.get("overall_pct", 100),
        "high_conf_fallback_count": quality.get("high_conf_fallback_count", 0),
    }
    scans = state.get("scans", [])
    scans.append(entry)
    state["scans"] = scans[-_MAX_SCANS:]

    # Consecutive failure counters
    n  = quality.get("n_deep", 0)
    cf = state.get("consecutive_fail", {})
    for source, ok_key, tot_key in [
        ("technical",   "tech_ok", "tech_total"),
        ("fundamental", "fund_ok", "fund_total"),
        ("sentiment",   "sent_ok", "sent_total"),
        ("positioning", "pos_ok",  "pos_total"),
    ]:
        ok  = quality.get(ok_key,  n)
        tot = quality.get(tot_key, n)
        if tot > 0 and ok < tot:
            cf[source] = cf.get(source, 0) + 1
        else:
            cf[source] = 0
    state["consecutive_fail"] = cf

    try:
        _STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass
    return state


# ── Alert generators ───────────────────────────────────────────────────────────

_SOURCE_LABELS = {
    "technical":   "Twelve Data candle data",
    "fundamental": "FRED interest rate data",
    "sentiment":   "NewsAPI sentiment data",
    "positioning": "CFTC COT positioning data",
}


def consecutive_alerts(state: dict) -> list:
    """Return Telegram strings for sources with 3+ consecutive scan failures."""
    cf     = state.get("consecutive_fail", {})
    alerts = []
    for source, count in cf.items():
        if count >= 3:
            label = _SOURCE_LABELS.get(source, source)
            alerts.append(
                f"🚨 Data issue — {label} has failed for {count} consecutive scans "
                f"— check GitHub Actions logs for error details"
            )
    return alerts


def high_volume_alert(quality: dict) -> str:
    """Return Telegram string if >8 candle fetches failed, else empty string."""
    n      = quality.get("n_deep",   0)
    t_ok   = quality.get("tech_ok",  0)
    t_fail = n - t_ok
    if n > 0 and t_fail > 8:
        return (
            f"⚠️ Data quality alert — {t_fail}/{n} pairs missing candle data — "
            f"this scan may miss genuine opportunities — "
            f"check next scan before acting"
        )
    return ""


# ── Weekly report ──────────────────────────────────────────────────────────────

def weekly_report(state: dict) -> list:
    """Build weekly data quality summary for the Monday learning report."""
    scans = state.get("scans", [])
    if not scans:
        return []

    cutoff = (datetime.now() - timedelta(days=7)).isoformat(timespec="seconds")
    recent = [s for s in scans if s.get("ts", "") >= cutoff]
    if not recent:
        return []

    n_scans   = len(recent)
    avg_qual  = sum(s.get("overall_pct", 100) for s in recent) / n_scans
    degraded  = [s for s in recent if s.get("overall_pct", 100) < 80]
    tech_pcts = [
        s.get("tech_ok", 0) / max(s.get("tech_total", 1), 1) * 100
        for s in recent
    ]
    avg_tech = sum(tech_pcts) / len(tech_pcts) if tech_pcts else 100.0
    missed   = sum(s.get("high_conf_fallback_count", 0) for s in degraded)

    lines = ["", "<b>DATA QUALITY REPORT (7 DAYS)</b>"]
    lines.append(
        f"Average candle fetch success rate: {avg_tech:.0f}% across {n_scans} scans "
        f"— overall data quality {avg_qual:.0f}%"
    )
    if degraded:
        lines.append(
            f"⚠️ {len(degraded)} scan{'s' if len(degraded) > 1 else ''} with degraded data "
            f"— {len(degraded) * 3 // n_scans + 1} scans had technical data unavailable for some pairs"
        )
        if missed:
            lines.append(
                f"Estimated missed opportunities: {missed} potential "
                f"setup{'s' if missed > 1 else ''} not fully detected due to data failures"
            )
    else:
        lines.append(
            f"✅ Data quality normal across all {n_scans} scans this week"
        )
    return lines
