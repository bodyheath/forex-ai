"""Tests for the 2026-09-08 DISCORD_LIVE_SEND safety gate in
src/discord_notifier.py.

Real incident: a live Discord investigation this session almost fired a
real POST/PATCH at the production fund-dashboard webhook from an
interactive dev sandbox that happened to have a real credential loaded via
a local .env, purely as a side effect of reproducing a code path to
diagnose an unrelated bug. Every real outbound Discord call in this module
(and monitor.py's own _send_dashboard()) now requires this explicit
opt-in, set only by the real GitHub Actions workflows -- default is a
safe no-op, everywhere else, always.
"""
import os
import unittest
from unittest.mock import patch

from src import discord_notifier as dn
from src import monitor


class TestDiscordLiveSendsEnabled(unittest.TestCase):

    def test_unset_is_disabled(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DISCORD_LIVE_SEND", None)
            self.assertFalse(dn._discord_live_sends_enabled())

    def test_yes_enables(self):
        with patch.dict(os.environ, {"DISCORD_LIVE_SEND": "YES"}):
            self.assertTrue(dn._discord_live_sends_enabled())

    def test_arbitrary_value_does_not_enable(self):
        with patch.dict(os.environ, {"DISCORD_LIVE_SEND": "maybe"}):
            self.assertFalse(dn._discord_live_sends_enabled())


class TestDcPostPatchGate(unittest.TestCase):

    def test_dc_post_blocked_by_default_never_calls_requests(self):
        with patch.dict(os.environ, {}, clear=False), \
             patch.object(dn, "requests") as mock_requests:
            os.environ.pop("DISCORD_LIVE_SEND", None)
            resp = dn._dc_post("https://discord.com/api/webhooks/fake/token", json={})
        mock_requests.post.assert_not_called()
        self.assertEqual(resp.status_code, 204)

    def test_dc_patch_blocked_by_default_never_calls_requests(self):
        with patch.dict(os.environ, {}, clear=False), \
             patch.object(dn, "requests") as mock_requests:
            os.environ.pop("DISCORD_LIVE_SEND", None)
            resp = dn._dc_patch("https://discord.com/api/webhooks/fake/token", json={})
        mock_requests.patch.assert_not_called()
        self.assertEqual(resp.status_code, 204)

    def test_dc_post_calls_through_when_explicitly_enabled(self):
        with patch.dict(os.environ, {"DISCORD_LIVE_SEND": "YES"}), \
             patch.object(dn, "requests") as mock_requests:
            dn._dc_post("https://discord.com/api/webhooks/fake/token", json={"a": 1}, timeout=10)
        mock_requests.post.assert_called_once_with(
            "https://discord.com/api/webhooks/fake/token", json={"a": 1}, timeout=10,
        )

    def test_dc_patch_calls_through_when_explicitly_enabled(self):
        with patch.dict(os.environ, {"DISCORD_LIVE_SEND": "YES"}), \
             patch.object(dn, "requests") as mock_requests:
            dn._dc_patch("https://discord.com/api/webhooks/fake/token", json={"a": 1}, timeout=10)
        mock_requests.patch.assert_called_once_with(
            "https://discord.com/api/webhooks/fake/token", json={"a": 1}, timeout=10,
        )


class TestMonitorSendDashboardUsesGate(unittest.TestCase):
    """monitor._send_dashboard() must route through discord_notifier's
    gated wrappers, never call requests directly -- confirms the fix that
    removed monitor.py's own separate `import requests as _req_dash`."""

    def test_send_dashboard_never_touches_requests_directly_when_blocked(self):
        with patch.dict(os.environ, {}, clear=False), \
             patch.object(dn, "WEBHOOK_FUND", "https://discord.com/api/webhooks/fake/token"), \
             patch.object(dn, "requests") as mock_requests:
            os.environ.pop("DISCORD_LIVE_SEND", None)
            monitor._send_dashboard(state={"balance": 10000}, log_fn=lambda m: None)
        mock_requests.post.assert_not_called()
        mock_requests.patch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
