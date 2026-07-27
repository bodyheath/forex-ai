"""Renders an experiment result dict into a human-readable markdown report.

Every report unconditionally includes the limitations note and the
sample-size flags produced by metrics.py — this module has no code path
that can omit them.
"""


def _fmt_group(label: str, g: dict) -> str:
    if g["n"] == 0:
        return f"**{label}**: {g['sample_size_flag']}"
    return (
        f"**{label}**: n={g['n']}  W={g['wins']}  L={g['losses']}  "
        f"WR={g['win_rate_pct']}%  PF={g['profit_factor']}  "
        f"expectancy={g['expectancy_r']}R/trade\n"
        f"  sample size: {g['sample_size_flag']}"
    )


def render_markdown(result: dict) -> str:
    hyp = result["hypothesis"]
    snap = result["snapshot_meta"]
    cmp = result["comparison_included_vs_excluded"]

    lines = []
    lines.append(f"# Backtest experiment: {hyp['name']}")
    lines.append("")
    lines.append(f"Run: {result['run_id']}  ({result['timestamp_utc']})")
    lines.append("")
    lines.append(f"**Description:** {hyp['description']}")
    lines.append("")
    lines.append(f"**Dataset:** `{hyp['dataset']}` — {snap['dataset_description']}")
    lines.append(f"**Data snapshot:** {snap['row_count']} rows, "
                  f"date range {snap['date_range']}, "
                  f"source CSV git commit `{snap['source_csv_git_commit']}`")
    lines.append("")
    lines.append("## Filters / rule applied")
    lines.append(f"```\nfilters: {hyp['filters']}\ncustom_rule: {hyp['custom_rule']}\n```")
    lines.append("")
    lines.append("## Results")
    lines.append("")
    lines.append(_fmt_group("Baseline (full dataset)", result["baseline"]))
    lines.append("")
    lines.append(_fmt_group("Included (trades that pass the hypothesis)", result["included"]))
    lines.append("")
    lines.append(_fmt_group("Excluded (trades the hypothesis would have skipped)", result["excluded"]))
    lines.append("")
    lines.append("## Statistical context: included vs. excluded")
    lines.append("")
    if not cmp.get("applicable"):
        lines.append(f"Not applicable — {cmp.get('reason')}")
    else:
        lines.append(f"chi2={cmp['chi2']}, dof={cmp['dof']}, p={cmp['p_value']}")
        lines.append(f"Minimum expected cell count: {cmp['min_expected_cell']}")
        lines.append(f"Validity: {cmp['validity_note']}")
        lines.append(f"Significant at p<0.05: {cmp['significant_at_0.05']}")
    lines.append("")
    lines.append("## " + result["limitations"].splitlines()[0])
    lines.append("")
    lines.append("\n".join(result["limitations"].splitlines()[1:]))
    lines.append("")

    return "\n".join(lines)
