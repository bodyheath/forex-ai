"""Standing health-check system for daily.py's scan pipeline.

Mines each scan's per-day log file into a compact rolling telemetry record
(data/scan_telemetry.jsonl), then runs a set of independent checks against
that telemetry plus other already-persisted state (fund_state.json,
research_trades.csv, the workflow/code files themselves) to catch
regressions in things that are easy to silently break: universe coverage,
gate code paths going dark, fund-state staleness, dispatch failures, grade-
quality inversion, and warnings that quietly become universal noise instead
of rare signals.

Pure log/data parsing — no API calls, no network access, no trade/gate
decision logic touched. record_scan_telemetry() is called once per scan
from daily.py; everything else here is read-only and meant to be run from
scripts/health_check.py on its own schedule.

Public API
----------
record_scan_telemetry(log_path, meta) -> None   mine one day's log, append to jsonl
load_telemetry(n=500)                  -> list  last n telemetry records
check_universe_coverage(records)       -> list[str]
check_gate_silence(records)            -> list[str]
check_fund_state_staleness()           -> list[str]
check_dispatch(records)                -> list[str]
check_grade_ordering()                 -> list[str]
check_rib_strongly_against_edge_health() -> list[str]
check_learning_signal_readiness()      -> list[str]
check_audit_fixes_present()            -> list[str]
check_warning_fire_rate(records)       -> list[str]
build_opened_trade_digest(records, n=5) -> list[str]
run_all_checks()                       -> dict {"flags": [...], "digest": [...]}
"""
import json
import re
from pathlib import Path

import config

_TELEMETRY_FILE = config.DATA_DIR / "scan_telemetry.jsonl"
_MAX_TELEMETRY  = 500

_SILENCE_WINDOW       = 20   # scans considered for "gate went dark" check
_SILENCE_MIN_HISTORY  = 5    # don't flag until we have at least this many scans
_WARNING_WINDOW       = 10   # scans considered for the universal-warning check
_WARNING_MIN_STREAK   = 3    # consecutive high-fire-rate scans required to flag
_WARNING_FIRE_RATE    = 0.80 # fires-per-candidate ratio considered "near-universal"
_GRADE_MIN_N          = 15   # minimum decisive trades per grade bucket to compare
_GRADE_ORDER          = ["A", "B", "C", "D", "F"]  # best to worst

_RIB_EDGE_WINDOW      = 40   # trailing decisive-trade window checked against the older baseline
_RIB_EDGE_MIN_N       = 15   # minimum size for EITHER the trailing window or the older baseline

_LEARNING_SIGNAL_MIN_N = 15  # same n>=15 convention as _GRADE_MIN_N / learning.py's MIN_SAMPLES

# project_ribbon_regime_carveout_threshold.md criterion 1 (n bar only --
# criteria 2/3 there require actually running the direct z-test/PF check by
# hand once n is reached, not something this tripwire auto-decides).
_RIBBON_CARVEOUT_MIN_N_OFF = 40   # trending_risk_off
_RIBBON_CARVEOUT_MIN_N_ON  = 100  # trending_risk_on

# project_vix_regime_edge_threshold.md's promotion bar. Trades closing on or
# before this freeze point are the discovery sample that found the effect --
# permanently excluded from the promotion check, which must run on new data
# alone or it would just re-confirm the same fitted sample.
_VIX_DISCOVERY_FREEZE = "2026-08-31 23:59:59"  # UTC
_VIX_MIN_N_PER_CELL   = 30    # vix_above AND vix_below, each, on new data only
_VIX_MIN_PF_ABOVE     = 0.70  # anchored to the decayed (weaker) second half of the discovery sample
_VIX_MIN_PF_GAP       = 0.30  # PF(above) - PF(below), same anchor

# Gates that leave a recognisable trace in the log whenever a fund-eligible
# candidate is evaluated, regardless of outcome. Presence-only (not exact
# per-candidate counts) — robust to free-text log wording, and sufficient to
# answer "did this code path run at all this scan".
_GATE_PATTERNS = {
    "trend_hard":    re.compile(r"\[trend-hard\]"),
    "session":       re.compile(r"\[session\]"),
    "dd_tier":       re.compile(r"\[sizing\]"),
    "concentration": re.compile(r"[Cc]urrency concentration"),
    "capacity":      re.compile(r"\[capacity\]"),
    "da_escalation": re.compile(r"\bSonnet\b"),
}
_GATE_BLOCK_PATTERNS = {
    "trend_hard": re.compile(r"\[trend-hard\] BLOCKED"),
    "session":    re.compile(r"\[session\] BLOCKING"),
}

_WARN_LINE_RE   = re.compile(r"\[([\w-]+)\]\s*WARNING\s*(.*)")
_PAIR_TOKEN_RE  = re.compile(r"\b[A-Z]{3}/[A-Z]{3}\b")
# Word-boundary on both sides so a digit embedded in an identifier (t1_price,
# t2_hit) is left alone -- only standalone numeric values (3/10, 0.50, -1.2)
# get collapsed. "_" counts as \w, so "t1_price" has no boundary before "1".
_NUM_TOKEN_RE   = re.compile(r"\b-?\d+(\.\d+)?\b")


def _normalize_warning(tag: str, rest: str) -> str:
    """Collapse pair names and numeric values so the same warning template
    from different pairs/values groups under one counter key."""
    rest = _PAIR_TOKEN_RE.sub("{pair}", rest)
    rest = _NUM_TOKEN_RE.sub("N", rest)
    return f"[{tag}] WARNING {rest}".strip()


