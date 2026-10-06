"""Tests for src/mechanical_scan.py -- the LLM-free forward test of the
Book G/H/I signals.

The load-bearing test is TestEquivalenceWithBacktest: it replays the
backtest's own scripts/mechanical_edge_mining_dataset.py::evaluate_pair_rich()
and this engine on the SAME bars and requires identical fire flags, exit
outcomes, and gross/net pips. A forward test is only evidence about the
backtest if it is the same construction.
"""
import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from scripts import mechanical_edge_mining_dataset as ds
from src import mechanical_scan as ms
from src import shadow_mode as sm


def synth_frame(n=360, seed=1, start="2025-01-01", base=1.10, vol=0.004):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=n)
    x = np.zeros(n)
    x[0] = base
    drift = 0.0
    for t in range(1, n):
        if t % 40 == 0:
            drift = rng.normal(0, 0.0015) * base / 1.10
        x[t] = x[t - 1] + drift + rng.normal(0, vol * base * 0.4) - 0.005 * (x[t - 1] - base)
    o = np.r_[x[0], x[:-1]]
    hi = np.maximum(o, x) + np.abs(rng.normal(0, vol * base * 0.3, n))
    lo = np.minimum(o, x) - np.abs(rng.normal(0, vol * base * 0.3, n))
    return pd.DataFrame({"open": o, "high": hi, "low": lo, "close": x}, index=idx)


def bars(rows, start="2026-01-05"):
    """rows: list of (open, high, low, close)."""
    idx = pd.bdate_range(start, periods=len(rows))
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


def cand(direction="BUY", entry=1.1000, stop=1.0950, target=1.1100,
         pair="EUR/USD", date="2026-01-02"):
    return {"pair": pair, "direction": direction, "entry": repr(entry),
            "stop_loss": repr(stop), "target": repr(target), "signal_date": date}


class TestEquivalenceWithBacktest(unittest.TestCase):

    def _compare(self, pair, frame):
        recs = ds.evaluate_pair_rich(pair, frame)
        B = pd.DataFrame(recs)
        self.assertGreater(len(B), 100, "backtest produced too few rows to be a real test")
        B["fireG"] = B["rib_against"] & B["osc_agrees"]
        B["fireH"] = B["osc_agrees"]
        B["fireI"] = (((B["direction"] == "BUY") & (B["bb_position"] <= 0.2)) |
                      ((B["direction"] == "SELL") & (B["bb_position"] >= 0.8)))
        rows = []
        for i in range(len(frame)):
            for r in ms.candidate_rows_for_bar(pair, frame, i):
                res = ms.settle_row(r, frame[frame.index > pd.Timestamp(r["signal_date"])])
                if res:
                    r.update(res)
                    rows.append(r)
        M = pd.DataFrame(rows)
        m = B.merge(M, left_on=["date", "direction"], right_on=["signal_date", "direction"],
                    suffixes=("_b", "_m"))
        self.assertEqual(len(m), len(B), "engine must produce every row the backtest did")
        # non-vacuous: every signal fires somewhere and does not fire elsewhere
        for col in ("fireG", "fireH", "fireI"):
            self.assertTrue(0 < m[col].sum() < len(m), f"{col} degenerate in synthetic data")
        for k, bc in (("G", "fireG"), ("H", "fireH"), ("I", "fireI")):
            self.assertTrue((m[bc].astype(bool) == (m[f"fire_{k}"] == "1")).all(), f"fire_{k} differs")
        rm = {"WIN": "TARGET", "LOSS": "STOP", "EXPIRED": "EXPIRED"}
        self.assertTrue((m["status_b"].map(rm) == m["exit_reason"]).all(), "exit outcome differs")
        self.assertEqual(float((m["gross_pips_b"] - m["gross_pips_m"].astype(float)).abs().max()), 0.0)
        self.assertEqual(float((m["net_pips_b"] - m["net_pips_m"].astype(float)).abs().max()), 0.0)
        self.assertEqual(float((m["bb_position_b"].astype(float) - m["bb_position_m"].astype(float)).abs().max()), 0.0)

    def test_matches_backtest_on_a_non_jpy_pair(self):
        self._compare("EUR/USD", synth_frame(seed=1))

    def test_matches_backtest_on_a_jpy_pair(self):
        self._compare("USD/JPY", synth_frame(seed=2, base=150.0))


