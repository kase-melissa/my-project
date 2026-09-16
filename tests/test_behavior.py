import unittest
from datetime import date

from bonus_planner.behavior import DemandModel, build_day_context, combine_with_decay
from bonus_planner.config import BehaviorParams, StoreConfig
from bonus_planner.models import Benefit, DayContext, Participation, PromoSchedule

DAY = date(2026, 10, 7)


def ben(**kw):
    base = dict(id="b", name="b", days=(DAY,))
    base.update(kw)
    return Benefit(**base)


class TestCombine(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(combine_with_decay([], 0.5), 0.0)

    def test_largest_first_regardless_of_order(self):
        a = combine_with_decay([1.0, 0.5], 0.5)
        b = combine_with_decay([0.5, 1.0], 0.5)
        self.assertAlmostEqual(a, b)
        self.assertAlmostEqual(a, 1.25)

    def test_sublinear(self):
        self.assertLess(combine_with_decay([0.4, 0.4, 0.4], 0.55), 1.2)


class TestDemandModel(unittest.TestCase):
    def setUp(self):
        self.p = BehaviorParams(base_sessions=1000.0, base_cvr=0.10, month={})
        self.store = StoreConfig(aov=20000, aov_sigma=0.55, promo_package=True)
        self.m = DemandModel(self.p, self.store)

    def ctx(self, benefits=None, day=DAY):
        return DayContext(day=day, benefits=benefits or [], weekday=day.weekday())

    # -- 市場規模 -----------------------------------------------------
    def test_market_factor_is_one_at_baseline(self):
        self.assertAlmostEqual(self.m.market_factor(self.p.baseline_rate), 1.0)

    def test_market_factor_grows_with_mall_wide_rate(self):
        self.assertGreater(self.m.market_factor(0.11), self.m.market_factor(0.07))

    def test_market_factor_is_sublinear(self):
        # 付与率を2倍にしても来訪は2倍にならない
        self.assertLess(self.m.market_factor(0.14), 2 * self.m.market_factor(0.07))

    # -- シェア -------------------------------------------------------
    def test_share_factor_is_one_without_advantage(self):
        self.assertAlmostEqual(self.m.share_factor(0.0), 1.0)

    def test_share_factor_at_reference_advantage(self):
        expected = 1.0 + self.p.share_gain_at_reference
        self.assertAlmostEqual(self.m.share_factor(self.p.reference_advantage), expected)

    def test_share_factor_is_independent_of_mall_wide_rate(self):
        """同じ上乗せなら、その日の基準率が高くてもシェア効果は同じ.

        顧客にとって2ポイント分の価値は基準率に依らず同じ金額のため。
        比率で見るとモールが最も集客している日を避ける提案になってしまう。
        シェア係数が全ストア共通の付与率を引数に取らないことで担保している。
        """
        import inspect

        params = inspect.signature(self.m.share_factor).parameters
        self.assertEqual(list(params), ["own_advantage"])

    def test_orders_is_sessions_times_cvr(self):
        benefits = [ben(id="base", rate=0.07, eligibility="all")]
        c = self.ctx(benefits)
        part = Participation(promo_package=True)
        self.assertAlmostEqual(
            self.m.orders(c, part), self.m.sessions(c, part) * self.m.cvr(c, part)
        )

    def test_cvr_is_capped_at_one(self):
        p = BehaviorParams(base_sessions=100.0, base_cvr=0.95, month={})
        m = DemandModel(p, self.store)
        benefits = [
            ben(id="base", rate=0.07, eligibility="all"),
            ben(id="big", rate=0.50, eligibility="promo_package"),
        ]
        c = self.ctx(benefits)
        self.assertLessEqual(m.cvr(c, Participation(promo_package=True)), 1.0)

    def test_entry_does_not_change_sessions(self):
        """参加してもモール全体の集客は変わらない。動くのは転換率だけ."""
        benefits = [
            ben(id="base", rate=0.07, eligibility="all"),
            ben(id="plus", rate=0.02, eligibility="bonus_store_plus"),
        ]
        c = self.ctx(benefits)
        out = Participation(promo_package=True, bonus_store_plus=False)
        inn = Participation(promo_package=True, bonus_store_plus=True)
        self.assertAlmostEqual(self.m.sessions(c, out), self.m.sessions(c, inn))
        self.assertGreater(self.m.cvr(c, inn), self.m.cvr(c, out))

    def test_share_factor_diminishes(self):
        one = self.m.share_factor(0.02) - 1.0
        two = self.m.share_factor(0.04) - 1.0
        self.assertGreater(two, one)
        self.assertLess(two, 2 * one)

    # -- 組み合わせ ---------------------------------------------------
    def test_entry_raises_orders_when_gated_benefit_exists(self):
        benefits = [
            ben(id="base", rate=0.07, eligibility="all"),
            ben(id="plus", rate=0.02, eligibility="bonus_store_plus"),
        ]
        c = self.ctx(benefits)
        out = self.m.orders(c, Participation(promo_package=True, bonus_store_plus=False))
        inn = self.m.orders(c, Participation(promo_package=True, bonus_store_plus=True))
        self.assertGreater(inn, out)

    def test_entry_does_nothing_without_gated_benefit(self):
        benefits = [ben(id="base", rate=0.07, eligibility="all")]
        c = self.ctx(benefits)
        out = self.m.orders(c, Participation(promo_package=True, bonus_store_plus=False))
        inn = self.m.orders(c, Participation(promo_package=True, bonus_store_plus=True))
        self.assertAlmostEqual(out, inn)

    def test_all_store_benefit_raises_market_not_share(self):
        plain = self.ctx([ben(id="base", rate=0.07, eligibility="all")])
        big = self.ctx([ben(id="base", rate=0.11, eligibility="all")])
        part = Participation(promo_package=True)
        self.assertGreater(self.m.orders(big, part), self.m.orders(plain, part))
        # シェア側は動いていない
        self.assertAlmostEqual(self.m.scoped_rates(big, part)[1], 0.0)

    def test_traffic_multiplier_defaults_to_one(self):
        c = self.ctx([ben(id="base", rate=0.07, eligibility="all")])
        self.assertAlmostEqual(self.m.traffic_multiplier(c, Participation()), 1.0)

    def test_traffic_multiplier_applies_when_set(self):
        c = self.ctx([ben(id="ad", rate=0.07, eligibility="all", traffic_multiplier=1.3)])
        self.assertAlmostEqual(self.m.traffic_multiplier(c, Participation()), 1.3)

    def test_calendar_factor_uses_weekday_and_payday(self):
        sunday = self.ctx(day=date(2026, 10, 25))
        tuesday = self.ctx(day=date(2026, 10, 6))
        self.assertGreater(self.m.calendar_factor(sunday), self.m.calendar_factor(tuesday))

    def test_build_day_context_flags(self):
        sched = PromoSchedule(
            month="2026-10", first_day=date(2026, 10, 1), last_day=date(2026, 10, 31),
            benefits=(ben(days=(date(2026, 10, 25),), rate=0.04),),
        )
        c = build_day_context(sched, date(2026, 10, 25))
        self.assertTrue(c.is_five_day)
        self.assertTrue(c.is_payday_window)
        self.assertEqual(len(c.benefits), 1)


if __name__ == "__main__":
    unittest.main()
