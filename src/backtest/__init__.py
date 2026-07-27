"""Backtesting sandbox — isolated, read-only, manual-only.

Tests "what if" hypotheses against already-logged historical trade outcomes
(research_trades.csv / trades.csv). Never writes to any file the live system
reads or writes; all output goes to data/sandbox/. Never imports daily.py,
monitor.py, analyst.py, discord_notifier.py, or telegram_alert.py — it is
structurally incapable of placing a trade or sending an alert because it
never calls into that code at all.

Not wired into any scheduled workflow. Run via:
    python -m src.backtest.cli --hypothesis <path/to/hypothesis.yaml>

Scope (Tier 1 only — see design proposal): every hypothesis must be
expressible as a filter/rule over fields already captured at trade-entry
time. Testing a genuinely new indicator that was never logged would require
re-fetching historical price data, which this module does not do.
"""
