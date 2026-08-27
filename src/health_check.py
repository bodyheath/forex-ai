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
_NUM_TOKEN_RE   = re.compile(r"-?\d+(\.\d+)?")


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
    catches a future regression of the same shape."""
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
            "fund_total_trades", "sizing_mode", "consecutive_losses",
            "consecutive_wins", "win_rate", "profit_factor",
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
    except Exception as e:
        flags.append(f"⚠️ fund-state staleness check itself failed to run: {e}")
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
    """
    flags = []
    try:
        import pandas as pd
        path = csv_path or (config.DATA_DIR / "research_trades.csv")
        if not Path(path).exists():
            return flags
        df = pd.read_csv(path)
        decisive = df[df["status"].astype(str).str.upper().isin(["WIN", "FULL_WIN", "LOSS"])]

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
    flags += check_dispatch(records)
    flags += check_grade_ordering()
    flags += check_audit_fixes_present()
    flags += check_warning_fire_rate(records)
    digest = build_opened_trade_digest(records)
    return {"flags": flags, "digest": digest}
