"""Win/loss/expiry outcome analysis via Claude API.

For each newly-closed trade, calls Sonnet to identify what worked (WIN) or
what went wrong (LOSS).  Extracts a one-sentence PATTERN: rule, appends the
full analysis to win_analysis.json / loss_analysis.json, and writes the
pattern into memory.json (source="outcome") so future analyses learn from it.

Design rules:
- Never re-analyses a trade ID already present in the output files.
- "outcome" patterns survive learning.update_memory() — that function only
  replaces source=="auto" records.
- Capped at 20 outcome records in memory; oldest are evicted (FIFO) first.
"""

import json
from datetime import datetime

from anthropic import Anthropic

import config
from src import memory, tracker

_MAX_OUTCOME_PATTERNS = 20
_DEDUP_CHARS = 60


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def _load_json(path) -> list:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _save_json(path, data: list) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

def _already_analysed(rec_id) -> bool:
    """True if this trade ID appears in either analysis file."""
    for path in (config.WIN_ANALYSIS_FILE, config.LOSS_ANALYSIS_FILE):
        for rec in _load_json(path):
            if str(rec.get("id")) == str(rec_id):
                return True
    return False


def _is_near_duplicate(new_pattern: str) -> bool:
    prefix = new_pattern[:_DEDUP_CHARS].lower()
    for r in memory.load():
        if r.get("source") == "outcome":
            if r.get("pattern", "")[:_DEDUP_CHARS].lower() == prefix:
                return True
    return False


# ---------------------------------------------------------------------------
# Memory write
# ---------------------------------------------------------------------------

def _add_to_memory(pattern: str, status: str, pair: str, closed_at: str) -> None:
    records = memory.load()
    outcome_recs = [r for r in records if r.get("source") == "outcome"]
    other_recs   = [r for r in records if r.get("source") != "outcome"]

    # Evict oldest outcome records if at cap.
    while len(outcome_recs) >= _MAX_OUTCOME_PATTERNS:
        outcome_recs.pop(0)

    outcome_recs.append({
        "source": "outcome",
        "pattern": pattern,
        "outcome": f"[{status}] {pair} closed {closed_at}",
    })
    memory.save(other_recs + outcome_recs)


# ---------------------------------------------------------------------------
# Claude prompts
# ---------------------------------------------------------------------------

def _win_prompt(row: dict, report_text: str) -> str:
    return (
        f"This forex trade recommendation was a WIN — the target was hit.\n\n"
        f"Trade data:\n"
        f"  Pair: {row.get('pair')} | Direction: {row.get('direction')}\n"
        f"  Confidence: {row.get('confidence')}/10\n"
        f"  Entry: {row.get('entry')} | Target: {row.get('target')} | Stop: {row.get('stop_loss')}\n"
        f"  R-multiple: {row.get('r_multiple')} | Pips: {row.get('pips')}\n"
        f"  Layer scores — Technical: {row.get('technical')}, "
        f"Fundamental: {row.get('fundamental')}, Sentiment: {row.get('sentiment')}, "
        f"Positioning: {row.get('positioning')}, Macro: {row.get('macro')}\n"
        f"  Opened: {row.get('timestamp')} | Closed: {row.get('closed_at')}\n\n"
        f"Original analysis report:\n{report_text}\n\n"
        f"In 3-4 sentences identify exactly what made this trade successful: which "
        f"data sources aligned correctly, which indicators were most predictive, and "
        f"whether the entry timing was optimal.\n\n"
        f"End your response with exactly one line:\n"
        f"PATTERN: <one concise generalizable rule for future trades, max 120 chars>"
    )


