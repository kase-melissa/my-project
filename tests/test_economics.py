import unittest
from datetime import date

from bonus_planner.config import StoreConfig
from bonus_planner.economics import (
    benefit_rate,
    breakeven_uplift_ratio,
    expected_benefit_rate,
    expected_coupon_rate,
    nominal_total_rate,
    perceived_total_rate,
    point_cost,
    rate_by_scope,
    store_funded_rate,
)
from bonus_planner.models import Benefit, Participation

DAY = date(2026, 10, 2)


def ben(**kw):
    base = dict(id="b", name="b", days=(DAY,))
    base.update(kw)
    return Benefit(**base)


class TestExpectedBenefitRate(unittest.TestCase):
    def test_no_cap_no_minimum_returns_nominal(self):
        self.assertAlmostEqual(expected_benefit_rate(0.04, 20000, 0.55), 0.04, places=6)

    def test_zero_rate(self):
        self.assertEqual(expected_benefit_rate(0.0, 20000, 0.55), 0.0)

    def test_cap_reduces_rate(self):
        capped = expected_benefit_rate(0.04, 20000, 0.55, cap_yen=500)
        self.assertLess(capped, 0.04)
        self.assertGreater(capped, 0.0)

    def test_minimum_order_reduces_rate(self):
        # 下限が平均単価を大きく超えると、ほとんどの注文が対象外になる
        low = expected_benefit_rate(0.04, 20000, 0.55, min_order_yen=100000)
        self.assertLess(low, 0.01)

    def test_minimum_below_all_orders_is_neutral(self):
        self.assertAlmostEqual(
            expected_benefit_rate(0.04, 20000, 0.55, min_order_yen=1),
            expected_benefit_rate(0.04, 20000, 0.55),
            places=4,
        )

    def test_never_exceeds_nominal(self):
        for aov in (5000, 20000, 80000, 300000):
            for r in (0.01, 0.03, 0.05):
                self.assertLessEqual(expected_benefit_rate(r, aov, 0.55, 0, 5000), r + 1e-12)

    def test_monotonic_in_rate(self):
        prev = -1.0
        for r in (0.01, 0.02, 0.03, 0.04, 0.05):
            v = expected_benefit_rate(r, 50000, 0.6, 0, 5000)
            self.assertGreater(v, prev)
            prev = v

    def test_zero_sigma_is_deterministic(self):
        # ばらつきなし・単価20万・5% -> 上限5000円 / 20万 = 2.5%
        self.assertAlmostEqual(
            expected_benefit_rate(0.05, 200000, 0.0, 0, 5000), 0.025, places=6
        )

    def test_zero_sigma_respects_minimum(self):
        self.assertEqual(expected_benefit_rate(0.05, 10000, 0.0, 25000), 0.0)

    def test_cap_below_minimum_threshold(self):
        # 下限を満たす注文がすべて上限に達しているケース
        v = expected_benefit_rate(0.05, 50000, 0.4, min_order_yen=100000, cap_yen=1000)
        self.assertGreater(v, 0.0)
        self.assertLess(v, 0.05)


class TestCoupon(unittest.TestCase):
    def test_high_minimum_reduces_effective_rate(self):
        low = expected_coupon_rate(1000, 19800, 0.55, 25000)
        free = expected_coupon_rate(1000, 19800, 0.55, 0)
        self.assertLess(low, free)
        self.assertAlmostEqual(free, 1000 / 19800, places=6)

    def test_zero_coupon(self):
        self.assertEqual(expected_coupon_rate(0, 19800, 0.55), 0.0)


class TestScopes(unittest.TestCase):
    def setUp(self):
        self.store = StoreConfig(aov=20000, aov_sigma=0.55, promo_package=True)
        self.benefits = [
            ben(id="base", rate=0.07, eligibility="all"),
            ben(id="pkg", rate=0.05, eligibility="promo_package"),
            ben(id="plus", rate=0.02, eligibility="bonus_store_plus"),
        ]

    def test_split_between_mall_wide_and_gated(self):
        part = Participation(promo_package=True, bonus_store_plus=True)
        mall_wide, gated = rate_by_scope(self.benefits, part, self.store)
        self.assertAlmostEqual(mall_wide, 0.07, places=4)
        self.assertAlmostEqual(gated, 0.07, places=4)  # 5% + 2%

    def test_without_entry_the_plus_row_is_excluded(self):
        part = Participation(promo_package=True, bonus_store_plus=False)
        mall_wide, gated = rate_by_scope(self.benefits, part, self.store)
        self.assertAlmostEqual(mall_wide, 0.07, places=4)
        self.assertAlmostEqual(gated, 0.05, places=4)

    def test_perceived_total_is_sum_of_scopes(self):
        part = Participation(promo_package=True, bonus_store_plus=True)
        mall_wide, gated = rate_by_scope(self.benefits, part, self.store)
        self.assertAlmostEqual(
            perceived_total_rate(self.benefits, part, self.store),
            mall_wide + gated,
            places=9,
        )

    def test_nominal_ignores_caps_and_minimums(self):
        part = Participation(promo_package=True, bonus_store_plus=True)
        capped = [ben(id="x", rate=0.05, min_order_yen=999999, user_cap_yen=1)]
        self.assertAlmostEqual(nominal_total_rate(capped, part, 20000), 0.05)
        self.assertLess(perceived_total_rate(capped, part, self.store), 0.001)

    def test_nominal_excludes_coupons(self):
        part = Participation(promo_package=True)
        items = [ben(id="c", coupon_yen=1000, eligibility="promo_package")]
        self.assertEqual(nominal_total_rate(items, part, 20000), 0.0)


class TestStoreFunding(unittest.TestCase):
    def setUp(self):
        self.store = StoreConfig(aov=20000, aov_sigma=0.55, point_cap_per_order=5000)

    def test_mall_funded_rows_are_not_charged(self):
        benefits = [ben(id="m", rate=0.05, funding="mall")]
        part = Participation()
        self.assertEqual(store_funded_rate(benefits, part, self.store, 0.0), 0.0)

    def test_store_funded_rows_are_charged(self):
        benefits = [ben(id="s", rate=0.01, funding="store")]
        part = Participation()
        self.assertAlmostEqual(
            store_funded_rate(benefits, part, self.store, 0.0), 0.01, places=4
        )

    def test_own_rate_adds_to_cost(self):
        benefits = [ben(id="s", rate=0.01, funding="store")]
        part = Participation()
        self.assertAlmostEqual(
            store_funded_rate(benefits, part, self.store, 0.02), 0.03, places=4
        )


class TestCostAndBreakeven(unittest.TestCase):
    def test_cost_includes_natural_orders(self):
        self.assertAlmostEqual(point_cost(100, 20000, 0.04), 80000)

    def test_fee_applied(self):
        self.assertAlmostEqual(point_cost(100, 20000, 0.04, 0.1), 88000)

    def test_breakeven_formula(self):
        store = StoreConfig(gross_margin_rate=0.32, aov=20000, point_cap_per_order=0)
        be = breakeven_uplift_ratio(store, 0.04)
        self.assertAlmostEqual(be, 0.04 / (0.32 - 0.04), places=4)

    def test_breakeven_infinite_when_rate_exceeds_margin(self):
        store = StoreConfig(gross_margin_rate=0.02, aov=20000, point_cap_per_order=0)
        self.assertEqual(breakeven_uplift_ratio(store, 0.05), float("inf"))


if __name__ == "__main__":
    unittest.main()
