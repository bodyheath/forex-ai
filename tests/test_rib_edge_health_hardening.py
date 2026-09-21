"""Regression tests for the 2026-09-2X hardening of
src/health_check.py::check_rib_strongly_against_edge_health().

Real bug this closes: investigating a real 2026-09-20 alert (recent 40
decisive rows WR=27.5% vs prior 459 WR=43.6%, p=0.024) found the "recent
40" was not 40 independent trials -- research_trades.csv re-evaluates the
same real underlying move many times a day, so 9 of the 40 raw rows were
the same real AUD/NZD decline re-sampled across scans, 7 were the same
USD/CHF move, etc. -- only 12 unique pair+direction combos across all 40.
Deduplicating to one evaluation per pair+direction+calendar-day shrank the
effective n from 40 to 20. The check also re-tests a raw p<0.05 bar every
6-hourly cycle indefinitely with no multiple-comparisons correction, unlike
shadow_mode.py's registered rules.

These tests confirm: (1) a clustered, single-move-driven burst of raw rows
doesn't inflate apparent evidence once deduplicated, (2) a genuine,
day-spread drift still fires, (3) the corrected (stricter) significance
bar is actually enforced, not just the raw p<0.05 the old code used.
"""
import unittest
from datetime import datetime, timedelta

import pandas as pd

from src import health_check as hc


def _row(pair, direction, status, closed_at, ribbon="ALIGNED_BEAR" if False else None):
    return {
        "pair": pair, "direction": direction, "status": status,
        "closed_at": closed_at.strftime("%Y-%m-%d %H:%M:%S"),
        "ribbon_state": "ALIGNED_BEAR" if direction == "BUY" else "ALIGNED_BULL",
    }


class _BaseRibEdgeHealthTest(unittest.TestCase):

    def _run(self, rows):
        import tempfile
        from pathlib import Path
        self._tmpdir = tempfile.TemporaryDirectory()
        path = Path(self._tmpdir.name) / "research_trades.csv"
        pd.DataFrame(rows).to_csv(path, index=False)
        flags = hc.check_rib_strongly_against_edge_health(csv_path=str(path))
        self._tmpdir.cleanup()
        return flags


class TestAutocorrelationDeduplication(unittest.TestCase):

    def test_clustered_duplicate_burst_does_not_falsely_flag(self):
        """Recent 40 unique-day combos have the SAME true 50% win rate as
        the 60-unique-day older baseline -- but half of the recent LOSS
        days are each represented by several raw duplicate LOSS rows (the
        same real move re-evaluated many times that day). A naive raw-row
        count would show recent looking much worse than it truly is; the
        deduplicated check must see through that and NOT flag."""
        base = datetime(2026, 8, 1)
        rows = []
        # Older baseline: 60 unique days, alternating WIN/LOSS (50% WR)
        for i in range(60):
            day = base + timedelta(days=i)
            status = "WIN" if i % 2 == 0 else "LOSS"
            rows.append(_row("EUR/CHF", "SELL", status, day))

        # Recent: 40 unique days (61-100), alternating WIN/LOSS (50% WR
        # truth) -- but each LOSS day gets 4 extra duplicate LOSS rows on
        # the exact same day (simulating the same real move re-scanned).
        for i in range(60, 100):
            day = base + timedelta(days=i, hours=1)
            status = "WIN" if i % 2 == 0 else "LOSS"
            rows.append(_row("AUD/NZD", "SELL", status, day))
            if status == "LOSS":
                for extra in range(4):
                    rows.append(_row("AUD/NZD", "SELL", "LOSS",
                                      day + timedelta(minutes=extra + 1)))

        test = _BaseRibEdgeHealthTest()
        flags = test._run(rows)
        self.assertEqual(flags, [],
                          "a clustered duplicate burst with the same TRUE "
                          "per-day win rate as baseline must not flag")

    def test_genuine_day_spread_drift_still_flags(self):
        """40 genuinely distinct days, each a different pair, with a real,
        large, consistent drop vs a healthy older baseline -- confirms the
        hardening doesn't neuter the check entirely."""
        base = datetime(2026, 8, 1)
        pairs = ["EUR/CHF", "AUD/NZD", "USD/CHF", "CHF/JPY", "AUD/CHF",
                 "CAD/CHF", "EUR/AUD", "EUR/CAD", "CAD/JPY", "EUR/JPY"]
        rows = []
        # Older: 60 unique days, healthy ~55% WR
        for i in range(60):
            day = base + timedelta(days=i)
            status = "WIN" if i % 20 < 11 else "LOSS"
            rows.append(_row(pairs[i % len(pairs)], "SELL", status, day))
        # Recent: 40 unique days, each a distinct pair+day, ~10% WR (real drop)
        for i in range(60, 100):
            day = base + timedelta(days=i, hours=1)
            status = "WIN" if i % 10 == 0 else "LOSS"
            rows.append(_row(pairs[i % len(pairs)], "SELL", status, day))

        test = _BaseRibEdgeHealthTest()
        flags = test._run(rows)
        self.assertEqual(len(flags), 1)
        self.assertIn("drifting", flags[0])
        self.assertIn("deduplicated", flags[0])


class TestCorrectedSignificanceBar(unittest.TestCase):

    def test_borderline_p_value_that_clears_raw_but_not_corrected_bar(self):
        """Constructs a recent-vs-older gap whose raw p-value sits between
        the corrected bar (0.0125) and the old raw bar (0.05) -- must NOT
        flag under the hardened check, proving the corrected bar is
        actually enforced, not just documented."""
        base = datetime(2026, 8, 1)
        pairs = ["EUR/CHF", "AUD/NZD", "USD/CHF", "CHF/JPY", "AUD/CHF",
                 "CAD/CHF", "EUR/AUD", "EUR/CAD", "CAD/JPY", "EUR/JPY"]
        rows = []
        # Older: 60 unique days at 50% WR
        for i in range(60):
            day = base + timedelta(days=i)
            status = "WIN" if i % 2 == 0 else "LOSS"
            rows.append(_row(pairs[i % len(pairs)], "SELL", status, day))
        # Recent: 40 unique days at a modestly lower ~30% WR -- real gap,
        # but engineered thin enough that p should land in the
        # raw-significant-but-not-Bonferroni-significant band.
        for i in range(60, 100):
            day = base + timedelta(days=i, hours=1)
            status = "WIN" if i % 10 < 3 else "LOSS"
            rows.append(_row(pairs[i % len(pairs)], "SELL", status, day))

        test = _BaseRibEdgeHealthTest()
        flags = test._run(rows)
        # Verify the p-value actually lands in the intended band directly,
        # so this test documents *why* it should or shouldn't fire rather
        # than just asserting a brittle expected outcome.
        from src.health_check import _ztest_worse_beats_better
        older_wins = sum(1 for i in range(60) if i % 2 == 0)
        recent_wins = sum(1 for i in range(60, 100) if i % 10 < 3)
        result = _ztest_worse_beats_better(older_wins, 60, recent_wins, 40)
        p_value = result[0]
        if 0.0125 <= p_value < 0.05:
            self.assertEqual(flags, [],
                              f"p={p_value:.4f} clears the old raw 0.05 bar but not "
                              f"the corrected 0.0125 bar -- must not flag")
        else:
            self.skipTest(f"engineered p={p_value:.4f} landed outside the intended "
                           f"band -- not a valid test of this specific boundary")


if __name__ == "__main__":
    unittest.main()
