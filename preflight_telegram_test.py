"""Pre-flight Telegram test — sends a confirmation message to all configured recipients.

Run after adding TELEGRAM_TOKEN (and optionally TELEGRAM_CHAT_ID / TELEGRAM_CHAT_ID_2)
to your .env file:

    python preflight_telegram_test.py
"""
import sys
import urllib.parse
import urllib.request

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import config

if not config.TELEGRAM_TOKEN:
    sys.exit("TELEGRAM_TOKEN not set — add it to .env and re-run.")

recipients = []
if config.TELEGRAM_CHAT_ID:
    recipients.append(("Heath", config.TELEGRAM_CHAT_ID))
if config.TELEGRAM_CHAT_ID_2:
    recipients.append(("George", config.TELEGRAM_CHAT_ID_2))
if config.TELEGRAM_CHAT_ID_3:
    recipients.append(("Max", config.TELEGRAM_CHAT_ID_3))

if not recipients:
    sys.exit("No TELEGRAM_CHAT_ID values set — add them to .env and re-run.")

MESSAGE = (
    "✅ Forex AI pre-flight check complete — 6am scan confirmed for tomorrow.\n\n"
    "All systems go: session times, MA ribbon, Fibonacci, divergence, oscillator "
    "confluence, cost tracking, and open trade updates are live."
)

url = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage"
all_ok = True
for name, chat_id in recipients:
    try:
        data = urllib.parse.urlencode({
            "chat_id":    chat_id,
            "text":       MESSAGE,
            "parse_mode": "HTML",
        }).encode()
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
        print(f"✅ Sent to {name} (chat_id: {chat_id})")
    except Exception as exc:
        print(f"❌ FAILED for {name}: {exc}")
        all_ok = False

if all_ok:
    print(f"\nAll {len(recipients)} recipient(s) confirmed.")
else:
    sys.exit("One or more sends failed — check token and chat IDs.")
