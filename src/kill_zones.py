"""Kill zone timing system — identifies highest-probability trading windows.

Kill zones correspond to the opening hours of major forex sessions, defined
in Auckland (New Zealand) time.  Signals that fire inside a kill zone aligned
to their currency pair receive a confidence boost; signals outside all
relevant kill zones receive a small penalty.

Confidence adjustments:
  +0.5  signal fires during a kill zone aligned with the pair's currencies
  −0.5  signal fires outside all kill zones relevant to that pair

Auckland timezone is resolved via zoneinfo (Python 3.9+) with a UTC+13
fallback for environments without the tzdata package (e.g. Windows dev).
"""

from datetime import datetime

try:
    from zoneinfo import ZoneInfo as _ZI
    _AKL_TZ = _ZI("Pacific/Auckland")
except Exception:
    from datetime import timezone as _tz, timedelta as _td
    _AKL_TZ = _tz(_td(hours=13))  # approximate NZDT fallback

# Kill zone definitions — times are Auckland local, currencies=None means all pairs
_ZONES = [
    {
        "key":          "LONDON",
        "start":        (19, 0),
        "end":          (20, 30),
        "currencies":   {"EUR", "GBP", "CHF"},
        "label":        "London kill zone",
        "session_desc": (
            "EUR GBP CHF pairs are in their highest probability window "
            "right now — optimal entry timing"
        ),
    },
    {
        "key":          "OVERLAP",
        "start":        (1, 0),
        "end":          (4, 0),
        "currencies":   None,   # all pairs benefit from London/NY overlap
        "label":        "London/New York overlap kill zone",
        "session_desc": (
            "all major pairs are in their highest probability window "
            "right now — optimal entry timing"
        ),
    },
    {
        "key":          "NEW_YORK",
        "start":        (1, 0),
        "end":          (2, 30),
        "currencies":   {"USD", "CAD"},
        "label":        "New York kill zone",
        "session_desc": (
            "USD CAD pairs are in their highest probability window "
            "right now — optimal entry timing"
        ),
    },
    {
        "key":          "TOKYO",
        "start":        (12, 0),
        "end":          (13, 30),
        "currencies":   {"AUD", "NZD", "JPY"},
        "label":        "Tokyo kill zone",
        "session_desc": (
            "AUD NZD JPY pairs are in their highest probability window "
            "right now — optimal entry timing"
        ),
    },
    {
        "key":          "LONDON_CLOSE",
        "start":        (3, 30),
        "end":          (4, 30),
        "currencies":   None,   # mean reversion — all pairs
        "label":        "London close kill zone",
        "session_desc": (
            "mean reversion setups are in their highest probability window "
            "right now — optimal entry timing"
        ),
    },
]

# Priority when multiple zones are simultaneously active
_PRIORITY = ["OVERLAP", "LONDON_CLOSE", "NEW_YORK", "LONDON", "TOKYO"]


def _now_akl() -> datetime:
    return datetime.now(_AKL_TZ)


def _in_range(h: int, m: int, start: tuple, end: tuple) -> bool:
    """True if (h, m) falls within [start, end)."""
    cur = h * 60 + m
    s   = start[0] * 60 + start[1]
    e   = end[0]   * 60 + end[1]
    if s <= e:
        return s <= cur < e
    # Spans midnight
    return cur >= s or cur < e


def _active_zones(now: datetime) -> list:
    return [z for z in _ZONES if _in_range(now.hour, now.minute, z["start"], z["end"])]


def _aligned_zone(base: str, quote: str, active: list):
    """Return the highest-priority active zone that covers this pair, or None."""
    ccys = {base.upper(), quote.upper()}
    by_key = {z["key"]: z for z in active}
    for key in _PRIORITY:
        z = by_key.get(key)
        if z is None:
            continue
        if z["currencies"] is None or ccys & z["currencies"]:
            return z
    return None


def _minutes_until(target_h: int, target_m: int, now_total: int) -> int:
    """Minutes from now_total until target time, wrapping through midnight (1..1440)."""
    target = target_h * 60 + target_m
    diff   = (target - now_total) % (24 * 60)
    return diff if diff > 0 else 24 * 60


def _next_relevant_zone(base: str, quote: str, now: datetime):
    """Return (minutes_until: int, zone: dict) for the next zone relevant to this pair."""
    ccys = {base.upper(), quote.upper()}
    now_total = now.hour * 60 + now.minute
    candidates = []
    for z in _ZONES:
        if z["currencies"] is not None and not (ccys & z["currencies"]):
            continue
        mins = _minutes_until(z["start"][0], z["start"][1], now_total)
        candidates.append((mins, z))
    if not candidates:
        return None, None
    candidates.sort(key=lambda x: x[0])
    return candidates[0]


def _fmt_duration(minutes: int) -> str:
    h = minutes // 60
    m = minutes % 60
    if h == 0:
        return f"{m}m"
    if m == 0:
        return f"{h}h"
    return f"{h}h {m}m"


def check(base: str, quote: str, now: datetime = None) -> dict:
    """Evaluate kill zone timing for a pair.

    Returns:
        {
          "zone_key":     str | None   — active zone key (e.g. "LONDON") or None
          "zone_label":   str | None
          "aligned":      bool         — True = inside a relevant kill zone
          "conf_delta":   float        — +0.5 (aligned) or -0.5 (outside)
          "display_line": str          — ⏰ Kill zone: ... line for Telegram
        }
    """
    if now is None:
        now = _now_akl()

    active       = _active_zones(now)
    zone         = _aligned_zone(base, quote, active)

    if zone:
        return {
            "zone_key":     zone["key"],
            "zone_label":   zone["label"],
            "aligned":      True,
            "conf_delta":   0.5,
            "display_line": (
                f"⏰ Kill zone: ✅ {zone['label']} active — "
                f"{zone['session_desc']}"
            ),
        }

    # Outside — show next relevant zone
    next_mins, next_zone = _next_relevant_zone(base, quote, now)
    if next_zone and next_mins:
        next_str = f"{next_zone['label']} in {_fmt_duration(next_mins)}"
    else:
        next_str = "unknown"
    return {
        "zone_key":     None,
        "zone_label":   None,
        "aligned":      False,
        "conf_delta":   -0.5,
        "display_line": (
            f"⏰ Kill zone: ⚠️ Outside optimal trading window — "
            f"this signal appeared during low probability hours — "
            f"next optimal window: {next_str} — consider waiting"
        ),
    }
