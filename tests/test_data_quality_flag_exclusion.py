"""Tests for the 2026-09-17 data_quality_flag exclusion mechanism
(src/trading/financials.py::calculate_fund_state).

Real incident: #6987 (USD/JPY) closed as a real LOSS, but its recorded
entry/exit were never real tradeable prices -- confirmed via two
independent sources (Yahoo Finance and Twelve Data 5-minute bars) showing
real USD/JPY traded ~150-290 pips away from both recorded levels the
entire time. A stale Yahoo daily bar fed the entry price. Nothing in
trades.csv distinguished this from a genuine loss: every real balance/
streak/WR-PF calculation counted it identically since the day it closed.

data_quality_flag gives a confirmed-fictitious close a real, structured
marker. Swept the fund's full pre-fix-cutoff history (29 real candidates)
against real intraday data -- #6987 is the only one this applies to.

Design intent, tested here directly:
  - running_bal/peak_bal/daily_pnl_d (real dollar figures) stay
    UNCONDITIONAL -- a flagged trade still moved the real account, and
    this does not rewrite historical balance.
  - consecutive_losses/consecutive_wins (the circuit breaker's real gate)
    EXCLUDE flagged rows entirely -- treated the same as a genuinely
    neutral row: streak unchanged, not reset.
  - win_rate/profit_factor/v2_win_rate/v2_net_pips/v2_decisive and every
    v2_* statistic EXCLUDE flagged rows.
  - A CSV with no data_quality_flag column at all (older snapshots, other
    tests) must behave exactly as before -- no flag column is not the
    same as an empty flag column, but both must resolve to "unflagged".
"""
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from src.trading import financials


def _row(id_, pair, direction, status, entry, stop_loss, pips, net_pips,
         timestamp, closed_at, flag="", system_version="v2"):
    return {
        "id": id_, "pair": pair, "direction": direction, "status": status,
        "trade_this": "YES", "system_version": system_version,
        "timestamp": timestamp, "closed_at": closed_at,
        "entry": entry, "stop_loss": stop_loss, "target": entry,
        "pips": pips, "net_pips": net_pips,
        "position_size_pct_at_entry": 1.0,
        "data_quality_flag": flag,
    }


