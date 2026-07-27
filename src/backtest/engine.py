"""Core experiment runner. Read-only against live data; all output confined
to data/sandbox/experiments/<run_id>/.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import config
from src.backtest import data_loader, metrics
from src.backtest.hypothesis import Hypothesis, build_predicate, load_hypothesis

SANDBOX_DIR = config.ROOT / "data" / "sandbox"
EXPERIMENTS_DIR = SANDBOX_DIR / "experiments"
RUNS_INDEX = SANDBOX_DIR / "runs_index.json"

LIMITATIONS_NOTE = (
    "LIMITATIONS — read before trusting this result:\n"
    "This is an approximation, not a replay of history. It filters ALREADY-LOGGED "
    "outcomes by an alternative rule applied after the fact — it cannot reconstruct "
    "how downstream gates (devil's-advocate demotion, drawdown filter, correlation/"
    "concentration limits, position sizing, etc.) might have behaved differently "
    "under the alternative rule, since those gates only ran once, under the actual "
    "historical rules, at the actual historical confidence/context values. A trade "
    "excluded or included by this hypothesis might, under a real system change, have "
    "triggered a different downstream gate entirely (e.g. freed up fund capacity that "
    "a different trade would have taken instead). Treat every result below as a "
    "DIRECTIONAL ESTIMATE of what the historical outcomes would suggest, not a "
    "precise 'this is what would have happened.'"
)


def _ensure_dirs():
    EXPERIMENTS_DIR.mkdir(parents=True, exist_ok=True)
    if not RUNS_INDEX.exists():
        RUNS_INDEX.write_text("[]", encoding="utf-8")


def _write_sandbox_only(path: Path, content: str):
    """Guard: refuse to write anywhere outside data/sandbox/."""
    resolved = path.resolve()
    if SANDBOX_DIR.resolve() not in resolved.parents and resolved != SANDBOX_DIR.resolve():
        raise RuntimeError(
            f"Refusing to write outside data/sandbox/: {resolved}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def run_experiment(hypothesis_path: str) -> dict:
    hyp = load_hypothesis(hypothesis_path)
    predicate = build_predicate(hyp)

    df = data_loader.load_dataset(hyp.dataset)
    snapshot = data_loader.snapshot_meta(hyp.dataset, df)

    mask = df.apply(predicate, axis=1)
    included = df[mask].copy()
    excluded = df[~mask].copy()

    baseline_stats = metrics.group_stats(df)
    included_stats = metrics.group_stats(included)
    excluded_stats = metrics.group_stats(excluded)
    comparison = metrics.compare_groups(included, excluded)

    ts = datetime.now(timezone.utc)
    run_id = f"{ts.strftime('%Y-%m-%d_%H%M%S')}_{hyp.name}"

    result = {
        "run_id": run_id,
        "timestamp_utc": ts.isoformat(),
        "hypothesis": {
            "name": hyp.name,
            "description": hyp.description,
            "dataset": hyp.dataset,
            "filters": hyp.filters,
            "custom_rule": hyp.custom_rule,
        },
        "snapshot_meta": snapshot,
        "baseline": baseline_stats,
        "included": included_stats,
        "excluded": excluded_stats,
        "comparison_included_vs_excluded": comparison,
        "limitations": LIMITATIONS_NOTE,
    }

    _ensure_dirs()
    out_dir = EXPERIMENTS_DIR / run_id
    hyp_yaml_text = Path(hypothesis_path).read_text(encoding="utf-8")

    from src.backtest import report as report_mod
    _write_sandbox_only(out_dir / "hypothesis.yaml", hyp_yaml_text)
    _write_sandbox_only(out_dir / "results.json", json.dumps(result, indent=2, default=str))
    _write_sandbox_only(out_dir / "results.md", report_mod.render_markdown(result))
    _write_sandbox_only(out_dir / "snapshot_meta.json", json.dumps(snapshot, indent=2, default=str))

    index = json.loads(RUNS_INDEX.read_text(encoding="utf-8"))
    index.append({
        "run_id": run_id,
        "timestamp_utc": ts.isoformat(),
        "name": hyp.name,
        "dataset": hyp.dataset,
        "included_n": included_stats["n"],
        "included_win_rate_pct": included_stats["win_rate_pct"],
        "excluded_n": excluded_stats["n"],
        "excluded_win_rate_pct": excluded_stats["win_rate_pct"],
    })
    _write_sandbox_only(RUNS_INDEX, json.dumps(index, indent=2, default=str))

    return result
