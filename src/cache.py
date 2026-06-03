"""Tiny on-disk cache for HTTP responses.

Keyed by a caller-supplied string; values are JSON-serialisable. Entries expire
after CACHE_TTL_HOURS. This exists primarily to protect the Alpha Vantage free
tier (~25 requests/day) from being burned by repeated runs.
"""

import hashlib
import json
import time
from pathlib import Path

import config


def _path_for(key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return config.CACHE_DIR / f"{digest}.json"


def get(key: str, ttl_hours: float = None):
    """Return the cached value for `key`, or None if missing/expired.

    If `ttl_hours` is provided it overrides config.CACHE_TTL_HOURS for this
    specific lookup, allowing callers to request a longer or shorter TTL.
    """
    path = _path_for(key)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    age_hours = (time.time() - payload.get("_cached_at", 0)) / 3600.0
    effective_ttl = ttl_hours if ttl_hours is not None else config.CACHE_TTL_HOURS
    if age_hours > effective_ttl:
        return None
    return payload.get("value")


def set(key: str, value) -> None:
    """Store `value` under `key` with the current timestamp."""
    path = _path_for(key)
    payload = {"_cached_at": time.time(), "_key": key, "value": value}
    try:
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass  # cache failures should never break a run
