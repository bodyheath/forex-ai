"""Tests for the 2026-09-09 monitor.lock staleness fix in src/monitor.py.

Real incident: monitor.lock got committed to the repo at 2026-09-08 05:31
UTC (a run acquired the lock but never released it, and monitor.yml's
`git add data/` wildcard swept the leftover file into a commit). Because
GHA does a fresh `git checkout` every run, the committed lock's mtime reset
to "just now" on every subsequent checkout -- permanently defeating the old
mtime-based 120s staleness check in _try_acquire_lock(). Every monitor run
since silently skipped all work (dashboard, closed-trades log, heartbeat,
real open-position stop/target-hit checks) with no exception and no trace,
for ~20 hours across dozens of real runs.

The fix stores a wall-clock "acquired_at_utc" timestamp inside the lock
file's own content and checks staleness against that, not filesystem mtime
-- so a lock is correctly recognized as stale even when its mtime says
"just written" (exactly the fresh-checkout scenario that broke silently).
"""
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from src import monitor


class TestMonitorLockStaleness(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._lock_path = Path(self._tmpdir.name) / "monitor.lock"
        self._patcher = patch("src.monitor._LOCK_FILE", self._lock_path)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmpdir.cleanup()

    def test_fresh_acquire_succeeds_and_writes_timestamp_payload(self):
        acquired = monitor._try_acquire_lock(log=lambda m: None)
        self.assertTrue(acquired)
        payload = json.loads(self._lock_path.read_text(encoding="utf-8"))
        self.assertIn("acquired_at_utc", payload)
        self.assertIn("pid", payload)
        # Round-trips as a real, parseable UTC timestamp
        datetime.fromisoformat(payload["acquired_at_utc"])

    def test_second_acquire_while_lock_is_genuinely_fresh_is_blocked(self):
        self.assertTrue(monitor._try_acquire_lock(log=lambda m: None))
        blocked = monitor._try_acquire_lock(log=lambda m: None)
        self.assertFalse(blocked)

    def test_stale_lock_removed_even_with_fresh_mtime(self):
        """The core regression: a lock file whose embedded timestamp is old
        but whose mtime is brand-new (simulating a fresh GHA git checkout of
        a commit that accidentally included this file) must still be
        recognized as stale and removed."""
        old_time = datetime.now(timezone.utc) - timedelta(
            seconds=monitor._LOCK_TIMEOUT + 3600
        )
        self._lock_path.write_text(
            json.dumps({"pid": 99999, "acquired_at_utc": old_time.isoformat()}),
            encoding="utf-8",
        )
        # Force mtime to "just now", exactly like a fresh git checkout would.
        now = time.time()
        import os as _os
        _os.utime(self._lock_path, (now, now))

        acquired = monitor._try_acquire_lock(log=lambda m: None)
        self.assertTrue(acquired, "a lock stale by embedded timestamp must be "
                                   "removed and re-acquired regardless of mtime")
        payload = json.loads(self._lock_path.read_text(encoding="utf-8"))
        self.assertNotEqual(payload.get("pid"), 99999)

    def test_recently_acquired_lock_with_fresh_mtime_stays_held(self):
        recent_time = datetime.now(timezone.utc) - timedelta(seconds=5)
        self._lock_path.write_text(
            json.dumps({"pid": 12345, "acquired_at_utc": recent_time.isoformat()}),
            encoding="utf-8",
        )
        acquired = monitor._try_acquire_lock(log=lambda m: None)
        self.assertFalse(acquired)

    def test_legacy_bare_pid_lock_falls_back_to_mtime_when_old(self):
        """Pre-2026-09-09 lock format (bare PID string, no timestamp) must
        still be recoverable via the mtime fallback."""
        self._lock_path.write_text("2345", encoding="utf-8")
        old = time.time() - (monitor._LOCK_TIMEOUT + 3600)
        import os as _os
        _os.utime(self._lock_path, (old, old))

        acquired = monitor._try_acquire_lock(log=lambda m: None)
        self.assertTrue(acquired)

    def test_legacy_bare_pid_lock_with_fresh_mtime_stays_held(self):
        self._lock_path.write_text("2345", encoding="utf-8")
        acquired = monitor._try_acquire_lock(log=lambda m: None)
        self.assertFalse(acquired)

    def test_release_lock_removes_file(self):
        monitor._try_acquire_lock(log=lambda m: None)
        self.assertTrue(self._lock_path.exists())
        monitor._release_lock()
        self.assertFalse(self._lock_path.exists())

    def test_release_lock_never_raises_if_file_missing(self):
        try:
            monitor._release_lock()
        except Exception as exc:
            self.fail(f"_release_lock raised {exc!r} on a missing file")


if __name__ == "__main__":
    unittest.main()
