import unittest
from datetime import date

from bonus_planner.behavior import DemandModel, build_day_context
from bonus_planner.config import AppConfig, BehaviorParams, StoreConfig
from bonus_planner.models import Benefit, PromoSchedule
from bonus_planner.planner import build_entry_units, estimate_day

D = date


def ben(**kw):
    base = dict(id="b", name="b", days=(D(2026, 10, 2),))
    base.update(kw)
    return Benefit(**base)


def schedule(*benefits, extends_to=None):
    return PromoSchedule(
        month="2026-10", first_day=D(2026, 10, 1), last_day=D(2026, 10, 31),
        benefits=tuple(benefits), extends_to=extends_to,
    )


def cfg(**store_kw):
    kw = dict(aov=20000, aov_sigma=0.55, point_cap_per_order=5000,
              gross_margin_rate=0.32, promo_package=True)
    kw.update(store_kw)
    return AppConfig(store=StoreConfig(**kw), behavior=BehaviorParams(month={}))


BASE = ben(id="base", name="定常", days=tuple(D(2026, 10, d) for d in range(1, 32)),
           rate=0.06, funding="mall", eligibility="all")
STORE_POINT = ben(id="sp", name="ストアポイント",
                  days=tuple(D(2026, 10, d) for d in range(1, 32)),
                  rate=0.01, funding="store", eligibility="all")
PLUS = ben(id="plus", name="BSPlus+2%", days=(D(2026, 10, 2),),
           rate=0.02, funding="mall", eligibility="bonus_store_plus")


class TestEstimateDay(unittest.TestCase):
    def setUp(self):
        self.cfg = cfg()
        self.sched = schedule(BASE, STORE_POINT, PLUS)
        self.model = DemandModel(self.cfg.behavior, self.cfg.store)

    def est(self, day, rate):
        return estimate_day(
            self.cfg, self.model, build_day_context(self.sched, day), rate
        )

    def test_unlocked_rate_on_bsplus_day(self):
        e = self.est(D(2026, 10, 2), 0.0)
        self.assertAlmostEqual(e.unlocked_mall_rate, 0.02, places=3)

    def test_no_unlock_on_plain_day(self):
        e = self.est(D(2026, 10, 3), 0.0)
        self.assertAlmostEqual(e.unlocked_mall_rate, 0.0, places=6)

    def test_free_unlock_costs_only_store_point_on_increment(self):
        """自社上乗せ0%なら、増分原資は増えた注文へのストアポイント分だけ."""
        e = self.est(D(2026, 10, 2), 0.0)
        self.assertGreater(e.incremental_gmv, 0)
        self.assertAlmostEqual(e.point_cost, e.incremental_gmv * 0.01, delta=1.0)
        self.assertGreater(e.roas, 50)

    def test_plain_day_with_zero_own_rate_is_a_noop(self):
        e = self.est(D(2026, 10, 3), 0.0)
        self.assertAlmostEqual(e.incremental_gmv, 0.0, places=6)
        self.assertAlmostEqual(e.point_cost, 0.0, places=6)

    def test_mall_funded_rows_never_enter_cost(self):
        """モール負担の+2%は原資に入らない."""
        e = self.est(D(2026, 10, 2), 0.0)
        # 参加時の原資はストアポイント1%のみ
        self.assertAlmostEqual(
            e.entry_point_cost, e.entry_gmv * 0.01, delta=1.0
        )

    def test_own_rate_increases_cost(self):
        low = self.est(D(2026, 10, 2), 0.01)
        high = self.est(D(2026, 10, 2), 0.04)
        self.assertGreater(high.point_cost, low.point_cost)
        self.assertGreater(high.incremental_gmv, low.incremental_gmv)

    def test_cost_is_marginal_not_absolute(self):
        """基準線でも発生するストアポイント分は増分原資に含めない."""
        e = self.est(D(2026, 10, 2), 0.02)
        self.assertAlmostEqual(
            e.point_cost, e.entry_point_cost - e.base_point_cost, places=6
        )
        self.assertGreater(e.base_point_cost, 0)


