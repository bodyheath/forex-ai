"""Unit tests: confirm market regime score caps in _compute_patience_score.

Rules being tested:
- ranging_high_vol  → score ≤ 4
- ranging (any)     → score ≤ 6
- risk_off          → score ≤ 6
- trending_risk_on  → no regime cap (score can reach 10)
"""
import sys
import types
import unittest
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Minimal stubs so daily.py can be imported without heavy dependencies
# ---------------------------------------------------------------------------

def _make_market_regime_stub(regime: str):
    mod = types.ModuleType("src.market_regime")
    mod.detect = lambda: {"regime": regime, "score": 0}
    return mod


class TestRegimeCaps(unittest.TestCase):

    def _score_for(self, regime: str, raw_score: int = 10) -> int:
        """Run _compute_patience_score with a mocked high raw score and a given regime."""
        stub = _make_market_regime_stub(regime)
        with patch.dict("sys.modules", {"src.market_regime": stub}):
            # Import the function after patching so it picks up the stub
            import importlib
            import daily as _d
            importlib.reload(_d)  # reload to pick up patched module
            # Build a ctx that produces a high raw score
            ctx = {
                "risk_env": regime.replace("_", " "),
                "vix": 15.0,          # low VIX → max VIX pts
                "high_impact_count": 0,
                "qualify_pct": 1.0,
                "avg_mtf": 0.90,      # strong trend
            }
            result = _d._compute_patience_score(ctx)
            return result["score"]

    def test_ranging_high_vol_capped_at_4(self):
        score = self._score_for("ranging_high_vol")
        self.assertLessEqual(score, 4,
            f"ranging_high_vol must cap at 4, got {score}")

    def test_ranging_low_vol_capped_at_6(self):
        score = self._score_for("ranging_low_vol")
        self.assertLessEqual(score, 6,
            f"ranging_low_vol must cap at 6, got {score}")

    def test_risk_off_capped_at_6(self):
        score = self._score_for("risk_off")
        self.assertLessEqual(score, 6,
            f"risk_off must cap at 6, got {score}")

    def test_trending_risk_on_no_regime_cap(self):
        stub = _make_market_regime_stub("trending_risk_on")
        with patch.dict("sys.modules", {"src.market_regime": stub}):
            import importlib
            import daily as _d
            importlib.reload(_d)
            ctx = {
                "risk_env": "trending risk-on",
                "vix": 12.0,
                "high_impact_count": 0,
                "qualify_pct": 1.0,
                "avg_mtf": 0.95,
            }
            result = _d._compute_patience_score(ctx)
            # trending_risk_on should NOT be capped — score may be 7+
            self.assertGreater(result["score"], 6,
                f"trending_risk_on should allow score > 6, got {result['score']}")


if __name__ == "__main__":
    unittest.main()
