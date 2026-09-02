# Known dormant / orphaned code

Entries here are code that `scripts/check_orphans.py` would otherwise flag,
but that is currently, deliberately unused for a documented reason. Anything
orphaned that is NOT listed here shows up as a visible flag when the checker
runs (manually, or via `tests/test_orphans.py` in the regression suite).

This list is not a place to bury things you haven't looked at -- add an
entry only when you know *why* something is dormant and *what* (if
anything) is expected to unorphan it. Remove the entry once that happens;
a stale allowlist entry just hides a regression.

Format (parsed by `load_allowlist()` in check_orphans.py):
  `- FIELD: name (path/to/file.py)`
  `- FUNCTION: name (path/to/file.py)`
  `- CLASS: name (path/to/file.py)`
  `- PARAMETER: func_name.param_name (path/to/file.py)`
  `- FILE: path/to/file.py`   (suppresses everything found in that one file)

---

## shadow_mode.py -- mid-rollout, Phase 04 not yet built

Built 2026-09-01 as the framework for evaluating future grading rules
without affecting live decisions. Nothing calls it yet because the daily.py
integration point ("Phase 04": wire `record_evaluation()` into the real
scan/close path) hasn't been built. This is expected to stay orphaned until
Phase 04 ships -- remove these entries once it does, since a Phase 04 that
silently failed to wire in would otherwise look identical to "not built
yet" from this checker's point of view.

- FUNCTION: register_rule (src/shadow_mode.py)
- FUNCTION: record_evaluation (src/shadow_mode.py)
- FUNCTION: check_promotion_readiness (src/shadow_mode.py)
- FUNCTION: list_rules (src/shadow_mode.py)
- FUNCTION: mark_promoted (src/shadow_mode.py)

## broker.py -- confirmed stub, no live-trading integration exists

Documented repeatedly this session: `LIVE_TRADING` defaults `False`, is
never set in any of the 12 GitHub Actions workflows, and the module is
never imported anywhere -- this is a pure paper/simulated system. These
functions are the intended future live-order interface; they'll unorphan
the day real broker execution is actually wired in, which is a deliberate
future project, not an oversight.

- FUNCTION: get_spread_pips (src/broker.py)
- FUNCTION: simulate_entry_slippage (src/broker.py)
- FUNCTION: get_live_balance (src/broker.py)
- FUNCTION: place_order (src/broker.py)
- FUNCTION: close_order (src/broker.py)

## _archived_partial_profit_checker.py -- superseded, kept for reference

Filename-prefixed `_archived_` deliberately -- this is the pre-cascade
partial-profit system, superseded by the current T1/T2/T3 cascade logic
(src/cascade.py) and kept in the tree only as historical reference, not as
live code. Every function in it is expected to be orphaned.

- FILE: src/_archived_partial_profit_checker.py

## risk_manager.py::risk_dashboard_lines() -- likely superseded, revisit if Telegram risk reporting is ever revived

A complete "RISK DASHBOARD" Telegram section (balance, win rate, risk/trade,
exposure, streak, drawdown banner). Its siblings drawdown_header_line() and
drawdown_tier_alert_lines() in the same file are both live. Content
substantially overlaps what monitor.py's Discord dashboard embed
(build_fund_dashboard_embed) already shows -- most likely superseded by
that richer Discord view rather than forgotten, but not certain enough to
delete outright. 2026-09-02 triage decision: leave as-is, revisit only if
Telegram (as opposed to Discord) risk reporting becomes a priority again.

- FUNCTION: risk_dashboard_lines (src/risk_manager.py)

## billing.py::build_credit_section() -- real feature, lower-priority than the working inline version

A well-designed CREDIT BALANCE section (backup-key display, low-balance
urgency framing) exists here, but daily.py renders credit balance with a
simpler inline one-liner instead (`💳 $X.XX combined (N days)`) and never
calls this function. Not broken, just using the plainer path. 2026-09-02
triage decision: leave as-is -- swapping to the fuller version is a real
but low-priority content upgrade, not a bug fix.

- FUNCTION: build_credit_section (src/billing.py)

## pair_statistics.py / regime_learning.py accessor duplication -- real drift risk, not urgent