class TestBuildEntryUnits(unittest.TestCase):
    def test_every_day_covered_once(self):
        units = build_entry_units(cfg(), schedule(BASE, STORE_POINT), D(2026, 9, 15))
        days = [d for u in units for d in u.days]
        self.assertEqual(len(days), 31)
        self.assertEqual(len(set(days)), 31)

    def test_period_benefit_forms_single_unit(self):
        period = ben(
            id="bakugai", name="爆買WEEK",
            days=tuple(D(2026, 10, d) for d in range(29, 32)),
            entry_unit="period", rate=0.04, eligibility="promo_package",
        )
        units = build_entry_units(cfg(), schedule(BASE, STORE_POINT, period), D(2026, 9, 15))
        merged = [u for u in units if len(u.days) > 1]
        self.assertEqual(len(merged), 1)
        self.assertEqual(len(merged[0].days), 3)

    def test_options_cover_all_configured_rates(self):
        c = cfg(store_bonus_rates=[0.0, 0.02, 0.05])
        units = build_entry_units(c, schedule(BASE, STORE_POINT, PLUS), D(2026, 9, 15))
        unit = next(u for u in units if D(2026, 10, 2) in u.days)
        self.assertEqual([o.store_rate for o in unit.options], [0.0, 0.02, 0.05])

    def test_deadline_in_past_blocks_unit(self):
        plus = ben(id="plus", name="BSPlus", days=(D(2026, 10, 2),), rate=0.02,
                   eligibility="bonus_store_plus", entry_deadline=D(2026, 9, 29))
        units = build_entry_units(cfg(), schedule(BASE, STORE_POINT, plus), D(2026, 9, 30))
        u = next(u for u in units if D(2026, 10, 2) in u.days)
        self.assertIsNotNone(u.blocked_reason)
        self.assertIn("締切", u.blocked_reason)

    def test_past_days_blocked(self):
        units = build_entry_units(cfg(), schedule(BASE, STORE_POINT), D(2026, 10, 20))
        u = next(u for u in units if u.days == [D(2026, 10, 1)])
        self.assertIsNotNone(u.blocked_reason)

    def test_gated_benefit_names_recorded(self):
        units = build_entry_units(cfg(), schedule(BASE, STORE_POINT, PLUS), D(2026, 9, 15))
        u = next(u for u in units if D(2026, 10, 2) in u.days)
        self.assertEqual(u.entry_required_benefits, ["BSPlus+2%"])

    def test_excellent_store_row_ignored_when_not_eligible(self):
        exc = ben(id="exc", name="優良+3%", days=(D(2026, 10, 2),), rate=0.03,
                  eligibility="bonus_store_plus_excellent")
        units = build_entry_units(
            cfg(excellent_store=False), schedule(BASE, STORE_POINT, PLUS, exc), D(2026, 9, 15)
        )
        u = next(u for u in units if D(2026, 10, 2) in u.days)
        self.assertEqual(u.entry_required_benefits, ["BSPlus+2%"])
        self.assertAlmostEqual(u.options[0].estimates[0].unlocked_mall_rate, 0.02, places=3)

    def test_excellent_store_adds_three_points(self):
        exc = ben(id="exc", name="優良+3%", days=(D(2026, 10, 2),), rate=0.03,
                  eligibility="bonus_store_plus_excellent")
        units = build_entry_units(
            cfg(excellent_store=True), schedule(BASE, STORE_POINT, PLUS, exc), D(2026, 9, 15)
        )
        u = next(u for u in units if D(2026, 10, 2) in u.days)
        self.assertAlmostEqual(u.options[0].estimates[0].unlocked_mall_rate, 0.05, places=3)

    def test_mandatory_flag_from_config(self):
        units = build_entry_units(
            cfg(mandatory_benefit_ids=["plus"]), schedule(BASE, STORE_POINT, PLUS),
            D(2026, 9, 15),
        )
        u = next(u for u in units if D(2026, 10, 2) in u.days)
        self.assertTrue(u.mandatory)


if __name__ == "__main__":
    unittest.main()
