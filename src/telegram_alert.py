"""Shared Telegram sender for background scripts (price checker, partial profit alerts).

Duplicates the send logic from daily._telegram() so standalone scripts
can send alerts without importing the full daily.py pipeline.
"""
import html as _html_mod
import re as _re
import time as _time_mod
import urllib.error
import urllib.parse
import urllib.request

import config


def send(message: str) -> None:
    """Send HTML-mode message to all configured Telegram chat IDs.

    <b> and </b> tags are preserved; all other angle-bracket content is
    entity-escaped so Telegram's HTML parser never returns a 400 error.

    Retries up to 3 times on transient failures: 429 rate limits wait 5/10/15s;
    other network errors wait 2/4s before retrying.
    """
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        print("[TELEGRAM] SKIP — TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not configured")
        return

    _TAG = _re.compile(r'(</?[bi]>)')
    parts = _TAG.split(message)
    safe_msg = "".join(
        p if i % 2 == 1 else _html_mod.escape(p, quote=False)
        for i, p in enumerate(parts)
    )

    recipients = [("primary", config.TELEGRAM_CHAT_ID)]
    if config.TELEGRAM_CHAT_ID_2:
        recipients.append(("secondary", config.TELEGRAM_CHAT_ID_2))
    if config.TELEGRAM_CHAT_ID_3:
        recipients.append(("tertiary", config.TELEGRAM_CHAT_ID_3))

    url     = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage"
    preview = safe_msg[:120].replace("\n", " ")

    for name, chat_id in recipients:
        for attempt in range(3):
            try:
                data = urllib.parse.urlencode({
                    "chat_id":    chat_id,
                    "text":       safe_msg,
                    "parse_mode": "HTML",
                }).encode()
                urllib.request.urlopen(
                    urllib.request.Request(url, data=data), timeout=15
                )
                print(f"[TELEGRAM] Sent to {name} ({len(safe_msg)} chars)")
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")[:200]
                if exc.code == 429 and attempt < 2:
                    wait = (attempt + 1) * 5
                    print(f"[TELEGRAM] Rate limited — waiting {wait}s (attempt {attempt+1}/3) | {name}")
                    _time_mod.sleep(wait)
                    continue
                print(f"[TELEGRAM] FAILED {name}: HTTP {exc.code} | {body} | {preview}")
                break
            except Exception as exc:
                if attempt < 2:
                    _time_mod.sleep(2 ** attempt)
                    continue
                print(f"[TELEGRAM] FAILED {name}: {exc} | {preview}")
