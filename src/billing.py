"""Anthropic credit balance checker.

Queries the Anthropic billing API to fetch remaining USD credit for each key.
Degrades gracefully — if the endpoint is unavailable or returns an unexpected
format, the balance is None and the Telegram section shows a link to the console.

Endpoint note
-------------
Anthropic may update their billing API paths over time.  The function tries
several known patterns in order.  If all return non-200, verify the current
path at https://docs.anthropic.com and update _ENDPOINTS below.
"""

import requests

_BASE      = "https://api.anthropic.com"
_TIMEOUT   = 10
_VERSION   = "2023-06-01"

# Candidate endpoints tried in order.  The first one to return a 200 with a
# parseable numeric balance wins; the rest are ignored.
_ENDPOINTS = (
    f"{_BASE}/v1/account/billing/credits",
    f"{_BASE}/v1/usage/credits",
    f"{_BASE}/v1/account",
    f"{_BASE}/v1/usage",
)

# Key-paths into the JSON response that might hold the remaining balance.
# Tried in order; the first path that resolves to a float is returned.
_PATHS = (
    ("remaining",),
    ("balance",),
    ("credits", "remaining"),
    ("credits", "balance"),
    ("credits", "available"),
    ("credit_balance",),
    ("available_credits",),
    ("data", "remaining"),
    ("data", "credits", "remaining"),
)


def fetch_balance(api_key: str) -> float | None:
    """Return remaining USD credit for api_key, or None if unavailable.

    Makes sequential GET requests to known Anthropic billing endpoints until
    one returns a parseable numeric balance.  All network and parse errors
    are caught so this function never raises.
    """
    if not api_key:
        return None

    headers = {
        "x-api-key":         api_key,
        "anthropic-version": _VERSION,
    }

    for url in _ENDPOINTS:
        try:
            resp = requests.get(url, headers=headers, timeout=_TIMEOUT)
            if resp.status_code not in (200, 206):
                continue
            data = resp.json()
            for path in _PATHS:
                node = data
                for key in path:
                    node = node.get(key) if isinstance(node, dict) else None
                if isinstance(node, (int, float)):
                    return float(node)
        except Exception:  # noqa: BLE001
            continue

    return None


def build_credit_section(
    primary_balance: float | None,
    backup_balance:  float | None,
    primary_name:    str,
    backup_name:     str,
    active_key:      str,
    daily_cost_usd:  float = 0.05,
    has_backup_key:  bool  = False,
) -> list:
    """Return Telegram-formatted lines for the CREDIT BALANCE section.

    Args:
        primary_balance:  USD remaining on primary key, or None if unavailable.
        backup_balance:   USD remaining on backup key, or None if unavailable.
        primary_name:     Display name for the primary account (e.g. 'Heath').
        backup_name:      Display name for the backup account (e.g. 'Partner').
        active_key:       'Primary' or 'Backup' — which key was used this run.
        daily_cost_usd:   Estimated cost per daily run in USD (for runway calc).
        has_backup_key:   True if ANTHROPIC_API_KEY_2 is configured.
    """
    def _fmt(bal: float | None) -> str:
        if bal is None:
            return "unavailable — check console.anthropic.com"
        return f"<b>${bal:.2f} remaining</b>"

    lines = [
        "",
        "━━━━━━━━━━━━━━━━━━━━━",
        "💳 <b>CREDIT BALANCE</b>",
        f"Primary account ({primary_name}): {_fmt(primary_balance)}",
    ]

    if has_backup_key:
        lines.append(f"Backup account ({backup_name}): {_fmt(backup_balance)}")

    # Runway: total remaining ÷ daily cost
    total = (primary_balance or 0.0) + (backup_balance or 0.0 if has_backup_key else 0.0)
    if total > 0 and daily_cost_usd > 0:
        runway = int(total / daily_cost_usd)
        lines.append(f"Total runway: <b>~{runway:,} days</b> at current usage")

    lines.append(f"Active key: <b>{active_key}</b>")

    # ── Warnings ──
    primary_low = primary_balance is not None and primary_balance < 5.0
    backup_low  = backup_balance  is not None and backup_balance  < 5.0
    no_backup   = not has_backup_key

    if primary_low and (backup_low or no_backup):
        lines.append(
            "🚨 <b>URGENT — Both accounts low, top up needed immediately.</b>"
        )
    elif primary_low:
        lines.append("⚠️ Primary account low — will switch to backup soon.")
    elif has_backup_key and backup_low:
        lines.append("⚠️ Backup account low — top up before primary runs out.")

    return lines
