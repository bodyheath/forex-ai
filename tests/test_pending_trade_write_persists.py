"""Test for the 2026-09-09 fix to _check_pending_trades()'s trades.csv
write-back in src/monitor.py.

Real bug: the save call was `_awc_pend(df, "data/trades.csv")`, but
atomic_write_csv()'s real signature is `(path, df)` (confirmed: every other
call site -- src/monitor.py:1343, src/trading/financials.py:415 -- uses the
correct order). With the arguments swapped, `Path(df)` is built from a
DataFrame inside atomic_write_csv()'s try block, raises, and the function
silently returns False every time -- while _check_pending_trades() still
logs "ACTIVATED" and still fires the real Discord "entry confirmed" alert.
The activation never actually reached disk: the trade would still show
PENDING on the next run and be re-evaluated indefinitely.

Checked real impact before fixing: `entry_confirmed_at` (only ever set by
this exact activation path) is empty on every row in the real trades.csv,
and there are 0 rows currently PENDING -- no evidence any real fund trade
has ever completed activation through this path, so this was a live but
apparently never-yet-exercised bug rather than one that has already cost
real money.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from src import monitor


_TRADES_COLUMNS = [
    "id", "pair", "direction", "status", "trade_this", "entry_type",
    "entry_trigger_price", "entry_trigger_expiry", "entry", "stop_loss",
    "target", "t1_price", "t2_price", "t3_price", "reward_risk",
    "confidence", "capacity_override",
]


class TestPendingTradeWritePersists(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_cwd = os.getcwd()
        os.chdir(self._tmpdir.name)
        Path("data").mkdir(exist_ok=True)
        # consecutive_losses < 3 so the (unrelated) circuit-breaker re-check
        # in this function doesn't block the activation this test exercises.
        Path("data/fund_state.json").write_text(
            json.dumps({"consecutive_losses": 0}), encoding="utf-8"
        )
        row = {c: "" for c in _TRADES_COLUMNS}
        row.update({
            "id": 9001,
            "pair": "AUD/NZD",
            "direction": "BUY",
            "status": "PENDING",
            "trade_this": "YES",
            "entry_type": "IMMEDIATE",
            "entry_trigger_price": 1.2000,
            "entry": 1.2000,
            "stop_loss": 1.1900,
            "target": 1.2200,
            "reward_risk": 2.0,
            "confidence": 7,
        })
        pd.DataFrame([row], columns=_TRADES_COLUMNS).to_csv(
            "data/trades.csv", index=False
        )

    def tearDown(self):
        os.chdir(self._orig_cwd)
        self._tmpdir.cleanup()

    def test_activation_persists_to_disk(self):
        activated = monitor._check_pending_trades({"AUD/NZD": 1.2000}, log_fn=lambda m: None)
        self.assertEqual(len(activated), 1, "activation should succeed in-memory")

        # The real regression: before the fix, this on-disk read would still
        # show PENDING because the swapped-argument write silently failed.
        df = pd.read_csv("data/trades.csv", encoding="utf-8-sig")
        row = df.iloc[0].to_dict()
        self.assertEqual(row["status"], "OPEN")
        self.assertTrue(str(row.get("entry_confirmed_at", "")).strip()
                         not in ("", "nan"))


if __name__ == "__main__":
    unittest.main()