class TestSettleRow(unittest.TestCase):

    def test_buy_target_hit(self):
        b = bars([(1.100, 1.102, 1.099, 1.101), (1.101, 1.111, 1.100, 1.110)], "2026-01-05")
        r = ms.settle_row(cand(), b)
        self.assertEqual((r["status"], r["exit_reason"]), ("WIN", "TARGET"))
        self.assertEqual(r["exit_price"], repr(1.11))

    def test_buy_stop_hit(self):
        b = bars([(1.100, 1.101, 1.094, 1.095)], "2026-01-05")
        r = ms.settle_row(cand(), b)
        self.assertEqual((r["status"], r["exit_reason"]), ("LOSS", "STOP"))
        self.assertLess(r["net_pips"], 0)

    def test_sell_mirror(self):
        c = cand("SELL", entry=1.1000, stop=1.1050, target=1.0900)
        win = ms.settle_row(c, bars([(1.100, 1.101, 1.089, 1.090)]))
        self.assertEqual(win["status"], "WIN")
        loss = ms.settle_row(c, bars([(1.100, 1.106, 1.099, 1.105)]))
        self.assertEqual(loss["status"], "LOSS")

    def test_stop_wins_when_both_hit_in_one_bar(self):
        b = bars([(1.100, 1.112, 1.094, 1.100)])
        r = ms.settle_row(cand(), b)
        self.assertEqual(r["exit_reason"], "STOP")

    def test_unresolved_while_fewer_than_expiry_bars_and_no_hit(self):
        b = bars([(1.100, 1.102, 1.098, 1.101)] * (ms.EXPIRY_BARS - 1))
        self.assertIsNone(ms.settle_row(cand(), b))

    def test_expiry_classified_by_net_pips_sign(self):
        up = bars([(1.100, 1.103, 1.098, 1.102)] * ms.EXPIRY_BARS)
        r = ms.settle_row(cand(), up)
        self.assertEqual(r["exit_reason"], "EXPIRED")
        self.assertEqual(r["status"], "WIN")      # +20 pips gross, positive after costs
        down = bars([(1.100, 1.102, 1.097, 1.098)] * ms.EXPIRY_BARS)
        r2 = ms.settle_row(cand(), down)
        self.assertEqual(r2["status"], "LOSS")

    def test_only_first_expiry_bars_are_considered(self):
        # a later bar that would hit the stop must not matter once expired
        rows = [(1.100, 1.102, 1.098, 1.101)] * ms.EXPIRY_BARS + [(1.100, 1.101, 1.090, 1.091)]
        r = ms.settle_row(cand(), bars(rows))
        self.assertEqual(r["exit_reason"], "EXPIRED")

    def test_days_held_floor_is_one(self):
        r = ms.settle_row(cand(date="2026-01-05"), bars([(1.100, 1.112, 1.099, 1.111)], "2026-01-06"))
        self.assertGreaterEqual(r["days_held"], 1.0)


class TestCandidateRows(unittest.TestCase):

    def setUp(self):
        self.frame = synth_frame(seed=1)

    def test_both_directions_with_two_to_one_levels(self):
        rows = ms.candidate_rows_for_bar("EUR/USD", self.frame, len(self.frame) - 1)
        self.assertEqual(sorted(r["direction"] for r in rows), ["BUY", "SELL"])
        for r in rows:
            e, s, t = float(r["entry"]), float(r["stop_loss"]), float(r["target"])
            self.assertAlmostEqual(abs(t - e) / abs(e - s), 2.0, places=6)
            if r["direction"] == "BUY":
                self.assertTrue(s < e < t)
            else:
                self.assertTrue(t < e < s)

    def test_entry_is_last_close_not_a_model_price(self):
        i = len(self.frame) - 1
        rows = ms.candidate_rows_for_bar("EUR/USD", self.frame, i)
        self.assertEqual(float(rows[0]["entry"]), float(self.frame["close"].iloc[i]))

    def test_too_little_history_yields_nothing(self):
        self.assertEqual(ms.candidate_rows_for_bar("EUR/USD", self.frame, 50), [])

    def test_flags_use_the_live_book_eligibility_functions(self):
        # Book H = oscillator agrees alone; G = H AND ribbon against.
        for i in range(ms.MIN_BARS, len(self.frame)):
            for r in ms.candidate_rows_for_bar("EUR/USD", self.frame, i):
                if r["fire_G"] == "1":
                    self.assertEqual(r["fire_H"], "1", "Book G's population must be a subset of Book H's")


