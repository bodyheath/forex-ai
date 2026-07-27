# Backtest experiment: confidence-reweight-min-6

Run: 2026-07-27_102817_confidence-reweight-min-6  (2026-07-27T10:28:17.626613+00:00)

**Description:** What if the minimum confidence required to take a trade was 6, instead of the current gate (effectively 4)? Tests whether raising the floor and dropping the 4-5 confidence bands would have improved results in the post-fix strict window.


**Dataset:** `research_v2_strict_postfix` — Research v2 trades, strict outcomes only (true TARGET_HIT/STOP_HIT, PARTIAL_WIN/EXPIRED excluded), closed_at >= the 2026-07-14 exit-logic fix. This is the checkpoint-tracked, current-rules population.
**Data snapshot:** 161 rows, date range ['2026-07-14 15:01:38', '2026-07-27 07:01:38'], source CSV git commit `a6ed9875371ce9478ff41148dd184f64a75fb196`

## Filters / rule applied
```
filters: {'min_confidence': 6}
custom_rule: None
```

## Results

**Baseline (full dataset)**: n=161  W=26  L=135  WR=16.15%  PF=0.401  expectancy=-0.495R/trade
  sample size: n=161 — usable sample size, still check the significance test below

**Included (trades that pass the hypothesis)**: n=91  W=12  L=79  WR=13.19%  PF=0.317  expectancy=-0.578R/trade
  sample size: n=91 — usable sample size, still check the significance test below

**Excluded (trades the hypothesis would have skipped)**: n=70  W=14  L=56  WR=20.0%  PF=0.516  expectancy=-0.387R/trade
  sample size: n=70 — usable sample size, still check the significance test below

## Statistical context: included vs. excluded

chi2=0.8998, dof=1, p=0.34283
Minimum expected cell count: 11.3
Validity: valid — all expected cells >= 5
Significant at p<0.05: False

## LIMITATIONS — read before trusting this result:

This is an approximation, not a replay of history. It filters ALREADY-LOGGED outcomes by an alternative rule applied after the fact — it cannot reconstruct how downstream gates (devil's-advocate demotion, drawdown filter, correlation/concentration limits, position sizing, etc.) might have behaved differently under the alternative rule, since those gates only ran once, under the actual historical rules, at the actual historical confidence/context values. A trade excluded or included by this hypothesis might, under a real system change, have triggered a different downstream gate entirely (e.g. freed up fund capacity that a different trade would have taken instead). Treat every result below as a DIRECTIONAL ESTIMATE of what the historical outcomes would suggest, not a precise 'this is what would have happened.'
