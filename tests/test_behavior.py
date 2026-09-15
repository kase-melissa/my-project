import unittest
from datetime import date

from bonus_planner.behavior import DemandModel, build_day_context, combine_with_decay
from bonus_planner.config import BehaviorParams
from bonus_planner.models import DayContext, PromoEvent, PromoSchedule


def event(**kw):
    base = dict(id="e", name="e", dates=(date(2026, 10, 4),))
    base.update(kw)
    return PromoEvent(**base)


class TestCombine(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(combine_with_decay([], 0.5), 0.0)

    def test_single(self):
        self.assertAlmostEqual(combine_with_decay([0.5], 0.5), 0.5)

    def test_largest_first(self):
        # 順序に関わらず大きい効果が満額、2つ目が逓減する
        a = combine_with_decay([1.0, 0.5], 0.5)
        b = combine_with_decay([0.5, 1.0], 0.5)
        self.assertAlmostEqual(a, b)
        self.assertAlmostEqual(a, 1.25)

    def test_sublinear(self):
        # 単純加算より必ず小さい
        self.assertLess(combine_with_decay([0.4, 0.4, 0.4], 0.55), 1.2)


class TestDemandModel(unittest.TestCase):
    def setUp(self):
        self.p = BehaviorParams(base_orders=100.0, month={})
        self.m = DemandModel(self.p)

    def ctx(self, day=date(2026, 10, 7), events=None):
        return DayContext(
            day=day,
            events=events or [],
            is_five_day=day.day in (5, 15, 25),
            is_zorome=day.day in (11, 22),
            weekday=day.weekday(),
        )

    def test_rate_response_is_one_at_reference(self):
        self.assertAlmostEqual(self.m.rate_response(self.p.reference_rate), 1.0)

    def test_rate_response_is_sublinear(self):
        # 還元率を2倍にしても効果は2倍にならない
        r1 = self.m.rate_response(0.02)
        r2 = self.m.rate_response(0.04)
        self.assertLess(r2, 2 * r1)
        self.assertGreater(r2, r1)

    def test_five_day_lifts_baseline(self):
        plain = self.m.base_orders(self.ctx(date(2026, 10, 7)))
        five = self.m.base_orders(self.ctx(date(2026, 10, 15)))
        self.assertGreater(five / plain, 1.3)

    def test_explicit_event_suppresses_implicit_five_day(self):
        # 公式スケジュールに明記がある日は暗黙の暦イベントを二重計上しない
        ev = event(dates=(date(2026, 10, 15),), traffic_multiplier=1.2, entry_uplift=0.3)
        c = self.ctx(date(2026, 10, 15), [ev])
        self.assertEqual(self.m.implicit_events(c), [])
        self.assertAlmostEqual(self.m.traffic_multiplier(c), 1.2)

    def test_entry_uplift_larger_on_event_day(self):
        ev = event(traffic_multiplier=1.4, entry_uplift=0.38)
        on_event = self.m.entry_uplift(self.ctx(date(2026, 10, 4), [ev]), 0.04)
        on_plain = self.m.entry_uplift(self.ctx(date(2026, 10, 7)), 0.04)
        self.assertGreater(on_event, on_plain)

    def test_uplift_never_below_normal_day_floor(self):
        ev = event(entry_uplift=0.01, entry_required=False)
        c = self.ctx(date(2026, 10, 7), [ev])
        self.assertGreaterEqual(self.m.max_entry_uplift(c), self.p.normal_day_uplift)

    def test_min_rate_from_events(self):
        ev = event(entry_uplift=0.3, min_bonus_rate=0.03)
        c = self.ctx(date(2026, 10, 4), [ev])
        self.assertEqual(self.m.required_min_rate(c), 0.03)
        self.assertEqual(min(self.m.allowed_rates(c)), 0.03)

    def test_allowed_rates_intersect_event_whitelist(self):
        ev = event(entry_uplift=0.3, allowed_rates=(0.02, 0.03))
        c = self.ctx(date(2026, 10, 4), [ev])
        self.assertEqual(self.m.allowed_rates(c), [0.02, 0.03])

    def test_min_rate_outside_candidates_still_usable(self):
        ev = event(entry_uplift=0.3, min_bonus_rate=0.08)
        c = self.ctx(date(2026, 10, 4), [ev])
        self.assertEqual(self.m.allowed_rates(c), [0.08])

    def test_aov_multiplier_capped(self):
        evs = [
            event(id=f"e{i}", dates=(date(2026, 10, 4),), entry_uplift=0.2, aov_multiplier=1.5)
            for i in range(4)
        ]
        c = self.ctx(date(2026, 10, 4), evs)
        self.assertLessEqual(self.m.aov_multiplier(c), self.p.aov_event_lift_cap)

    def test_build_day_context_flags(self):
        sched = PromoSchedule(
            month="2026-10", first_day=date(2026, 10, 1), last_day=date(2026, 10, 31),
            events=(event(dates=(date(2026, 10, 25),), entry_uplift=0.4),),
        )
        c = build_day_context(sched, date(2026, 10, 25))
        self.assertTrue(c.is_five_day)
        self.assertTrue(c.is_payday_window)
        self.assertEqual(len(c.events), 1)


if __name__ == "__main__":
    unittest.main()