# ── Telemetry recording ──────────────────────────────────────────────────────

def record_scan_telemetry(log_path, meta: dict) -> None:
    """Mine one scan's just-written log file and append a compact JSON record.

    `meta` carries the handful of fields not reliably log-minable as free
    text (universe_size, n_deep, all_ohlcv_failed, early_reject_count,
    opened_pairs) — everything else (gate presence, warning counts, dispatch
    success) is extracted from the log text itself. Never raises: any
    failure here must not affect the scan that called it.
    """
    try:
        text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        text = ""

    candidates_evaluated = int(meta.get("n_deep") or 0)

    gates = {}
    for name, pattern in _GATE_PATTERNS.items():
        evaluated = bool(pattern.search(text))
        entry = {"evaluated": evaluated}
        block_pat = _GATE_BLOCK_PATTERNS.get(name)
        if block_pat is not None:
            entry["blocked"] = len(block_pat.findall(text))
        gates[name] = entry

    warnings: dict = {}
    opened_pairs = list(meta.get("opened_pairs") or [])
    opened_pair_warnings: dict = {p: [] for p in opened_pairs}
    for line in text.splitlines():
        m = _WARN_LINE_RE.search(line)
        if not m:
            continue
        tag, rest = m.group(1), m.group(2)
        key = _normalize_warning(tag, rest)
        warnings[key] = warnings.get(key, 0) + 1
        for p in opened_pairs:
            if p in line:
                opened_pair_warnings[p].append(line.strip())

    dispatch = {
        "telegram": (
            True if "[dispatch] telegram=OK" in text
            else False if "[dispatch] telegram=FAILED" in text
            else None
        ),
        "discord": (
            True if "[dispatch] discord=OK" in text
            else False if "[dispatch] discord=FAILED" in text
            else None
        ),
    }

    from datetime import datetime
    record = {
        "ts":                   datetime.now().isoformat(timespec="seconds"),
        "mode":                 meta.get("mode", ""),
        "universe_size":        int(meta.get("universe_size") or 0),
        "n_deep":               candidates_evaluated,
        "all_ohlcv_failed":     bool(meta.get("all_ohlcv_failed", False)),
        "candidates_evaluated": candidates_evaluated,
        "early_reject_count":   int(meta.get("early_reject_count") or 0),
        "gates":                gates,
        "warnings":             warnings,
        "opened_pairs":         opened_pairs,
        "opened_pair_warnings": {p: w for p, w in opened_pair_warnings.items() if w},
        "dispatch":             dispatch,
    }

    try:
        lines = []
        if _TELEMETRY_FILE.exists():
            lines = _TELEMETRY_FILE.read_text(encoding="utf-8").splitlines()
        lines.append(json.dumps(record))
        lines = lines[-_MAX_TELEMETRY:]
        _TELEMETRY_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


def load_telemetry(n: int = 500) -> list:
    """Load the last n telemetry records, oldest first. Empty list on any error."""
    try:
        lines = _TELEMETRY_FILE.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    records = []
    for line in lines[-n:]:
        try:
            records.append(json.loads(line))
        except Exception:
            continue
    return records


# ── Checks ────────────────────────────────────────────────────────────────────

def check_universe_coverage(records: list) -> list:
    """Flag if the full pair universe wasn't scanned for 2+ consecutive scans,
    or an OHLCV failure truncated a scan entirely."""
    flags = []
    if not records:
        return flags
    try:
        from src import selector
        expected = len(selector.UNIVERSE)
    except Exception:
        expected = 0

    last = records[-1]
    if last.get("all_ohlcv_failed"):
        flags.append(
            f"⚠️ Universe coverage — most recent {last.get('mode', '?')} scan had a "
            f"total OHLCV failure (all_ohlcv_failed=true)"
        )

    if expected > 0 and len(records) >= 2:
        recent = records[-2:]
        if all(r.get("universe_size", 0) < expected for r in recent):
            sizes = [r.get("universe_size", 0) for r in recent]
            flags.append(
                f"⚠️ Universe coverage — last 2 scans both scanned fewer pairs "
                f"than the configured universe ({sizes} vs expected {expected})"
            )
    return flags


def check_gate_silence(records: list) -> list:
    """Flag any gate whose log trace hasn't appeared at all across the rolling
    window, despite candidates having been evaluated in every one of those scans."""
    flags = []
    window = [r for r in records[-_SILENCE_WINDOW:] if r.get("candidates_evaluated", 0) > 0]
    if len(window) < _SILENCE_MIN_HISTORY:
        return flags  # not enough history yet — stay silent rather than guess

    for gate_name in _GATE_PATTERNS:
        if all(not (r.get("gates", {}).get(gate_name, {}).get("evaluated")) for r in window):
            flags.append(
                f"⚠️ Gate silent — '{gate_name}' has left zero trace in the log across "
                f"the last {len(window)} scans, despite candidates being evaluated every "
                f"time — its code path may not be executing"
            )
    return flags


