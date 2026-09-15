import unittest

from bonus_planner.config import StoreConfig
from bonus_planner.economics import (
    breakeven_uplift_ratio,
    customer_perceived_rate,
    effective_point_rate,
    evaluate_profit,
    point_cost,
)


class TestEffectiveRate(unittest.TestCase):
    def test_cap_not_binding_returns_nominal(self):
        # 単価2万・上限5000円なら4%(=800円)は上限に届かない
        self.assertAlmostEqual(effective_point_rate(0.04, 20000, 0.55, 5000), 0.04, places=4)

    def test_cap_binding_reduces_rate(self):
        # 単価20万・5%(=1万円)は上限5000円で頭打ち
        eff = effective_point_rate(0.05, 200000, 0.55, 5000)
        self.assertLess(eff, 0.05)
        self.assertGreater(eff, 0.0)

    def test_zero_rate(self):
        self.assertEqual(effective_point_rate(0.0, 20000, 0.55, 5000), 0.0)

    def test_no_cap(self):
        self.assertEqual(effective_point_rate(0.04, 20000, 0.55, 0), 0.04)

    def test_monotonic_in_rate(self):
        prev = -1.0
        for r in (0.01, 0.02, 0.03, 0.04, 0.05):
            eff = effective_point_rate(r, 50000, 0.6, 5000)
            self.assertGreater(eff, prev)
            prev = eff

    def test_effective_never_exceeds_nominal(self):
        for aov in (5000, 20000, 80000, 300000):
            for r in (0.01, 0.03, 0.05):
                self.assertLessEqual(
                    effective_point_rate(r, aov, 0.55, 5000), r + 1e-12
                )

    def test_zero_sigma_is_deterministic(self):
        # ばらつきなし・単価20万・5% -> 上限5000円 / 20万 = 2.5%
        self.assertAlmostEqual(
            effective_point_rate(0.05, 200000, 0.0, 5000), 0.025, places=6
        )


class TestPerceivedRate(unittest.TestCase):
    def test_capped(self):
        self.assertAlmostEqual(customer_perceived_rate(0.10, 100000, 5000), 0.05)

    def test_uncapped(self):
        self.assertAlmostEqual(customer_perceived_rate(0.03, 20000, 5000), 0.03)


class TestPointCost(unittest.TestCase):
    def test_includes_natural_orders(self):
        # エントリーしなくても発生した注文にも原資はかかる
        self.assertAlmostEqual(point_cost(100, 20000, 0.04), 80000)

    def test_fee_applied(self):
        self.assertAlmostEqual(point_cost(100, 20000, 0.04, 0.1), 88000)


class TestProfit(unittest.TestCase):
    def setUp(self):
        self.store = StoreConfig(
            gross_margin_rate=0.32, aov=20000, aov_sigma=0.55,
            point_cap_per_order=5000, new_customer_ratio=0.0,
            ltv_uplift_per_new_customer=0.0,
        )

    def test_uplift_too_small_is_unprofitable(self):
        r = evaluate_profit(self.store, 100, 20000, 101, 20000, 0.04)
        self.assertLess(r["net_value"], 0)

    def test_large_uplift_is_profitable(self):
        r = evaluate_profit(self.store, 100, 20000, 150, 20000, 0.04)
        self.assertGreater(r["net_value"], 0)

    def test_breakeven_matches_formula(self):
        be = breakeven_uplift_ratio(self.store, 0.04, 20000)
        r = evaluate_profit(self.store, 100, 20000, 100 * (1 + be), 20000, 0.04)
        self.assertAlmostEqual(r["gross_profit_delta"], 0.0, delta=1.0)

    def test_breakeven_infinite_when_rate_exceeds_margin(self):
        store = StoreConfig(gross_margin_rate=0.02, aov=20000, point_cap_per_order=0)
        self.assertEqual(breakeven_uplift_ratio(store, 0.05, 20000), float("inf"))

    def test_ltv_counted_for_new_customers(self):
        store = StoreConfig(
            gross_margin_rate=0.32, aov=20000, point_cap_per_order=5000,
            new_customer_ratio=0.5, ltv_uplift_per_new_customer=4000,
        )
        r = evaluate_profit(store, 100, 20000, 120, 20000, 0.04)
        self.assertAlmostEqual(r["ltv_value"], 20 * 0.5 * 4000)


if __name__ == "__main__":
    unittest.main()
