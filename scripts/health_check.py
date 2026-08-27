"""Standing health-check runner — GHA-native scheduled, independent script.

Runs every check in src/health_check.py (universe coverage, gate silence,
fund_state staleness, dispatch failures, grade ordering, audit-fix presence,
universal-warning fire-rate) and sends ONE alert only if something is
actually flagged. Silent (exit 0, no message) when everything's clean.

Pure log/data parsing against files already committed to the repo — no API
calls, no network access beyond the alert itself.

Alert dedup: state persisted in data/health_check_state.json, keyed on the
exact sorted set of currently-flagged messages. Only sends when that set
changes from what was last alerted (a new flag appears, or the flag set
otherwise differs) — same "alert on change, not on every run" shape as
heartbeat_watchdog.py, but keyed on flag content since there's no single
"same outage" identifier here.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "health_check_state.json"


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _send_telegram(msg: str) -> None:
    import urllib.parse
    import urllib.request
    token = os.environ.get("TELEGRAM_TOKEN", "")
    chat_ids = [c for c in [
        os.environ.get("TELEGRAM_CHAT_ID", ""),
        os.environ.get("TELEGRAM_CHAT_ID_2", ""),
        os.environ.get("TELEGRAM_CHAT_ID_3", ""),
    ] if c]
    if not token or not chat_ids:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chat_id in chat_ids:
        try:
            data = urllib.parse.urlencode({"chat_id": chat_id, "text": msg}).encode()
            urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
        except Exception as e:
            print(f"[health-check] Telegram send failed for {chat_id}: {e}", file=sys.stderr)


def _send_discord(msg: str) -> None:
    webhook = os.environ.get("DISCORD_WEBHOOK_HEALTH") or os.environ.get("DISCORD_WEBHOOK_CRITICAL", "")
    if not webhook:
        return
    try:
        import urllib.request as _ur
        payload = json.dumps({
            "embeds": [{"title": "Standing Health Check", "description": msg[:4000],
                        "color": 15105570}]
        }).encode()
        req = _ur.Request(webhook, data=payload, headers={"Content-Type": "application/json"})
        _ur.urlopen(req, timeout=10)
    except Exception as e:
        print(f"[health-check] Discord send failed: {e}", file=sys.stderr)


def main() -> int:
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    from src import health_check as hc

    result = hc.run_all_checks()
    flags = result["flags"]
    digest = result["digest"]

    print(f"[health-check] {len(flags)} flag(s), {len(digest)} opened-trade warning(s)")
    for f in flags:
        print(f"  {f}")

    if digest:
        print("[health-check] opened-trade warning digest:")
        for d in digest:
            print(f"  {d}")

    if not flags:
        print("[health-check] all checks clean — no alert.")
        return 0

    state = _load_json(STATE_FILE)
    last_flags = set(state.get("last_flags", []))
    current_flags = set(flags)

    if current_flags == last_flags:
        print("[health-check] same flag set as last alert — staying silent (dedup).")
        return 0

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    msg = (
        f"⚠️ HEALTH CHECK — {len(flags)} issue(s) flagged\n\n"
        + "\n\n".join(flags)
    )
    if digest:
        msg += "\n\n---\nOpened-trade warnings:\n" + "\n".join(digest[-10:])

    print(f"[health-check] flag set changed — sending alert.")
    _send_telegram(msg)
    _send_discord(msg)

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({
        "last_flags": sorted(current_flags),
        "last_alert_sent_at": now.isoformat(),
    }, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