def check_fund_state_staleness() -> list:
    """Compare fund_state.json against a fresh recompute from trades.csv.
    Same class of bug as the fund-state staleness fix shipped earlier —
    catches a future regression of the same shape.

    Only compares fields daily.py actually reads off _fund_st to make a
    sizing decision (consecutive_losses, consecutive_wins,
    current_drawdown_pct — confirmed via grep, these are the only
    `_fund_st.get(...)` calls in the file). fund_total_trades and
    sizing_mode are NOT read anywhere for a live decision and legitimately
    drift between a trade opening and the next reconcile-on-close — flagging
    those would be a permanent, meaningless false positive rather than a
    real staleness signal.
    """
    flags = []
    try:
        import pandas as pd
        from src import fund_state as _fs
        from src.trading import financials

        trades_path = config.DATA_DIR / "trades.csv"
        if not trades_path.exists():
            return flags
        df = pd.read_csv(trades_path)
        fresh = financials.calculate_fund_state(df, None)
        disk = _fs.load()

        stable_fields = [
            "consecutive_losses", "consecutive_wins", "current_drawdown_pct",
        ]
        mismatches = []
        for field in stable_fields:
            fv, dv = fresh.get(field), disk.get(field)
            if isinstance(fv, float) or isinstance(dv, float):
                if fv is None or dv is None or abs(float(fv) - float(dv)) > 0.01:
                    mismatches.append(f"{field}: fresh={fv!r} vs disk={dv!r}")
            elif fv != dv:
                mismatches.append(f"{field}: fresh={fv!r} vs disk={dv!r}")

        if mismatches:
            flags.append(
                "🚨 fund_state.json staleness — disk state disagrees with a fresh "
                "recompute from trades.csv: " + "; ".join(mismatches)
            )

        # 2026-08-31: peak_balance/balance were excluded from stable_fields above
        # deliberately (see Finding 2 of the 2026-08-31 full-system audit) --
        # but that left a real blind spot: a torn write can wipe peak_balance to
        # 0.0 (reproduced this session) with nothing here to catch it. Can't
        # just add "peak_balance" to stable_fields with an equality check the
        # way the other three fields work, though -- disk peak is SUPPOSED to
        # sit above the current balance during a real, legitimate drawdown, so
        # disk != fresh is the normal case, not a bug. The one shape that is
        # never legitimate under correct operation is disk peak dropping BELOW
        # the fresh current balance -- peak = max(peak, balance) makes that
        # mathematically impossible unless the stored peak was wiped/corrupted
        # low. Checking against fresh BALANCE specifically (not fresh peak) is
        # deliberate too: fresh peak from a full chronological recompute could
        # legitimately be a hair below a stale disk peak from something this
        # narrow check shouldn't be trying to adjudicate.
        fresh_balance = fresh.get("balance")
        disk_peak     = disk.get("peak_balance")
        if fresh_balance is not None and disk_peak is not None:
            if float(disk_peak) < float(fresh_balance) - 0.01:
                flags.append(
                    "🚨 fund_state.json peak_balance corruption — disk peak_balance "
                    f"({disk_peak!r}) is LESS than a fresh recompute's current balance "
                    f"({fresh_balance!r}), which is impossible under correct operation "
                    "(peak can never be below balance) — the stored peak was likely "
                    "wiped or corrupted by an interrupted write"
                )
    except Exception as e:
        flags.append(f"⚠️ fund-state staleness check itself failed to run: {e}")
    return flags


def check_outcome_analysis_gap() -> list:
    """Same ground-truth-vs-cache reconciliation shape as
    check_fund_state_staleness() — flags any closed fund trade missing from
    win_analysis.json/loss_analysis.json. Confirmed this can happen
    silently even with the pending-retry queue in place (#2059, #2257,
    #2446, #2484, #2909), since that queue only helps a trade that was
    attempted at least once; a trade never passed to run_outcome_analysis()
    at all leaves no trace anywhere.

    run_outcome_analysis() now self-heals this on every call (daily.py scan
    + monitor.py cycle) via find_unanalyzed_closed_trades(), so this check
    should always read empty in normal operation. If it ever fires, that
    means the self-healing itself is failing (e.g. Claude API persistently
    erroring) — investigate directly, don't just wait for the next scan.
    """
    flags = []
    try:
        from src import outcome_analyst
        missing = outcome_analyst.find_unanalyzed_closed_trades()
        if missing:
            ids = [str(m.get("id")) for m in missing]
            flags.append(
                f"🚨 Outcome-analysis gap — {len(missing)} closed fund trade(s) "
                f"missing from win/loss analysis despite self-healing "
                f"reconciliation: {ids} — the reconciliation itself may be "
                f"failing, investigate directly rather than waiting"
            )
    except Exception as e:
        flags.append(f"⚠️ Outcome-analysis gap check itself failed to run: {e}")
    return flags