class TestAssignCluster(unittest.TestCase):

    def test_consecutive_and_weekend_gap_share_a_cluster(self):
        t = {}
        a = ms.assign_cluster(t, "G", "EUR/USD", "BUY", True, "2026-01-02")   # Fri
        b = ms.assign_cluster(t, "G", "EUR/USD", "BUY", True, "2026-01-05")   # Mon (3-day gap)
        self.assertEqual(a, b)

    def test_gap_beyond_tolerance_starts_new_cluster(self):
        t = {}
        a = ms.assign_cluster(t, "G", "EUR/USD", "BUY", True, "2026-01-02")
        b = ms.assign_cluster(t, "G", "EUR/USD", "BUY", True, "2026-01-12")
        self.assertNotEqual(a, b)

    def test_fire_state_pair_direction_and_book_are_independent(self):
        t = {}
        base = ms.assign_cluster(t, "G", "EUR/USD", "BUY", True, "2026-01-02")
        for args in (("G", "EUR/USD", "BUY", False), ("G", "GBP/USD", "BUY", True),
                     ("G", "EUR/USD", "SELL", True), ("H", "EUR/USD", "BUY", True)):
            other = ms.assign_cluster(t, *args, "2026-01-02")
            self.assertTrue(other.endswith("_r1"))
        self.assertTrue(base.endswith("_r1"))


class ScanTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        mech = root / "mechanical_path"
        self._real_shadow = root / "REAL_shadow_rules.json"     # stand-in for the shared file
        self._patches = [
            patch.object(ms, "MECH_DIR", mech),
            patch.object(ms, "CANDIDATES_CSV", mech / "candidates.csv"),
            patch.object(ms, "SHADOW_FILE", mech / "shadow_rules.json"),
            patch.object(ms, "TRACKER_JSON", mech / "regime_tracker.json"),
            patch.object(sm, "_SHADOW_FILE", self._real_shadow),
        ]
        for p in self._patches:
            p.start()
        self.full = synth_frame(n=370, seed=3)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def fetcher(self, upto):
        return lambda pair: self.full.iloc[:upto]

    def today_after(self, upto):
        return (self.full.index[upto - 1] + pd.Timedelta(days=1)).date()

    def scan(self, upto, pairs=("EUR/USD",)):
        return ms.run_scan(pairs=list(pairs), fetch_fn=self.fetcher(upto),
                           log=lambda m: None, today=self.today_after(upto))


class TestRunScan(ScanTestCase):

    def test_first_run_creates_only_the_latest_bar_both_directions(self):
        res = self.scan(361)
        self.assertEqual(res["created"], 2)
        rows = ms.load_candidates()
        self.assertEqual({r["signal_date"] for r in rows}, {self.full.index[360].strftime("%Y-%m-%d")})
        self.assertTrue(all(r["status"] == "OPEN" for r in rows))

    def test_rerun_same_day_is_idempotent(self):
        self.scan(361)
        res = self.scan(361)
        self.assertEqual((res["created"], res["settled"], res["recorded"]), (0, 0, 0))
        self.assertEqual(len(ms.load_candidates()), 2)

    def test_candidates_settle_as_bars_arrive_and_match_direct_settlement(self):
        self.scan(361)
        for upto in range(362, 367):
            self.scan(upto)
        rows = ms.load_candidates()
        first = [r for r in rows if r["signal_date"] == self.full.index[360].strftime("%Y-%m-%d")]
        self.assertEqual(len(first), 2)
        for r in first:
            self.assertNotEqual(r["status"], "OPEN", "must be resolved after 4+ later bars")
            direct = ms.settle_row(r, self.full[self.full.index > pd.Timestamp(r["signal_date"])])
            self.assertEqual(r["exit_reason"], direct["exit_reason"])
            self.assertEqual(float(r["net_pips"]), direct["net_pips"])

    def test_a_short_outage_is_backfilled_not_skipped(self):
        self.scan(361)
        res = self.scan(364)               # three new bars since the last run
        dates = {r["signal_date"] for r in ms.load_candidates()}
        self.assertEqual(res["created"], 6)
        self.assertEqual(len(dates), 4)

    def test_backfill_is_capped(self):
        self.scan(361)
        self.scan(369)                     # 8 new bars; cap is MAX_BACKFILL_BARS
        dates = {r["signal_date"] for r in ms.load_candidates()}
        self.assertEqual(len(dates), 1 + ms.MAX_BACKFILL_BARS)

    def test_fetch_failure_and_stale_data_are_skipped_and_counted(self):
        def fetch(pair):
            if pair == "GBP/USD":
                raise RuntimeError("boom")
            if pair == "USD/JPY":
                return self.full.iloc[:361]
            return self.full.iloc[:361]
        res = ms.run_scan(pairs=["EUR/USD", "GBP/USD", "USD/JPY"], fetch_fn=fetch, log=lambda m: None,
                          today=self.today_after(361) + pd.Timedelta(days=30))
        self.assertEqual(res["created"], 0)
        self.assertEqual(res["failures"], ["GBP/USD"])
        self.assertEqual(sorted(res["stale"]), ["EUR/USD", "USD/JPY"])

    def test_short_history_pair_is_skipped(self):
        res = ms.run_scan(pairs=["EUR/USD"], fetch_fn=lambda p: self.full.iloc[:100],
                          log=lambda m: None, today=self.today_after(361))
        self.assertEqual(res["failures"], ["EUR/USD"])


