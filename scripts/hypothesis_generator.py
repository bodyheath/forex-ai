"""Offline, one-shot causal-hypothesis generation for this session's confirmed
statistical edges (rib_strongly_against non-GBP, the CHF-cluster exclusion,
the Improvement-4 weekly/ribbon overlap).

NOT wired into daily.py or any live pipeline. This script:
  1. Reads already-committed data/research_trades.csv.
  2. Re-derives real sample rows for each edge from the persisted fields
     (pair, direction, ribbon_state, status, r_multiple) using the exact
     strict/v2/decisive accounting standard used throughout this session's
     investigations, so Sonnet reasons from real trade context, not a
     restated summary.
  3. Calls Sonnet once per edge, asking for an explicit, falsifiable
     hypothesis for WHY the pattern might hold -- plus a concrete
     "would stop being true if X" invalidation clause, not just a
     restatement of the statistics.
  4. Writes the result to docs/edge_hypotheses.md for human review.

This is a research/documentation artifact, not a decision input. Nothing it
produces is read by daily.py, tracker.py, or any grading/confidence logic.

Run manually:  python scripts/hypothesis_generator.py
"""

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from anthropic import Anthropic

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from src import research_tracker

WIN_STATUSES  = {"WIN", "FULL_WIN", "PARTIAL_WIN"}
LOSS_STATUSES = {"LOSS"}
# Same strict-accounting cutoff used throughout this session's re-verifications:
# the exit-logic fix landed 2026-07-14 13:46:31 UTC; anything closed before that
# used the old (confirmed buggy) exit accounting and is excluded.
EXIT_LOGIC_FIX_UTC = datetime(2026, 7, 14, 13, 46, 31, tzinfo=timezone.utc)


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _closed_at(row):
    ts = row.get("closed_at") or ""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(ts[:19], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _strict_decisive(rows):
    """Strict/v2/decisive filter used throughout this session's re-verifications."""
    out = []
    for r in rows:
        if r.get("system_version") != "v2":
            continue
        if r.get("status") not in (WIN_STATUSES | LOSS_STATUSES):
            continue
        ts = _closed_at(r)
        if ts is None or ts < EXIT_LOGIC_FIX_UTC:
            continue
        out.append(r)
    return out


def _stats(rows):
    n = len(rows)
    if n == 0:
        return {"n": 0, "wr": None, "pf": None}
    wins = [r for r in rows if r["status"] in WIN_STATUSES]
    wr = len(wins) / n
    gains = sum(_to_float(r.get("r_multiple")) or 0 for r in rows if (_to_float(r.get("r_multiple")) or 0) > 0)
    losses = sum(-(_to_float(r.get("r_multiple")) or 0) for r in rows if (_to_float(r.get("r_multiple")) or 0) < 0)
    pf = (gains / losses) if losses > 0 else None
    return {"n": n, "wr": wr, "pf": pf}


def _rib_strongly_against(r):
    direction = (r.get("direction") or "").upper()
    ribbon    = r.get("ribbon_state") or ""
    return (
        (direction == "BUY"  and ribbon == "ALIGNED_BEAR") or
        (direction == "SELL" and ribbon == "ALIGNED_BULL")
    )


def _is_gbp_cross(pair):
    return "GBP" in (pair or "").upper()


def _sample_lines(rows, fields, limit=5):
    lines = []
    for r in rows[:limit]:
        lines.append(", ".join(f"{f}={r.get(f, '')}" for f in fields))
    return lines


def _build_edges(all_rows):
    decisive = _strict_decisive(all_rows)

    rib_non_gbp = [r for r in decisive if _rib_strongly_against(r) and not _is_gbp_cross(r.get("pair"))]
    chf_cluster = [r for r in rib_non_gbp if (r.get("pair") or "").upper() in ("EUR/CHF", "NZD/CHF", "AUD/CHF")]
    rib_non_gbp_ex_chf = [r for r in rib_non_gbp if r not in chf_cluster]

    fields = ["pair", "direction", "ribbon_state", "grade", "confidence", "market_regime", "status", "r_multiple"]

    edges = []

    s = _stats(rib_non_gbp)
    edges.append({
        "name": "rib_strongly_against (non-GBP)",
        "population": (
            "Trade candidates where the setup direction is strongly opposed by the "
            "daily EMA ribbon (BUY against an ALIGNED_BEAR ribbon, or SELL against an "
            "ALIGNED_BULL ribbon), excluding any GBP cross. Live re-derivation from "
            "data/research_trades.csv, strict/v2/decisive accounting "
            "(system_version=='v2', status in WIN/FULL_WIN/PARTIAL_WIN/LOSS, "
            "closed_at >= 2026-07-14 13:46:31 UTC exit-logic fix)."
        ),
        "stats_text": f"n={s['n']}, win rate={s['wr']*100:.1f}%, profit factor={s['pf']:.2f}" if s["n"] else "n=0",
        "samples": _sample_lines(rib_non_gbp, fields),
    })

    s = _stats(chf_cluster)
    edges.append({
        "name": "CHF-cluster exclusion (within rib_strongly_against non-GBP)",
        "population": (
            "The subset of the above population restricted to EUR/CHF, NZD/CHF, and "
            "AUD/CHF -- a net-loser cluster found by splitting the non-GBP "
            "rib_strongly_against population by pair. Same strict/v2/decisive filter."
        ),
        "stats_text": f"n={s['n']}, win rate={s['wr']*100:.1f}%, profit factor={s['pf']:.2f}" if s["n"] else "n=0 in current data (originally confirmed at n=61)",
        "samples": _sample_lines(chf_cluster, fields),
        "comparison": (
            f"For contrast, the SAME population excluding CHF pairs "
            f"(n={_stats(rib_non_gbp_ex_chf)['n']}, "
            f"win rate={(_stats(rib_non_gbp_ex_chf)['wr'] or 0)*100:.1f}%, "
            f"profit factor={_stats(rib_non_gbp_ex_chf)['pf'] or 0:.2f}) is a confirmed edge."
        ),
    })

    # Improvement-4 overlap: "weekly-opposed" is not a persisted field
    # (get_weekly_trend()'s output was confirmed, separately this session, to
    # never actually reach research_trades.csv -- the weekly_trend/
    # weekly_trend_at_entry keys silently fall through to "" everywhere).
    # Not re-derivable from committed data; described from the already-cited,
    # already-verified backtest instead of a fresh live sample.
    edges.append({
        "name": "Improvement-4 overlap (weekly-trend hard block vs. ribbon carve-out)",
        "population": (
            "Non-GBP candidates that are BOTH rib_strongly_against AND opposed by the "
            "weekly trend filter (Improvement 4's hard block). This is a text-only "
            "description, not a fresh live query: the underlying weekly-trend field "
            "is computed at scan time via get_weekly_trend() but is not persisted to "
            "research_trades.csv (confirmed separately this session), so this "
            "population cannot be re-derived from committed data alone. The figures "
            "below are the already-verified backtest cited in daily.py's own code "
            "comments (2026-08-26 investigation, real yfinance weekly bars fed "
            "through the exact get_weekly_trend() formula, Sonnet-sourced only)."
        ),
        "stats_text": (
            "Weekly-opposed AND rib_strongly_against, non-GBP: n=23, win rate=73.9%, "
            "profit factor=6.606 (p<0.00001 vs the strict aggregate win rate of "
            "25.52%). Weekly-opposed WITHOUT rib_strongly_against (still hard-blocked "
            "today): n=10, win rate=20.0%, profit factor=0.320 -- confirming the "
            "block is correctly excluding a genuinely different, weaker population."
        ),
        "samples": [],
    })

    return edges


_SYSTEM_PROMPT = """You are reviewing confirmed statistical findings from a live forex \
trading system's trade history. For each finding, you will be given: a precise \
population definition, real summary statistics, and (where available) a small \
sample of actual trades matching that population.

Your task is NOT to restate the statistics. Generate ONE specific, falsifiable \
causal hypothesis for WHY this pattern might exist in real market structure -- \
a candidate mechanism, not a description of the correlation itself.

Then state an explicit INVALIDATION_CONDITION: a concrete, observable change in \
market structure or behavior that would mean your hypothesis is wrong and the \
edge should be expected to decay or reverse. This must name something checkable \
(a specific driver, flow, or regime shift), not a vague hedge like "if the \
pattern stops working" or "if market conditions change".

Be honest that this is a candidate explanation for a correlation, not a proven \
mechanism -- do not overstate confidence.

Respond in exactly this format, nothing else:
HYPOTHESIS: <one to three sentences>
INVALIDATION_CONDITION: <one to two sentences, naming a specific checkable driver>
"""


def _build_user_message(edge):
    parts = [
        f"FINDING: {edge['name']}",
        f"POPULATION: {edge['population']}",
        f"STATS: {edge['stats_text']}",
    ]
    if edge.get("comparison"):
        parts.append(f"COMPARISON: {edge['comparison']}")
    if edge["samples"]:
        parts.append("SAMPLE TRADES (real, from this population):")
        parts.extend(f"  - {s}" for s in edge["samples"])
    else:
        parts.append("SAMPLE TRADES: none available in current committed data for this specific finding.")
    return "\n".join(parts)


def _parse_response(text):
    hyp = re.search(r"HYPOTHESIS:\s*(.+?)(?=\nINVALIDATION_CONDITION:|\Z)", text, re.DOTALL)
    inv = re.search(r"INVALIDATION_CONDITION:\s*(.+)", text, re.DOTALL)
    return {
        "hypothesis": hyp.group(1).strip() if hyp else "",
        "invalidation_condition": inv.group(1).strip() if inv else "",
        "raw": text.strip(),
    }


def generate(edges, log=print):
    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    results = []
    for edge in edges:
        log(f"Generating hypothesis for: {edge['name']} ...")
        user_message = _build_user_message(edge)
        resp = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=400,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        parsed = _parse_response(text)
        if not parsed["hypothesis"] or not parsed["invalidation_condition"]:
            log(f"  WARNING: could not parse expected fields from response for {edge['name']!r}")
        results.append({"edge": edge, "user_message": user_message, **parsed})
    return results


def render_markdown(results) -> str:
    lines = [
        "# Edge Hypotheses",
        "",
        "Generated by `scripts/hypothesis_generator.py`. These are candidate causal "
        "explanations for confirmed statistical edges found this session -- "
        "**unvalidated hypotheses, not confirmed mechanisms.** They exist to give "
        "each edge a checkable invalidation condition, not to be treated as proven.",
        "",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
    ]
    for r in results:
        edge = r["edge"]
        lines.append(f"## {edge['name']}")
        lines.append("")
        lines.append(f"**Population:** {edge['population']}")
        lines.append("")
        lines.append(f"**Stats:** {edge['stats_text']}")
        if edge.get("comparison"):
            lines.append("")
            lines.append(f"**Comparison:** {edge['comparison']}")
        lines.append("")
        lines.append(f"**Hypothesis:** {r['hypothesis'] or '(unparsed -- see raw response below)'}")
        lines.append("")
        lines.append(f"**Invalidation condition:** {r['invalidation_condition'] or '(unparsed -- see raw response below)'}")
        if not r["hypothesis"] or not r["invalidation_condition"]:
            lines.append("")
            lines.append(f"<details><summary>Raw response</summary>\n\n```\n{r['raw']}\n```\n\n</details>")
        lines.append("")
    return "\n".join(lines)


def main():
    all_rows = research_tracker.load()
    edges = _build_edges(all_rows)
    results = generate(edges)
    md = render_markdown(results)
    out_path = config.DATA_DIR.parent / "docs" / "edge_hypotheses.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