def check_duplicate_open_trades() -> list:
    """Flag any currently-open/pending fund position that exists as more
    than one row in trades.csv (same pair/direction/timestamp/entry/stop).

    2026-08-28: confirmed real — a single open EUR/AUD position existed as
    3 identical rows (2 with a corrupted blank id) after a BOM-introduced
    id-wiping bug, which inflated _get_open_fund_count()/
    check_currency_exposure() and could silently block new legitimate
    trades on nothing but a data artifact. Both of those now dedupe
    defensively at count-time, so this check is a read-only backstop:
    it should stay empty; if it ever fires, a duplication bug has
    recurred and needs investigating directly, the same as
    check_fund_state_staleness()/check_outcome_analysis_gap().
    """
    flags = []
    try:
        import pandas as pd
        path = config.DATA_DIR / "trades.csv"
        if not path.exists():
            return flags
        df = pd.read_csv(path, encoding="utf-8-sig", dtype=str, keep_default_na=False)
        open_df = df[
            (df["trade_this"].astype(str) == "YES")
            & (df["status"].isin(["OPEN", "PENDING"]))
        ]
        dedup_cols = [c for c in ("pair", "direction", "timestamp", "entry", "stop_loss")
                      if c in open_df.columns]
        if not dedup_cols:
            return flags
        dupe_mask = open_df.duplicated(subset=dedup_cols, keep=False)
        dupes = open_df[dupe_mask]
        if len(dupes):
            groups = dupes.groupby(dedup_cols).size()
            pairs = sorted(set(open_df.loc[dupe_mask, "pair"]))
            flags.append(
                f"🚨 Duplicate open trade rows — {len(dupes)} row(s) across "
                f"{len(groups)} duplicate group(s) for {pairs} — capacity/"
                f"concentration counting dedupes defensively, but the "
                f"underlying duplication itself needs investigating"
            )
    except Exception as e:
        flags.append(f"⚠️ Duplicate open-trade check itself failed to run: {e}")
    return flags


def check_dispatch(records: list) -> list:
    """Flag 2+ consecutive dispatch failures on either channel (ignoring
    scans where that channel wasn't attempted at all)."""
    flags = []
    for channel in ("telegram", "discord"):
        attempted = [r for r in records if r.get("dispatch", {}).get(channel) is not None]
        recent = attempted[-2:]
        if len(recent) == 2 and all(r["dispatch"][channel] is False for r in recent):
            flags.append(
                f"🚨 Dispatch failure — {channel} has failed on the last "
                f"{len(recent)} scans that attempted it"
            )
    return flags


def _ztest_worse_beats_better(better_wins, better_n, worse_wins, worse_n):
    """Two-proportion z-test; returns p-value, or None if inputs are degenerate."""
    try:
        p1, p2 = worse_wins / worse_n, better_wins / better_n
        p_pool = (worse_wins + better_wins) / (worse_n + better_n)
        se = (p_pool * (1 - p_pool) * (1 / worse_n + 1 / better_n)) ** 0.5
        if se == 0:
            return None
        z = (p1 - p2) / se
        from math import erf, sqrt
        p_value = 1 - 0.5 * (1 + erf(abs(z) / sqrt(2)))
        return p_value, p1, p2
    except Exception:
        return None


def check_grade_ordering(csv_path=None) -> list:
    """Flag if a strictly-worse grade significantly outperforms a strictly-
    better one on decisive win-rate — same class as the rib_strongly_against
    mis-grading bug found earlier this session.

    Stays silent for any bucket below _GRADE_MIN_N — a thin/unstable sample
    is not evidence of anything, and must never be reported as a false flag.

    2026-09-01: filtered to v2 + closed_at>=2026-07-14 13:46:31 UTC (the
    exit-logic-fix cutoff) -- the follow-up the 2026-08-28 re-verification
    flagged as needed but blocked on data volume at the time. Without this
    filter, D-vs-C fired every run on stale pre-fix grade labels (already
    diagnosed and closed as a duplication/staleness artifact, not a live
    finding -- re-confirmed 2026-09-01: raw n=812/61 p=0.037, but strict
    n=475/40 p=0.375, not significant). F-vs-D holds under both, and more
    strongly under strict (p=1.8e-8) -- this filter doesn't suppress a real
    signal, it removes a stale one from re-triggering every run.
    """
    flags = []
    try:
        import pandas as pd
        path = csv_path or (config.DATA_DIR / "research_trades.csv")
        if not Path(path).exists():
            return flags
        df = pd.read_csv(path)
        status_u = df["status"].astype(str).str.upper()
        closed_dt = pd.to_datetime(df["closed_at"], errors="coerce", utc=True)
        cutoff = pd.Timestamp("2026-07-14 13:46:31", tz="UTC")
        decisive = df[
            status_u.isin(["WIN", "FULL_WIN", "LOSS"])
            & (df["system_version"] == "v2")
            & (closed_dt >= cutoff)
        ]

        buckets = {}
        for grade in _GRADE_ORDER:
            g = decisive[decisive["grade"].astype(str) == grade]
            n = len(g)
            if n < _GRADE_MIN_N:
                continue  # thin sample — excluded from comparison, not flagged
            wins = int((g["status"].astype(str).str.upper().isin(["WIN", "FULL_WIN"])).sum())
            buckets[grade] = (wins, n)

        present = [g for g in _GRADE_ORDER if g in buckets]
        for i in range(len(present) - 1):
            better, worse = present[i], present[i + 1]
            b_wins, b_n = buckets[better]
            w_wins, w_n = buckets[worse]
            result = _ztest_worse_beats_better(b_wins, b_n, w_wins, w_n)
            if result is None:
                continue
            p_value, worse_wr, better_wr = result
            if worse_wr > better_wr and p_value < 0.05:
                flags.append(
                    f"🚨 Grade ordering inverted — grade {worse} (n={w_n}, "
                    f"WR={worse_wr*100:.1f}%) significantly outperforms grade {better} "
                    f"(n={b_n}, WR={better_wr*100:.1f}%), p={p_value:.4f}"
                )
    except Exception as e:
        flags.append(f"⚠️ Grade ordering check itself failed to run: {e}")
    return flags