get_adaptive_targets()/count_needed() (pair_statistics.py) and
get_regime_win_rate()/get_time_win_rate() (regime_learning.py) are clean
accessor functions, but every real call site (daily.py, 3 locations)
bypasses them and reads the underlying JSON stores directly instead --
one call site even hardcodes the same `>= 10` threshold
get_adaptive_targets() already encapsulates. Not missing functionality,
just duplicated logic with a real risk of the two copies drifting apart
over time. 2026-09-02 triage decision: leave as-is, revisit if that drift
risk ever actually bites (e.g. the threshold gets changed in one place
and not the other).

- FUNCTION: get_adaptive_targets (src/pair_statistics.py)
- FUNCTION: count_needed (src/pair_statistics.py)
- FUNCTION: get_regime_win_rate (src/regime_learning.py)
- FUNCTION: get_time_win_rate (src/regime_learning.py)

## Parameters deliberately dead by design -- pure 2:1 TP-or-SL system retired the 3-target cascade

cascade.py's own module docstring: "Pure 2:1 risk:reward target system...
t1_price = (unused for new trades; column preserved for historical
display)... t3_price = (unused)." ATR/adaptive-multiplier params on
compute_levels() are explicitly documented as "accepted but ignored";
t3_hit() is documented as "Always False -- no T3 in the pure TP-or-SL
system." send_fund_milestone()'s t1/t3/t1_hit/t2_hit/t3_hit params are the
same story one layer up -- the function only has 2 real outcomes (trail-to-
breakeven at T1, full win at T2) under the current system. None of these
are gaps; they're signature compatibility with the retired 3-target
design, kept so old call patterns don't need touching.

- PARAMETER: compute_levels.atr (src/cascade.py)
- PARAMETER: compute_levels.t1_mult (src/cascade.py)
- PARAMETER: compute_levels.t2_mult (src/cascade.py)
- PARAMETER: compute_levels.t3_mult (src/cascade.py)
- PARAMETER: t3_hit.price (src/cascade.py)
- PARAMETER: t3_hit.row (src/cascade.py)
- PARAMETER: send_fund_milestone.t1 (src/discord_notifier.py)
- PARAMETER: send_fund_milestone.t3 (src/discord_notifier.py)
- PARAMETER: send_fund_milestone.t1_hit (src/discord_notifier.py)
- PARAMETER: send_fund_milestone.t2_hit (src/discord_notifier.py)
- PARAMETER: send_fund_milestone.t3_hit (src/discord_notifier.py)

## Other deliberately-dead parameters, individually verified

- PARAMETER: send_master_scan_report.strategy_start_date (src/discord_notifier.py)
  -- deliberately bypassed: a comment at the top of discord_notifier.py
  explains calculate_fund_state()'s computed value was found unreliable
  (a mislabeled v2 trade skewed it), so the hardcoded V2_STRATEGY_START
  constant is used everywhere instead. Working as intended.
- PARAMETER: send_master_scan_report.ml_accuracy (src/discord_notifier.py)
  -- set to the exact same value as ml_overall_wr (a duplicate alias),
  which the message already displays under that name. Nothing missing.
- PARAMETER: compute_costs.lots (src/trade_costs.py)
  -- this function returns a cost breakdown in PIPS, which doesn't scale
  with position size; only a dollar conversion would need lots.
- PARAMETER: _classify_candle.direction (src/feature_extractor.py)
  -- candle-shape classification (PIN_BAR/ENGULFING/etc.) is a property
  of the OHLC data alone, not the trade's intended direction.
- PARAMETER: extract.pair (src/feature_extractor.py)
  -- the ML feature set is deliberately pair-agnostic to keep the model
  generalizable rather than overfit to specific instruments.
- PARAMETER: _send_entry_confirmed_alert.log_fn (src/discord_notifier.py)
  -- optional logging callback, never actually invoked; cosmetic only.
- PARAMETER: _send_fallback_alerts.log (src/monitor.py)
  -- same as above, cosmetic only.
- PARAMETER: drawdown_header_line.profile (src/risk_manager.py)
  -- the function only needs risk_state; profile is accepted but unused,
  cosmetic only.
- PARAMETER: send_master_scan_report.fund_win_rate (src/discord_notifier.py)
  -- 2026-09-02, found while wiring up fund_wins/fund_protected/fund_losses
  into a new "Breakdown: N full win / N protected win / N loss" line (same
  batch as the entries above): fund_win_rate is numerically identical to
  v2_win_rate, which the existing win-rate line already displays -- adding
  it again would just repeat the same percentage under a second label with
  no new information, unlike fund_wins/fund_protected/fund_losses which add
  the full-vs-protected split. Left unused deliberately.
