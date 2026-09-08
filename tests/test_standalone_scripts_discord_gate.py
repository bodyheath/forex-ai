"""Tests for the 2026-09-09 DISCORD_LIVE_SEND gate extension to
scripts/health_check.py and scripts/heartbeat_watchdog.py.

These two standalone scripts have their own independent raw urllib-based
Discord sends, never routed through src/discord_notifier.py -- so the
2026-09-08 safety gate added there didn't cover them. Both now check
discord_notifier._discord_live_sends_enabled() before sending, failing
CLOSED (blocking the send) if that check itself can't be performed.
"""
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


health_check_script = _load_script_module(
    "health_check_script", _REPO_ROOT / "scripts" / "health_check.py"
)
heartbeat_watchdog_script = _load_script_module(
    "heartbeat_watchdog_script", _REPO_ROOT / "scripts" / "heartbeat_watchdog.py"
)


class TestHealthCheckDiscordGate(unittest.TestCase):

    def test_blocked_by_default_never_opens_url(self):
        with patch.dict(os.environ, {"DISCORD_WEBHOOK_HEALTH": "https://discord.com/api/webhooks/fake/token"}, clear=False), \
             patch("urllib.request.urlopen") as mock_urlopen:
            os.environ.pop("DISCORD_LIVE_SEND", None)
            health_check_script._send_discord("test message")
        mock_urlopen.assert_not_called()

    def test_sends_when_explicitly_enabled(self):
        with patch.dict(os.environ, {
                    "DISCORD_WEBHOOK_HEALTH": "https://discord.com/api/webhooks/fake/token",
                    "DISCORD_LIVE_SEND": "YES",
                }), \
             patch("urllib.request.urlopen") as mock_urlopen:
            health_check_script._send_discord("test message")
        mock_urlopen.assert_called_once()


class TestHeartbeatWatchdogDiscordGate(unittest.TestCase):

    def test_blocked_by_default_never_opens_url(self):
        with patch.dict(os.environ, {"DISCORD_WEBHOOK_CRITICAL": "https://discord.com/api/webhooks/fake/token"}, clear=False), \
             patch.object(heartbeat_watchdog_script.urllib.request, "urlopen") as mock_urlopen:
            os.environ.pop("DISCORD_LIVE_SEND", None)
            heartbeat_watchdog_script._send_discord("test message")
        mock_urlopen.assert_not_called()

    def test_sends_when_explicitly_enabled(self):
        with patch.dict(os.environ, {
                    "DISCORD_WEBHOOK_CRITICAL": "https://discord.com/api/webhooks/fake/token",
                    "DISCORD_LIVE_SEND": "YES",
                }), \
             patch.object(heartbeat_watchdog_script.urllib.request, "urlopen") as mock_urlopen:
            heartbeat_watchdog_script._send_discord("test message")
        mock_urlopen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