def check_rib_strongly_against_edge_health(csv_path=None) -> list:
    """Standing regression tripwire for the rib_strongly_against (non-GBP)
    edge confirmed this session (docs/edge_hypotheses.md has the causal
    hypothesis and its own invalidation condition for WHY this edge might
    hold or break).

    2026-08-30: a regime split of this exact population found it is NOT
    uniform -- ranging_low_vol/trending_risk_off decisive trades win at
    ~51-55% (PF 2.4+), while trending_risk_on decisive trades (the majority
    of this population) win at ~31.5% (PF 0.93, effectively breakeven),
    p=0.003. That's an already-known, currently-true structural fact, not
    an anomaly to alert on every run -- alerting on it unconditionally would
    be exactly the "known condition treated as permanent noise" anti-pattern
    check_warning_fire_rate exists to catch elsewhere in this file.

    What IS worth a standing tripwire: whether the edge's overall
    performance is drifting away from its own established history, not
    whether it's regime-dependent (already known, already reflected in the
    Grade-C cap this population receives). Compares the most recent
    _RIB_EDGE_WINDOW decisive trades in this population against the OLDER,
    disjoint remainder as the baseline (not the whole population including
    the recent window, which would compare a subset against a superset
    containing itself) -- flags only if recent performance has fallen
    significantly below that baseline.
    """
    flags = []
    try:
        import pandas as pd
        path = csv_path or (config.DATA_DIR / "research_trades.csv")
        if not Path(path).exists():
            return flags
        df = pd.read_csv(path)
        decisive = df[df["status"].astype(str).str.upper().isin(["WIN", "FULL_WIN", "LOSS"])].copy()
        if decisive.empty:
            return flags

        direction = decisive["direction"].astype(str).str.upper()
        ribbon    = decisive["ribbon_state"].astype(str)
        rib_against = ((direction == "BUY")  & (ribbon == "ALIGNED_BEAR")) | \
                      ((direction == "SELL") & (ribbon == "ALIGNED_BULL"))
        non_gbp = ~decisive["pair"].astype(str).str.upper().str.contains("GBP")
        pop = decisive[rib_against & non_gbp].copy()

        if "closed_at" in pop.columns:
            pop["_closed_dt"] = pd.to_datetime(pop["closed_at"], errors="coerce")
            pop = pop.sort_values("_closed_dt")

        n_total = len(pop)
        recent = pop.tail(_RIB_EDGE_WINDOW)
        older  = pop.iloc[: max(0, n_total - _RIB_EDGE_WINDOW)]
        if len(recent) < _RIB_EDGE_MIN_N or len(older) < _RIB_EDGE_MIN_N:
            return flags  # too little data on one side for a meaningful comparison

        def _wins(d):
            return int(d["status"].astype(str).str.upper().isin(["WIN", "FULL_WIN"]).sum())

        older_n, older_wins   = len(older), _wins(older)
        recent_n, recent_wins = len(recent), _wins(recent)

        result = _ztest_worse_beats_better(older_wins, older_n, recent_wins, recent_n)
        if result is None:
            return flags
        p_value, recent_wr, older_wr = result
        if recent_wr < older_wr and p_value < 0.05:
            flags.append(
                f"🚨 rib_strongly_against (non-GBP) edge drifting — most recent "
                f"{recent_n} decisive trades WR={recent_wr*100:.1f}% vs the prior "
                f"{older_n} decisive trades WR={older_wr*100:.1f}%, p={p_value:.4f}. "
                f"See docs/edge_hypotheses.md for the causal hypothesis this may be invalidating."
            )
    except Exception as e:
        flags.append(f"⚠️ rib_strongly_against edge-health check itself failed to run: {e}")
    return flags


