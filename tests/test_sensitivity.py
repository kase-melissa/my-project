import unittest
from datetime import date
from pathlib import Path

from bonus_planner.calibrate import MonthlyRow, calibrate_monthly
from bonus_planner.config import AppConfig, BehaviorParams, StoreConfig
from bonus_planner.schedule import load_schedule
from bonus_planner.sensitivity import (
    average_market_factor,
    fragile_days,
    robust_days,
    run_scenarios,
)

ROOT = Path(__file__).resolve().parent.parent
SCHEDULE = ROOT / "data" / "promo_schedule" / "2026-10.yaml"
CONFIG = ROOT / "config" / "config.yaml"
BEHAVIOR = ROOT / "config" / "behavior_priors.yaml"
TODAY = date(2026, 9, 15)


class TestScenarios(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = AppConfig.load(CONFIG, BEHAVIOR)
        cls.schedule = load_schedule(SCHEDULE)
        cls.scenarios = run_scenarios(cls.cfg, cls.schedule, TODAY)

    def test_scenarios_span_weak_to_strong(self):
        self.assertEqual(
            [s.name for s in self.scenarios], ["実測点推定", "弱気", "既定", "強気"]
        )
        gains = [s.share_gain_at_reference for s in self.scenarios]
        self.assertEqual(gains, sorted(gains))

    def test_weakest_scenario_still_keeps_bonus_store_plus_days(self):
        """最も弱い前提でも、モール負担の上乗せが開く6日は残る."""
        weakest = self.scenarios[0]
        days = {d.day for d in weakest.selected_days if d.month == 10}
        self.assertTrue({2, 7, 16, 20, 27, 28} <= days, sorted(days))

    def test_default_uses_configured_params(self):
        default = next(s for s in self.scenarios if s.name == "既定")
        self.assertAlmostEqual(
            default.market_elasticity, self.cfg.behavior.market_elasticity
        )
        self.assertAlmostEqual(
            default.share_gain_at_reference, self.cfg.behavior.share_gain_at_reference
        )

    def test_stronger_response_yields_more_gmv(self):
        gmvs = [s.incremental_gmv for s in self.scenarios]
        self.assertEqual(gmvs, sorted(gmvs))

    def test_all_scenarios_respect_budget(self):
        for s in self.scenarios:
            self.assertLessEqual(s.point_cost, self.cfg.store.monthly_point_budget)

    def test_robust_and_fragile_partition_the_union(self):
        robust = robust_days(self.scenarios)
        fragile = fragile_days(self.scenarios)
        union = set()
        for s in self.scenarios:
            union |= s.selected_days
        self.assertEqual(robust | fragile, union)
        self.assertEqual(robust & fragile, set())

    def test_bonus_store_plus_days_are_robust(self):
        """モール負担の+2%が開く日は、どの前提でも選ばれるはず."""
        robust = {d.day for d in robust_days(self.scenarios) if d.month == 10}
        self.assertTrue({2, 7, 16, 20, 27, 28} <= robust)

    def test_empty_scenarios(self):
        self.assertEqual(robust_days([]), set())
        self.assertEqual(fragile_days([]), set())


class TestAverageMarketFactor(unittest.TestCase):
    def test_above_one_for_a_month_with_events(self):
        cfg = AppConfig.load(CONFIG, BEHAVIOR)
        schedule = load_schedule(SCHEDULE)
        factor = average_market_factor(cfg, schedule)
        self.assertGreater(factor, 1.0)
        self.assertLess(factor, 1.3)

    def test_correction_lowers_base_orders(self):
        """イベント上振れを差し引くと base_orders は小さくなる."""
        rows = [
            MonthlyRow(2025, m, 1200, 24_000_000) for m in range(1, 13)
        ] + [MonthlyRow(2026, 1, 1200, 24_000_000)]
        prior = BehaviorParams(month={})
        uncorrected = calibrate_monthly(rows, prior, average_market_factor=1.0)
        corrected = calibrate_monthly(rows, prior, average_market_factor=1.074)
        self.assertLess(corrected.params.base_orders, uncorrected.params.base_orders)
        self.assertAlmostEqual(
            corrected.params.base_orders * 1.074,
            uncorrected.params.base_orders,
            delta=0.5,
        )

    def test_warns_when_correction_is_skipped(self):
        rows = [MonthlyRow(2025, m, 1200, 24_000_000) for m in range(1, 13)]
        result = calibrate_monthly(rows, BehaviorParams(month={}), average_market_factor=0.0)
        self.assertTrue(any("過大" in n for n in result.notes))


if __name__ == "__main__":
    unittest.main()
