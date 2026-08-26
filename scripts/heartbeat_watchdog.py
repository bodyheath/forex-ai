"""GHA-native heartbeat watchdog — independent of cron-job.org.

2026-08-26: closes the last remaining single point of failure found in the
system audit. Every existing check-in mechanism (the scans themselves, and
daily.py's own >120-minute heartbeat-staleness alert at the top of each run)
is triggered by cron-job.org calling workflow_dispatch. If cron-job.org has
a full outage, NOTHING in the repo would ever notice or alert -- the
existing watchdog and the thing it watches share the same single external
trigger source.

This script is invoked by a workflow using GitHub Actions' own native
`schedule:` cron trigger, which does not depend on cron-job.org at all.

Staleness threshold: 150 minutes -- deliberately looser than daily.py's
120-minute check (see that check's own comment, daily.py "Item 2: Monitor
heartbeat check"), for two reasons:
  1. This workflow's own schedule is not exempt from GitHub's documented
     behavior for scheduled workflows: "high loads of GitHub Actions
     workflow runs across GitHub may cause a delay... in some cases the
     workflow may not run" -- a 30-minute cron here can genuinely fire late
     under platform load, so the threshold needs slack for its own
     execution jitter, not just monitor.py's.
  2. Letting the existing 120-minute check "own" the tighter threshold and
     using this one specifically as the cron-job.org-independent backstop
     avoids this workflow racing the other one to fire first on routine,
     recoverable jitter.

Alert dedup: state is persisted in data/heartbeat_watchdog_state.json.
Alerts once per distinct outage (identified by the heartbeat's own
last_monitor_run value not having changed since the last alert), with a
6-hour reminder cadence if the same outage is still ongoing, rather than
firing every time this workflow runs while the condition remains true.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

STALENESS_MINUTES = 150
REMINDER_HOURS = 6

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
HEARTBEAT_FILE = DATA_DIR / "heartbeat.json"
STATE_FILE = DATA_DIR / "heartbeat_watchdog_state.json"


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _parse_hb_timestamp(raw: str):
    """Exact same parsing as daily.py's existing heartbeat check, so both
    mechanisms agree on what 'stale' means for the same underlying value."""
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)
        return ts
    except Exception:
        return None


def _send_telegram(msg: str) -> None:
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
            print(f"[heartbeat-watchdog] Telegram send failed for {chat_id}: {e}", file=sys.stderr)


def _send_discord(msg: str) -> None:
    webhook = os.environ.get("DISCORD_WEBHOOK_CRITICAL", "")
    if not webhook:
        return
    try:
        import urllib.request as _ur
        payload = json.dumps({
            "embeds": [{"title": "Heartbeat Watchdog (GHA-native)", "description": msg,
                        "color": 16711680}]
        }).encode()
        req = _ur.Request(webhook, data=payload, headers={"Content-Type": "application/json"})
        _ur.urlopen(req, timeout=10)
    except Exception as e:
        print(f"[heartbeat-watchdog] Discord send failed: {e}", file=sys.stderr)


def main() -> int:
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    if not HEARTBEAT_FILE.exists():
        print("[heartbeat-watchdog] heartbeat.json does not exist yet -- nothing to check "
              "(expected on a brand-new repo before monitor.py has ever run once).")
        return 0

    hb = _load_json(HEARTBEAT_FILE)
    hb_raw = hb.get("last_monitor_run") or hb.get("last_run") or hb.get("timestamp") or hb.get("updated_at") or ""
    if not hb_raw:
        print(f"[heartbeat-watchdog] heartbeat.json has no usable timestamp field: {hb!r} -- skipping.")
        return 0

    hb_ts = _parse_hb_timestamp(hb_raw)
    if hb_ts is None:
        print(f"[heartbeat-watchdog] could not parse heartbeat timestamp {hb_raw!r} -- skipping.")
        return 0

    gap_minutes = (now - hb_ts).total_seconds() / 60.0
    print(f"[heartbeat-watchdog] last_monitor_run={hb_raw} gap={gap_minutes:.1f} minutes "
          f"(threshold={STALENESS_MINUTES})")

    if gap_minutes <= STALENESS_MINUTES:
        print("[heartbeat-watchdog] heartbeat is fresh -- no alert.")
        return 0

    state = _load_json(STATE_FILE)
    same_outage = state.get("alerted_for_last_monitor_run") == hb_raw
    last_alert_str = state.get("last_alert_sent_at", "")
    should_alert = True
    if same_outage and last_alert_str:
        try:
            last_alert_dt = datetime.fromisoformat(last_alert_str)
            if now - last_alert_dt < timedelta(hours=REMINDER_HOURS):
                should_alert = False
        except Exception:
            pass

    if not should_alert:
        print(f"[heartbeat-watchdog] already alerted for this outage "
              f"(last alert {last_alert_str}) -- within the {REMINDER_HOURS}h reminder "
              f"cooldown, staying silent.")
        return 0

    is_reminder = same_outage
    msg = (
        f"{'🔴 STILL DOWN' if is_reminder else '🔴 MONITOR HEARTBEAT GAP'} — "
        f"GHA-native watchdog (independent of cron-job.org)\n\n"
        f"monitor.py last ran {gap_minutes:.0f} minutes ago (last_monitor_run={hb_raw}Z), "
        f"expected every 30 minutes.\n"
        f"This check runs on GitHub Actions' own schedule trigger and does not depend on "
        f"cron-job.org — if you're seeing this, cron-job.org itself may be down, since the "
        f"scan-based heartbeat check (daily.py, 120-minute threshold) never gets a chance "
        f"to run in that scenario.\n\n"
        f"Open fund positions are NOT being monitored for stop/target hits while this persists."
    )
    print(f"[heartbeat-watchdog] ALERTING ({'reminder' if is_reminder else 'new outage'}): {msg}")
    _send_telegram(msg)
    _send_discord(msg)

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({
        "alerted_for_last_monitor_run": hb_raw,
        "last_alert_sent_at": now.isoformat(),
    }), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