class TestDataQualityFlagStreakExclusion(unittest.TestCase):

    def test_flagged_loss_does_not_increment_streak(self):
        # win, then a flagged loss, then two real losses -- if the flagged
        # row counted, streak would be 3; excluded, it should be 2.
        rows = [
            _row(1, "EUR/USD", "BUY", "WIN", 1.10, 1.09, 50, 50,
                 "2026-09-01 00:00:00", "2026-09-01 01:00:00"),
            _row(2, "USD/JPY", "BUY", "LOSS", 156.197, 154.6068, -159.0, -161.3,
                 "2026-09-08 05:15:56", "2026-09-08 05:31:25",
                 flag="stale_daily_bar_entry_price"),
            _row(3, "AUD/NZD", "SELL", "LOSS", 1.2256, 1.2333, -77.0, -82.5,
                 "2026-09-07 11:17:17", "2026-09-08 11:10:58"),
            _row(4, "CHF/JPY", "BUY", "LOSS", 189.667, 187.844, -182.3, -186.5,
                 "2026-09-10 18:19:44", "2026-09-11 21:01:36"),
        ]
        df = pd.DataFrame(rows)
        state = financials.calculate_fund_state(df, prices={})
        self.assertEqual(state["consecutive_losses"], 2)

    def test_unflagged_same_sequence_gives_streak_of_three(self):
        rows = [
            _row(1, "EUR/USD", "BUY", "WIN", 1.10, 1.09, 50, 50,
                 "2026-09-01 00:00:00", "2026-09-01 01:00:00"),
            _row(2, "USD/JPY", "BUY", "LOSS", 156.197, 154.6068, -159.0, -161.3,
                 "2026-09-08 05:15:56", "2026-09-08 05:31:25"),  # not flagged
            _row(3, "AUD/NZD", "SELL", "LOSS", 1.2256, 1.2333, -77.0, -82.5,
                 "2026-09-07 11:17:17", "2026-09-08 11:10:58"),
            _row(4, "CHF/JPY", "BUY", "LOSS", 189.667, 187.844, -182.3, -186.5,
                 "2026-09-10 18:19:44", "2026-09-11 21:01:36"),
        ]
        df = pd.DataFrame(rows)
        state = financials.calculate_fund_state(df, prices={})
        self.assertEqual(state["consecutive_losses"], 3)

    def test_flagged_win_does_not_reset_streak(self):
        # A flagged WIN shouldn't reset an in-progress loss streak either --
        # symmetry with the loss case, same "streak unchanged" treatment.
        rows = [
            _row(1, "EUR/USD", "SELL", "LOSS", 1.10, 1.11, -100, -102,
                 "2026-09-01 00:00:00", "2026-09-01 01:00:00"),
            _row(2, "USD/JPY", "BUY", "WIN", 156.197, 154.6068, 300, 295,
                 "2026-09-08 05:15:56", "2026-09-08 05:31:25",
                 flag="stale_daily_bar_entry_price"),
            _row(3, "AUD/NZD", "SELL", "LOSS", 1.2256, 1.2333, -77.0, -82.5,
                 "2026-09-07 11:17:17", "2026-09-08 11:10:58"),
        ]
        df = pd.DataFrame(rows)
        state = financials.calculate_fund_state(df, prices={})
        self.assertEqual(state["consecutive_losses"], 2)

    def test_real_five_loss_sequence_recomputes_to_four_excluding_6987(self):
        """Direct reconstruction of the real 2026-09-17 streak, both ways."""
        rows = [
            _row(5468, "USD/CAD", "BUY", "EXPIRED", 1.3791, 1.3723, 51.5, 52.9,
                 "2026-08-20 18:18:42", "2026-08-24 21:10:59"),
            _row(5952, "EUR/AUD", "BUY", "EXPIRED", 1.6244, 1.6118, -64.7, -70.9,
                 "2026-08-26 18:20:51", "2026-08-30 21:10:58"),
            _row(6987, "USD/JPY", "BUY", "LOSS", 156.197, 154.6068, -159.0, -161.3,
                 "2026-09-08 05:15:56", "2026-09-08 05:31:25",
                 flag="stale_daily_bar_entry_price"),
            _row(6895, "AUD/NZD", "SELL", "LOSS", 1.2256, 1.2333, -77.0, -82.5,
                 "2026-09-07 11:17:17", "2026-09-08 11:10:58"),
            _row(7325, "CHF/JPY", "BUY", "LOSS", 189.667, 187.844, -182.3, -186.5,
                 "2026-09-10 18:19:44", "2026-09-11 21:01:36"),
            _row(7615, "AUD/NZD", "SELL", "LOSS", 1.2345, 1.2422, -77.0, -82.7,
                 "2026-09-14 21:18:25", "2026-09-17 06:01:32"),
        ]
        df = pd.DataFrame(rows)
        state = financials.calculate_fund_state(df, prices={})
        self.assertEqual(state["consecutive_losses"], 4,
                          "excluding the confirmed-fictitious #6987 close, the real "
                          "streak is 4, not 5")


class TestDataQualityFlagBalanceUnaffected(unittest.TestCase):

    def test_flagged_row_still_moves_real_balance(self):
        flagged_loss = [
            _row(1, "USD/JPY", "BUY", "LOSS", 156.197, 154.6068, -159.0, -161.3,
                 "2026-09-08 05:15:56", "2026-09-08 05:31:25",
                 flag="stale_daily_bar_entry_price"),
        ]
        unflagged_same_loss = [
            _row(1, "USD/JPY", "BUY", "LOSS", 156.197, 154.6068, -159.0, -161.3,
                 "2026-09-08 05:15:56", "2026-09-08 05:31:25"),
        ]
        state_flagged = financials.calculate_fund_state(pd.DataFrame(flagged_loss), prices={})
        state_unflagged = financials.calculate_fund_state(pd.DataFrame(unflagged_same_loss), prices={})
        # Balance impact must be identical either way -- this does not
        # rewrite historical balance/dollar figures.
        self.assertEqual(state_flagged["balance"], state_unflagged["balance"])
        self.assertEqual(state_flagged["peak_balance"], state_unflagged["peak_balance"])