def check_learning_signal_readiness(csv_path=None) -> list:
    """Standing tripwire for the Part 5.4 gap found in the 2026-08-30
    structural audit: memory_hash, consensus_adj, and da_fired all had
    pre-registered analysis plans (see project_learning_signals_analysis_
    plan.md and project_da_downgrade_tracking.md) but nothing that would
    ever tell a human when there's actually enough data to run them --
    "learning from itself" depended entirely on someone remembering to
    check back. This closes that gap: once a signal crosses its
    pre-registered minimum n, flag it once.

    "Meaningful" is NOT simply "non-blank" for two of these three fields,
    and that's deliberate, not a shortcut:
      - memory_hash: non-blank is the correct, unambiguous signal -- it is
        only ever set when a candidate actually reached Stage-2 Sonnet.
      - consensus_adj: non-blank would be USELESS here -- daily.py writes
        the literal default 0 for every sweep-sourced/ineligible row too
        (research_tracker.log_research_trade's `_rp.get("consensus_adj", 0)`
        fallback), so "0" is never actually blank once the schema exists.
        consensus_eligible (added 2026-08-30 specifically for this
        ambiguity) is the real signal: True only when the candidate reached
        the decisive/neutral branch inside _apply_currency_consensus().
      - da_fired: same class of problem, NOT fixed this pass (out of scope
        of the 2026-08-30 ambiguity fix, which only covered consensus_adj)
        -- _trade_quality_grade() always returns a real True/False, so
        non-blank would count nearly every decisive row, firing this
        readiness flag long before there's actually enough
        DA-genuinely-evaluated data. Using da_fired=="True" specifically
        undercounts (misses "DA ran, found nothing" rows) but is the only
        unambiguous non-blank signal available -- and undercounting means
        this flag can only fire LATE, never prematurely, which is the safe
        direction for a "wait until ready" gate.

    Each flag's text is a static string (no live n/percentage baked in) so
    it fires exactly once via the same flag-set dedup every other check in
    this file already relies on -- once true, the underlying count only
    grows, so the flag would otherwise never need to repeat, but a live
    number in the text would make every run's message text "new" and
    defeat the dedup.

    2026-08-31: extended to cover two more pending analyses that were being
    checked manually rather than by this tripwire -- the ribbon-regime
    carve-out's n bar (project_ribbon_regime_carveout_threshold.md) and the
    vix_vs_20d_avg-within-trending_risk_on promotion bar
    (project_vix_regime_edge_threshold.md). Each lives in its own private
    helper below since both need a materially different population query
    than the three signals above (different decisive-status sets, extra
    filters, a new-data-only slice for the vix check) -- kept as separate
    functions for readability, not as a separate tripwire: both still feed
    into this function's one return value, so run_all_checks() and the
    alert dedup in scripts/health_check.py need no changes to pick them up.
    """
    flags = []
    try:
        import pandas as pd
        path = csv_path or (config.DATA_DIR / "research_trades.csv")
        if not Path(path).exists():
            return flags
        df = pd.read_csv(path)
        decisive = df[df["status"].astype(str).str.upper().isin(["WIN", "FULL_WIN", "LOSS"])]
        if decisive.empty:
            return flags

        def _truthy(series):
            return series.fillna("").astype(str).str.strip().str.lower().isin(("true", "1", "1.0"))

        def _non_blank(series):
            # pandas reads an empty CSV cell as NaN, and str(NaN) == "nan" --
            # non-empty as a string, so a naive astype(str) check would
            # miscount genuinely blank/missing cells as meaningful.
            return series.fillna("").astype(str).str.strip() != ""

        checks = []
        if "memory_hash" in decisive.columns:
            n = int(_non_blank(decisive["memory_hash"]).sum())
            checks.append(("memory_hash", n,
                            "see project_learning_signals_analysis_plan.md's before/after natural-experiment design"))
        if "consensus_eligible" in decisive.columns:
            n = int(_truthy(decisive["consensus_eligible"]).sum())
            checks.append(("consensus_adj", n,
                            "see project_learning_signals_analysis_plan.md — filter to consensus_eligible==True, compare within grade"))
        if "da_fired" in decisive.columns:
            n = int(_truthy(decisive["da_fired"]).sum())
            checks.append(("da_downgraded", n,
                            "see project_da_downgrade_tracking.md — compare within da_grade_before, n undercounts (da_fired=True only)"))

        for label, n, pointer in checks:
            if n >= _LEARNING_SIGNAL_MIN_N:
                flags.append(
                    f"📊 {label} has reached n>={_LEARNING_SIGNAL_MIN_N} decisive trades — "
                    f"ready for the pre-registered analysis ({pointer})"
                )
    except Exception as e:
        flags.append(f"⚠️ learning-signal readiness check itself failed to run: {e}")

    try:
        flags += _check_ribbon_regime_carveout_readiness(csv_path)
    except Exception as e:
        flags.append(f"⚠️ ribbon-regime carve-out readiness check itself failed to run: {e}")

    try:
        flags += _check_vix_regime_edge_readiness(csv_path)
    except Exception as e:
        flags.append(f"⚠️ vix-regime edge readiness check itself failed to run: {e}")

    return flags


