"""Sentiment layer: pulls recent headlines per currency from NewsAPI.

We deliberately do NOT score sentiment with a naive keyword model here - we hand
the raw headlines to Claude, which assesses tone far more reliably. A lightweight
volume/recency summary is included as a hint.
"""

import requests

import config
from src import cache

_URL = "https://newsapi.org/v2/everything"
_TIMEOUT = 30
_MAX_HEADLINES = 8


def _headlines_for(ccy: str) -> dict:
    meta = config.CURRENCIES.get(ccy, {})
    terms = meta.get("news_terms") or ccy
    key = f"NEWS:{ccy}:{terms}"
    cached = cache.get(key, ttl_hours=12.0)
    if cached is None:
        try:
            resp = requests.get(
                _URL,
                params={
                    "q": terms,
                    "language": "en",
                    "sortBy": "publishedAt",
                    "pageSize": _MAX_HEADLINES,
                    "apiKey": config.NEWS_API_KEY,
                },
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            payload = resp.json()
            cached = [
                {
                    "title": a.get("title"),
                    "source": (a.get("source") or {}).get("name"),
                    "published": a.get("publishedAt"),
                    "desc": (a.get("description") or "")[:200],
                }
                for a in payload.get("articles", [])
            ]
            cache.set(key, cached)
        except Exception as exc:  # noqa: BLE001
            return {"currency": ccy, "status": "UNAVAILABLE", "error": str(exc)}

    if not cached:
        return {"currency": ccy, "status": "no recent articles"}
    return {"currency": ccy, "status": "ok", "article_count": len(cached), "headlines": cached}


def analyse(base: str, quote: str) -> dict:
    return {
        "status": "ok",
        "base": _headlines_for(base),
        "quote": _headlines_for(quote),
        "note": (
            "Retail-sentiment positioning feed is not wired in (no free API). "
            "Treat the contrarian retail-extreme check as UNAVAILABLE."
        ),
    }
