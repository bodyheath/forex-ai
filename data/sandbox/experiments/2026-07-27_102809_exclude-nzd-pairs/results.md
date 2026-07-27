# Backtest experiment: exclude-nzd-pairs

Run: 2026-07-27_102809_exclude-nzd-pairs  (2026-07-27T10:28:09.325816+00:00)

**Description:** What if we never took any NZD-involving pair trade? Tests whether excluding NZD exposure would have improved results in the post-fix strict window.


**Dataset:** `research_v2_strict_postfix` — Research v2 trades, strict outcomes only (true TARGET_HIT/STOP_HIT, PARTIAL_WIN/EXPIRED excluded), closed_at >= the 2026-07-14 exit-logic fix. This is the checkpoint-tracked, current-rules population.
**Data snapshot:** 161 rows, date range ['2026-07-14 15:01:38', '2026-07-27 07:01:38'], source CSV git commit `a6ed9875371ce9478ff41148dd184f64a75fb196`

## Filters / rule applied
```
filters: {'exclude_pair_contains': ['NZD']}
custom_rule: None
```

## Results

**Baseline (full dataset)**: n=161  W=26  L=135  WR=16.15%  PF=0.401  expectancy=-0.495R/trade
  sample size: n=161 — usable sample size, still check the significance test below

**Included (trades that pass the hypothesis)**: n=139  W=22  L=117  WR=15.83%  PF=0.393  expectancy=-0.502R/trade
  sample size: n=139 — usable sample size, still check the significance test below

**Excluded (trades the hypothesis would have skipped)**: n=22  W=4  L=18  WR=18.18%  PF=0.453  expectancy=-0.447R/trade
  sample size: SMALL (n=22 < 50) — directional only, treat with caution

## Statistical context: included vs. excluded

chi2=0.0, dof=1, p=1.0
Minimum expected cell count: 3.55
Validity: INVALID / UNRELIABLE — minimum expected cell (3.55) is below the standard chi-square validity threshold of 5; this p-value should not be trusted at this sample size
Significant at p<0.05: not applicable (invalid test)

## LIMITATIONS — read before trusting this result:

This is an approximation, not a replay of history. It filters ALREADY-LOGGED outcomes by an alternative rule applied after the fact — it cannot reconstruct how downstream gates (devil's-advocate demotion, drawdown filter, correlation/concentration limits, position sizing, etc.) might have behaved differently under the alternative rule, since those gates only ran once, under the actual historical rules, at the actual historical confidence/context values. A trade excluded or included by this hypothesis might, under a real system change, have triggered a different downstream gate entirely (e.g. freed up fund capacity that a different trade would have taken instead). Treat every result below as a DIRECTIONAL ESTIMATE of what the historical outcomes would suggest, not a precise 'this is what would have happened.'