def _check_ribbon_regime_carveout_readiness(csv_path=None) -> list:
    """project_ribbon_regime_carveout_threshold.md criterion 1: n>=40 in
    trending_risk_off AND n>=100 in trending_risk_on. Fires once both are
    crossed; criteria 2 (direct z-test) and 3 (PF<=0.80) still require a
    human to actually run them -- this only tells you when it's worth doing.

    Population (frozen -- do not redefine after the fact): rib_against
    superset (ALIGNED_BEAR/LEANING_BEAR for BUY, ALIGNED_BULL/LEANING_BULL
    for SELL), non-GBP, non-CHF-cluster (EUR/CHF, NZD/CHF, AUD/CHF),
    confidence>=6, v2, closed_at>=2026-07-14 13:46:31 UTC.

    "Decisive" here INCLUDES PARTIAL_WIN -- confirmed 2026-08-31 as the
    convention that actually produced this population's originally-recorded
    n=32/77 (see the addendum in project_ribbon_regime_carveout_threshold.md
    and feedback_decisive_status_ambiguity.md). This is deliberately NOT the
    same decisive set as check_learning_signal_readiness()'s outer decisive
    variable or _check_vix_regime_edge_readiness() below (WIN/FULL_WIN/LOSS,
    no PARTIAL_WIN) -- each population's frozen definition is spelled out
    explicitly per-check rather than assumed shared, precisely because that
    assumption is what caused the apparent "shrinkage" this was found from.
    """
    flags = []
    import pandas as pd
    path = csv_path or (config.DATA_DIR / "research_trades.csv")
    if not Path(path).exists():
        return flags
    df = pd.read_csv(path)
    required = {"status", "system_version", "closed_at", "direction",
                "ribbon_state", "pair", "confidence", "market_regime"}
    if not required.issubset(df.columns):
        return flags

    decisive = df[df["status"].astype(str).str.upper().isin(
        ["WIN", "FULL_WIN", "PARTIAL_WIN", "LOSS"])].copy()
    if decisive.empty:
        return flags
    decisive = decisive[decisive["system_version"] == "v2"]
    decisive["_closed_dt"] = pd.to_datetime(decisive["closed_at"], errors="coerce", utc=True)
    cutoff = pd.Timestamp("2026-07-14 13:46:31", tz="UTC")
    decisive = decisive[decisive["_closed_dt"] >= cutoff]

    direction = decisive["direction"].astype(str).str.upper()
    ribbon    = decisive["ribbon_state"].astype(str)
    rib_against = ((direction == "BUY")  & ribbon.isin(["ALIGNED_BEAR", "LEANING_BEAR"])) | \
                  ((direction == "SELL") & ribbon.isin(["ALIGNED_BULL", "LEANING_BULL"]))
    pair_up = decisive["pair"].astype(str).str.upper()
    non_gbp = ~pair_up.str.contains("GBP")
    non_chf_cluster = ~pair_up.isin(["EUR/CHF", "NZD/CHF", "AUD/CHF"])
    conf = pd.to_numeric(decisive["confidence"], errors="coerce")

    pop = decisive[rib_against & non_gbp & non_chf_cluster & (conf >= 6)]
    n_off = int((pop["market_regime"] == "trending_risk_off").sum())
    n_on  = int((pop["market_regime"] == "trending_risk_on").sum())

    if n_off >= _RIBBON_CARVEOUT_MIN_N_OFF and n_on >= _RIBBON_CARVEOUT_MIN_N_ON:
        flags.append(
            f"📊 Ribbon-regime carve-out population has reached n>={_RIBBON_CARVEOUT_MIN_N_OFF} "
            f"trending_risk_off and n>={_RIBBON_CARVEOUT_MIN_N_ON} trending_risk_on — "
            f"ready to run the pre-registered direct z-test and PF check "
            f"(see project_ribbon_regime_carveout_threshold.md criteria 2-3)"
        )
    return flags


def _check_vix_regime_edge_readiness(csv_path=None) -> list:
    """project_vix_regime_edge_threshold.md's promotion bar, checked ONLY
    against trades closing strictly after the 2026-08-31 discovery freeze --
    pooling with the discovery sample would just re-confirm the same fitted
    data, which is exactly what this bar exists to prevent given the effect
    already decayed once within that sample's own two time-halves.

    Population: market_regime=='trending_risk_on', decisive (WIN/FULL_WIN/
    LOSS -- this population's own frozen definition does NOT include
    PARTIAL_WIN, unlike the ribbon-regime check above), v2, closed_at both
    >=2026-07-14 13:46:31 UTC and > the discovery freeze.

    All three of the doc's checkable criteria are evaluated: n>=30 in EACH
    of vix_above/vix_below, a direct (not pooled) two-proportion z-test
    p<0.05 in the hypothesized direction, and a PF floor/gap anchored to the
    discovery sample's weaker (already-decayed) second half. Criterion 4
    there (re-verify the regime confound) is structurally guaranteed here --
    the query is already restricted to market_regime=='trending_risk_on'
    alone, so there is no cross-regime pooling for the confound to hide in.
    """
    flags = []
    import pandas as pd
    path = csv_path or (config.DATA_DIR / "research_trades.csv")
    if not Path(path).exists():
        return flags
    df = pd.read_csv(path)
    required = {"status", "system_version", "closed_at", "market_regime",
                "vix_vs_20d_avg", "net_pips"}
    if not required.issubset(df.columns):
        return flags

    decisive = df[df["status"].astype(str).str.upper().isin(["WIN", "FULL_WIN", "LOSS"])].copy()
    if decisive.empty:
        return flags
    decisive = decisive[decisive["system_version"] == "v2"]
    decisive["_closed_dt"] = pd.to_datetime(decisive["closed_at"], errors="coerce", utc=True)
    cutoff = pd.Timestamp("2026-07-14 13:46:31", tz="UTC")
    freeze = pd.Timestamp(_VIX_DISCOVERY_FREEZE, tz="UTC")
    new_data = decisive[(decisive["_closed_dt"] >= cutoff) & (decisive["_closed_dt"] > freeze)]

    ton = new_data[new_data["market_regime"] == "trending_risk_on"]
    above = ton[ton["vix_vs_20d_avg"] == 1.0]
    below = ton[ton["vix_vs_20d_avg"] == -1.0]
    n_above, n_below = len(above), len(below)
    if n_above < _VIX_MIN_N_PER_CELL or n_below < _VIX_MIN_N_PER_CELL:
        return flags  # not enough new (post-freeze) data yet

    def _wins(d):
        return int(d["status"].astype(str).str.upper().isin(["WIN", "FULL_WIN"]).sum())

    def _pf(d):
        gp = d.loc[d["net_pips"] > 0, "net_pips"].sum()
        gl = -d.loc[d["net_pips"] < 0, "net_pips"].sum()
        return gp / gl if gl > 0 else float("inf")

    wins_above, wins_below = _wins(above), _wins(below)
    pf_above, pf_below = _pf(above), _pf(below)

    result = _ztest_worse_beats_better(wins_above, n_above, wins_below, n_below)
    if result is None:
        return flags
    p_value, wr_below, wr_above = result

    sig   = wr_above > wr_below and p_value < 0.05
    pf_ok = pf_above >= _VIX_MIN_PF_ABOVE and (pf_above - pf_below) >= _VIX_MIN_PF_GAP

    if sig and pf_ok:
        flags.append(
            f"📊 vix_vs_20d_avg-within-trending_risk_on has cleared its promotion bar "
            f"on NEW data alone (n>={_VIX_MIN_N_PER_CELL}/cell closing after "
            f"{_VIX_DISCOVERY_FREEZE} UTC, independent p<0.05, PF gap>={_VIX_MIN_PF_GAP}) — "
            f"ready to evaluate for promotion (see project_vix_regime_edge_threshold.md)"
        )
    return flags


