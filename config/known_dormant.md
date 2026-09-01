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