def _loss_prompt(row: dict, report_text: str) -> str:
    return (
        f"This forex trade recommendation was a LOSS — the stop loss was hit.\n\n"
        f"Trade data:\n"
        f"  Pair: {row.get('pair')} | Direction: {row.get('direction')}\n"
        f"  Confidence: {row.get('confidence')}/10\n"
        f"  Entry: {row.get('entry')} | Target: {row.get('target')} | Stop: {row.get('stop_loss')}\n"
        f"  R-multiple: {row.get('r_multiple')} | Pips: {row.get('pips')}\n"
        f"  Layer scores — Technical: {row.get('technical')}, "
        f"Fundamental: {row.get('fundamental')}, Sentiment: {row.get('sentiment')}, "
        f"Positioning: {row.get('positioning')}, Macro: {row.get('macro')}\n"
        f"  Opened: {row.get('timestamp')} | Closed: {row.get('closed_at')}\n\n"
        f"Original analysis report:\n{report_text}\n\n"
        f"In 3-4 sentences identify exactly what went wrong: which conflicting signal "
        f"was missed or underweighted, whether a news/macro event overrode the "
        f"technical setup, and what warning sign should have prevented this trade.\n\n"
        f"End your response with exactly one line:\n"
        f"PATTERN: <one concise warning rule to prevent similar losses, max 120 chars>"
    )


def _call_claude(prompt: str) -> str:
    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def _extract_pattern(text: str) -> str | None:
    for line in text.splitlines():
        if line.strip().upper().startswith("PATTERN:"):
            p = line.split(":", 1)[1].strip()
            return p[:120] if p else None
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_outcome_analysis(closed_trades: list, log=print) -> list:
    """Analyse each newly-closed trade with Claude.

    Skips EXPIRED trades (no clear directional lesson) unless R > 0 (treat as
    WIN) or R < 0 (treat as LOSS).  Returns list of pattern strings added to
    memory this run.
    """
    new_patterns = []

    for trade in closed_trades:
        rec_id = trade.get("id")
        pair   = trade.get("pair", "?")
        status = (trade.get("status") or "").upper()

        try:
            # Resolve effective status for EXPIRED trades.
            if status == "EXPIRED":
                try:
                    r_val = float(trade.get("r_multiple") or 0)
                except (TypeError, ValueError):
                    r_val = 0.0
                if r_val == 0.0:
                    log(f"  Outcome analysis — #{rec_id} {pair} EXPIRED at breakeven, skipping.")
                    continue
                effective_status = "WIN" if r_val > 0 else "LOSS"
                log(f"  Outcome analysis — #{rec_id} {pair} EXPIRED "
                    f"(treating as {effective_status} at expiry)")
            elif status in ("WIN", "LOSS"):
                effective_status = status
                log(f"  Outcome analysis — #{rec_id} {pair} {status}")
            else:
                continue

            if _already_analysed(rec_id):
                log(f"  #{rec_id} already analysed, skipping.")
                continue

            # Load original report text for richer context.
            rows = tracker.load()
            row = next((r for r in rows if str(r.get("id")) == str(rec_id)), {})
            report_text = ""
            rf = row.get("report_file", "")
            if rf:
                rp = config.REPORTS_DIR / rf
                if rp.exists():
                    report_text = rp.read_text(encoding="utf-8")

            # Call Claude.
            if effective_status == "WIN":
                prompt = _win_prompt(row, report_text)
                dest   = config.WIN_ANALYSIS_FILE
            else:
                prompt = _loss_prompt(row, report_text)
                dest   = config.LOSS_ANALYSIS_FILE

            analysis = _call_claude(prompt)
            pattern  = _extract_pattern(analysis)

            # Persist full analysis.
            records = _load_json(dest)
            records.append({
                "id":          rec_id,
                "pair":        pair,
                "status":      status,
                "r_multiple":  trade.get("r_multiple"),
                "analysis":    analysis,
                "pattern_rule": pattern,
                "analysed_at": _now(),
            })
            _save_json(dest, records)

            # Add pattern to memory.
            if pattern and not _is_near_duplicate(pattern):
                _add_to_memory(pattern, status, pair, trade.get("closed_at", ""))
                new_patterns.append(pattern)
                log(f"  Pattern learned: {pattern[:80]}{'...' if len(pattern) > 80 else ''}")

            # Update estimated account balance in risk profile.
            try:
                from src import risk_manager
                risk_manager.update_balance_from_outcome(trade)
            except Exception:
                pass

        except Exception as exc:
            log(f"  Outcome analysis failed for #{rec_id} {pair}: {exc}")

    return new_patterns