# Lightweight, static presence checks mirroring the session's 16-item final
# verification pass. Each is (label, file, pattern) — file content only,
# no execution. Kept intentionally small: this is a regression tripwire for
# "did this get silently reverted/lost", not a re-audit.
_AUDIT_FIX_CHECKS = [
    ("Improvement-4 exemption",           "daily.py",                                  r"_rib_weekly_exempt"),
    ("FUND_TRADE_MIN_CONF at 7.0",        "daily.py",                                  r"FUND_TRADE_MIN_CONF\s*=\s*7\.0"),
    ("Fund-state reconcile helper",       "src/fund_state.py",                         r"def reconcile_from_trades"),
    ("monitor.py calls reconcile",        "src/monitor.py",                            r"reconcile_from_trades"),
    ("absorb_remote_trades generalized",  "scripts/absorb_remote_trades.py",           r"research_trades\.csv"),
    ("heartbeat watchdog workflow",       ".github/workflows/heartbeat_watchdog.yml",  r"schedule:"),
    ("heartbeat watchdog script",         "scripts/heartbeat_watchdog.py",             r"STALENESS_MINUTES"),
    ("intraday if:always() commit step",  ".github/workflows/intraday.yml",            r"if: always\(\)"),
    ("daily.yml if:always() commit step", ".github/workflows/daily.yml",               r"if: always\(\)"),
    ("london scan slot",                  "daily.py",                                  r'"london":\s*\('),
    ("intraday native schedule trigger",  ".github/workflows/intraday.yml",            r"cron:\s*'5 7 \* \* 1-5'"),
]


def check_audit_fixes_present(repo_root=None) -> list:
    """Automated, lightweight version of the manual verification sweep — flags
    anything that's disappeared from the code since it was last confirmed live."""
    flags = []
    root = Path(repo_root) if repo_root else Path(config.__file__).resolve().parent
    for label, rel_path, pattern in _AUDIT_FIX_CHECKS:
        path = root / rel_path
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            if not re.search(pattern, text):
                flags.append(f"🚨 Audit fix missing — '{label}' not found in {rel_path}")
        except Exception as e:
            flags.append(f"⚠️ Audit fix check couldn't read {rel_path} for '{label}': {e}")
    return flags


def check_warning_fire_rate(records: list) -> list:
    """Flag any warning template that fires on a near-universal share of
    candidates for several scans running — the exact signature of the
    t1_price bug (a warning meant for rare bad data instead firing on
    every trade and drowning out real signal)."""
    flags = []
    window = [r for r in records[-_WARNING_WINDOW:] if r.get("candidates_evaluated", 0) > 0]
    if len(window) < _WARNING_MIN_STREAK:
        return flags

    templates = set()
    for r in window:
        templates.update(r.get("warnings", {}).keys())

    for template in templates:
        rates = []
        for r in window:
            n = r.get("candidates_evaluated", 0)
            count = r.get("warnings", {}).get(template, 0)
            rates.append(count / n if n else 0)
        # Check the most recent _WARNING_MIN_STREAK scans for a sustained high rate
        tail = rates[-_WARNING_MIN_STREAK:]
        if len(tail) == _WARNING_MIN_STREAK and all(r >= _WARNING_FIRE_RATE for r in tail):
            flags.append(
                f"⚠️ Universal warning — '{template}' fired on ≥{_WARNING_FIRE_RATE*100:.0f}% "
                f"of candidates for the last {_WARNING_MIN_STREAK} scans — likely stale/"
                f"miscalibrated rather than a rare data issue (same class as the t1_price bug)"
            )
    return flags


def build_opened_trade_digest(records: list, n: int = 5) -> list:
    """Collect WARNING lines attached to trades that actually opened, across
    the last n scans, so they surface in one place instead of scan noise."""
    digest = []
    for r in records[-n:]:
        for pair, warns in r.get("opened_pair_warnings", {}).items():
            for w in warns:
                digest.append(f"[{r.get('ts', '?')}] {pair}: {w}")
    return digest


def run_all_checks() -> dict:
    """Run every check and return {'flags': [...], 'digest': [...]}."""
    records = load_telemetry()
    flags = []
    flags += check_universe_coverage(records)
    flags += check_gate_silence(records)
    flags += check_fund_state_staleness()
    flags += check_outcome_analysis_gap()
    flags += check_duplicate_open_trades()
    flags += check_dispatch(records)
    flags += check_grade_ordering()
    flags += check_rib_strongly_against_edge_health()
    flags += check_learning_signal_readiness()
    flags += check_audit_fixes_present()
    flags += check_warning_fire_rate(records)
    digest = build_opened_trade_digest(records)
    return {"flags": flags, "digest": digest}