class TestDataQualityFlagStatsExclusion(unittest.TestCase):

    def test_v2_win_rate_and_decisive_exclude_flagged(self):
        rows = [
            _row(1, "EUR/USD", "BUY", "WIN", 1.10, 1.09, 100, 100,
                 "2026-09-01 00:00:00", "2026-09-01 01:00:00"),
            _row(2, "GBP/USD", "SELL", "WIN", 1.30, 1.31, 100, 100,
                 "2026-09-02 00:00:00", "2026-09-02 01:00:00"),
            _row(3, "USD/JPY", "BUY", "LOSS", 156.197, 154.6068, -159.0, -161.3,
                 "2026-09-08 05:15:56", "2026-09-08 05:31:25",
                 flag="stale_daily_bar_entry_price"),
        ]
        df = pd.DataFrame(rows)
        state = financials.calculate_fund_state(df, prices={})
        # 2 real wins, 0 real losses (the loss is flagged out) -> 100% WR, n=2
        self.assertEqual(state["v2_decisive"], 2)
        self.assertEqual(state["v2_wins"], 2)
        self.assertEqual(state["v2_losses"], 0)
        self.assertEqual(state["v2_win_rate"], 100.0)

    def test_survives_a_real_csv_round_trip_not_just_an_in_memory_frame(self):
        """The real bug this test would have caught: a DataFrame built
        in-memory (like every other test in this file) keeps an empty
        string as "" -- but data_quality_flag on real trades.csv rows
        always passes through pd.read_csv first, where a blank cell reads
        back as NaN (a float), not "". `nan or ""` evaluates to nan itself
        (nan is truthy), so a naive `str(x or "").strip()` guard silently
        treated every real unflagged row as flagged, zeroing out
        consecutive_losses/v2_decisive/v2_win_rate entirely on the actual
        file. Round-trips through a real temp CSV to reproduce that
        exactly, instead of asserting against an in-memory frame that can't
        expose it."""
        rows = [
            _row(1, "EUR/USD", "BUY", "WIN", 1.10, 1.09, 100, 100,
                 "2026-09-01 00:00:00", "2026-09-01 01:00:00"),
            _row(2, "GBP/USD", "SELL", "LOSS", 1.30, 1.31, -100, -100,
                 "2026-09-02 00:00:00", "2026-09-02 01:00:00"),
            _row(3, "USD/JPY", "BUY", "LOSS", 156.197, 154.6068, -159.0, -161.3,
                 "2026-09-08 05:15:56", "2026-09-08 05:31:25",
                 flag="stale_daily_bar_entry_price"),
        ]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "trades.csv"
            pd.DataFrame(rows).to_csv(path, index=False)
            df = pd.read_csv(path)  # real round-trip: blank flags become NaN
            state = financials.calculate_fund_state(df, prices={})
        # 1 real win, 1 real loss (the flagged one excluded) -> n=2, not 0
        self.assertEqual(state["v2_decisive"], 2)
        self.assertEqual(state["v2_wins"], 1)
        self.assertEqual(state["v2_losses"], 1)
        self.assertEqual(state["consecutive_losses"], 1)

    def test_no_flag_column_at_all_behaves_as_before(self):
        rows = [
            {"id": 1, "pair": "EUR/USD", "direction": "BUY", "status": "WIN",
             "trade_this": "YES", "system_version": "v2",
             "timestamp": "2026-09-01 00:00:00", "closed_at": "2026-09-01 01:00:00",
             "entry": 1.10, "stop_loss": 1.09, "target": 1.10,
             "pips": 100, "net_pips": 100, "position_size_pct_at_entry": 1.0},
            {"id": 2, "pair": "USD/JPY", "direction": "BUY", "status": "LOSS",
             "trade_this": "YES", "system_version": "v2",
             "timestamp": "2026-09-08 05:15:56", "closed_at": "2026-09-08 05:31:25",
             "entry": 156.197, "stop_loss": 154.6068, "target": 156.197,
             "pips": -159.0, "net_pips": -161.3, "position_size_pct_at_entry": 1.0},
        ]
        df = pd.DataFrame(rows)  # no data_quality_flag column anywhere
        try:
            state = financials.calculate_fund_state(df, prices={})
        except Exception as exc:
            self.fail(f"calculate_fund_state raised with no data_quality_flag "
                      f"column present: {exc!r}")
        self.assertEqual(state["consecutive_losses"], 1)
        self.assertEqual(state["v2_decisive"], 2)


if __name__ == "__main__":
    unittest.main()