class TestShadowFeedAndIsolation(ScanTestCase):

    def run_to_settled(self):
        self.scan(361)
        for upto in range(362, 367):
            self.scan(upto)

    def test_settled_candidates_feed_the_own_shadow_file_with_clusters(self):
        self.run_to_settled()
        state = __import__("json").loads(ms.SHADOW_FILE.read_text(encoding="utf-8"))
        self.assertEqual(set(state), {rule for (_e, rule, _d) in ms.BOOKS.values()})
        for rule, data in state.items():
            self.assertTrue(data["cluster_aware"])
            self.assertEqual((data["min_n_fire"], data["min_n_no_fire"]), (30, 30))
            self.assertGreater(len(data["evaluations"]), 0)
            self.assertTrue(all(e["context"].get("regime_cluster") for e in data["evaluations"]))

    def test_shared_shadow_file_is_never_touched_and_pointer_is_restored(self):
        self.run_to_settled()
        self.assertFalse(self._real_shadow.exists(), "mechanical path must not write the shared shadow file")
        self.assertEqual(sm._SHADOW_FILE, self._real_shadow, "shadow_mode file pointer must be restored")

    def test_pointer_restored_even_if_recording_raises(self):
        self.scan(361)
        rows = ms.load_candidates()
        for r in rows:
            r.update(status="WIN", net_pips="10", shadow_recorded="0")
        with patch.object(sm, "record_evaluation", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                ms.record_settled(rows)
        self.assertEqual(sm._SHADOW_FILE, self._real_shadow)

    def test_recording_is_idempotent(self):
        self.run_to_settled()
        n1 = sum(len(d["evaluations"]) for d in __import__("json").loads(ms.SHADOW_FILE.read_text(encoding="utf-8")).values())
        self.scan(367)
        self.scan(367)
        rows = ms.load_candidates()
        self.assertEqual(ms.record_settled(rows), 0)
        self.assertGreaterEqual(n1, 3)

    def test_virtual_books_files_are_untouched(self):
        from src import virtual_books as vb
        with tempfile.TemporaryDirectory() as d:
            vbdir = Path(d)
            with patch.object(vb, "VBOOKS_DIR", vbdir), \
                 patch.object(vb, "CANDIDATES_CSV", vbdir / "candidates.csv"), \
                 patch.object(vb, "REJECTIONS_CSV", vbdir / "rejections.csv"):
                self.run_to_settled()
                self.assertEqual(list(vbdir.iterdir()), [])

    def test_candidates_csv_is_well_formed(self):
        self.run_to_settled()
        with open(ms.CANDIDATES_CSV, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(list(rows[0].keys()), ms.CANDIDATE_FIELDS)
        self.assertEqual(len({int(r["id"]) for r in rows}), len(rows))


class TestReadinessAndCli(ScanTestCase):

    def test_readiness_adds_shared_pool_bonferroni(self):
        self.scan(361)
        for upto in range(362, 367):
            self.scan(upto)
        # seed the "shared" file with 12 other unpromoted rules
        with patch.object(sm, "list_rules", return_value={f"r{i}": {"promoted": False} for i in range(12)}):
            out = ms.readiness()
        for key, s in out.items():
            self.assertTrue(s["registered"])
            self.assertAlmostEqual(s["global_corrected_alpha"], 0.05 / (12 + 3), places=6)
            self.assertFalse(s["promotable_global"], "a handful of candidates cannot clear n>=30 regimes")

    def test_readiness_before_any_settlement_reports_unregistered(self):
        self.assertTrue(all(not s.get("registered") for s in ms.readiness().values()))

    def test_report_runs_on_empty_and_populated_data(self):
        lines = []
        ms.report(log=lines.append)
        self.scan(361)
        for upto in range(362, 367):
            self.scan(upto)
        ms.report(log=lines.append)
        self.assertTrue(any("Book G" in l for l in lines))

    def test_cli_scan_fails_loudly_when_nothing_could_be_fetched(self):
        with patch.object(ms, "fetch_daily_frame", side_effect=RuntimeError("no network")):
            self.assertEqual(ms.main(["scan", "--pairs", "EUR/USD,GBP/USD"]), 1)

    def test_cli_scan_succeeds_with_data(self):
        with patch.object(ms, "fetch_daily_frame", side_effect=lambda p: self.full.iloc[:361]), \
             patch.object(ms, "datetime") as dt:
            dt.now.return_value = pd.Timestamp(self.today_after(361)).to_pydatetime()
            dt.strptime = __import__("datetime").datetime.strptime
            self.assertEqual(ms.main(["scan", "--pairs", "EUR/USD"]), 0)


if __name__ == "__main__":
    unittest.main()
