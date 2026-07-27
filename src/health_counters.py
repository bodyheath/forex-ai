"""In-process tally of silent-failure events for the scan report's System
Health section.

Not persisted across runs — each GitHub Actions invocation is a fresh
process, so counts naturally reflect "this scan" without needing an
explicit reset between runs. reset() exists for clarity/safety at the top
of daily.py's run() in case the module is ever imported more than once
within a single process.

Categories recorded (matching the scan report's System Health line):
  mfe_mae         — MFE/MAE update failures (research or fund)
  t2_price        — t2_price lazy-init failures (research)
  ghost_trade     — fund-state loop reconciliation mismatches
  sonnet_max_tokens — Sonnet confirmation hit the max_tokens ceiling
"""

_events: dict = {}


def reset() -> None:
    _events.clear()


def record(category: str, detail: str = "") -> None:
    _events.setdefault(category, []).append(detail)


def counts() -> dict:
    """{category: count} for every category that fired at least once."""
    return {k: len(v) for k, v in _events.items()}


def details() -> dict:
    """{category: [detail, ...]} — full detail strings, for logging."""
    return {k: list(v) for k, v in _events.items()}


def total() -> int:
    return sum(len(v) for v in _events.values())
