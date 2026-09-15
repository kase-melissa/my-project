import unittest
from datetime import date

from bonus_planner.config import AppConfig, BehaviorParams, StoreConfig
from bonus_planner.models import PromoEvent, PromoSchedule
from bonus_planner.planner import build_entry_units

D = date


def schedule(*events):
    return PromoSchedule(
        month="2026-10", first_day=D(2026, 10, 1), last_day=D(2026, 10, 31),
        events=tuple(events),
    )


def cfg(**store_kw):
    kw = dict(aov=20000, aov_sigma=0.55, point_cap_per_order=5000, gross_margin_rate=0.32)
    kw.update(store_kw)
    return AppConfig(store=StoreConfig(**kw), behavior=BehaviorParams(month={}))


class TestBuildEntryUnits(unittest.TestCase):
    def test_every_day_is_covered_exactly_once(self):
        units = build_entry_units(cfg(), schedule(), D(2026, 9, 15))
        days = [d for u in units for d in u.days]
        self.assertEqual(len(days), 31)
        self.assertEqual(len(set(days)), 31)

    def test_period_event_forms_single_unit(self):
        ev = PromoEvent(
            id="matsuri", name="超PayPay祭",
            dates=tuple(D(2026, 10, d) for d in range(20, 26)),
            entry_unit="period", entry_uplift=0.5, entry_deadline=D(2026, 10, 13),
        )
        units = build_entry_units(cfg(), schedule(ev), D(2026, 9, 15))
        period = [u for u in units if len(u.days) > 1]
        self.assertEqual(len(period), 1)
        self.assertEqual(len(period[0].days), 6)
        self.assertEqual(period[0].entry_deadline, D(2026, 10, 13))

    def test_overlapping_periods_merge(self):
        a = PromoEvent(id="a", name="爆買いWEEK",
                       dates=tuple(D(2026, 10, d) for d in range(12, 17)),
                       entry_unit="period", entry_uplift=0.3)
        b = PromoEvent(id="b", name="超PayPay祭",
                       dates=tuple(D(2026, 10, d) for d in range(15, 21)),
                       entry_unit="period", entry_uplift=0.5)
        units = build_entry_units(cfg(), schedule(a, b), D(2026, 9, 15))
        merged = [u for u in units if len(u.days) > 1]
        self.assertEqual(len(merged), 1)
        self.assertEqual(len(merged[0].days), 9)  # 10/12〜10/20

    def test_deadline_in_past_blocks_unit(self):
        ev = PromoEvent(id="a", name="感謝デー", dates=(D(2026, 10, 8),),
                        entry_uplift=0.36, entry_deadline=D(2026, 10, 5))
        units = build_entry_units(cfg(), schedule(ev), D(2026, 10, 6))
        u = next(u for u in units if D(2026, 10, 8) in u.days)
        self.assertIsNotNone(u.blocked_reason)
        self.assertIn("締切", u.blocked_reason)

    def test_past_days_blocked(self):
        units = build_entry_units(cfg(), schedule(), D(2026, 10, 20))
        u = next(u for u in units if u.days == [D(2026, 10, 1)])
        self.assertIsNotNone(u.blocked_reason)

    def test_min_rate_constrains_period_options(self):
        ev = PromoEvent(id="a", name="超PayPay祭",
                        dates=tuple(D(2026, 10, d) for d in range(20, 23)),
                        entry_unit="period", entry_uplift=0.5, min_bonus_rate=0.03)
        units = build_entry_units(cfg(), schedule(ev), D(2026, 9, 15))
        u = next(u for u in units if len(u.days) > 1)
        self.assertGreaterEqual(min(o.bonus_rate for o in u.options), 0.03)

    def test_period_options_sum_over_days(self):
        ev = PromoEvent(id="a", name="爆買いWEEK",
                        dates=tuple(D(2026, 10, d) for d in range(12, 17)),
                        entry_unit="period", entry_uplift=0.3)
        units = build_entry_units(cfg(), schedule(ev), D(2026, 9, 15))
        u = next(u for u in units if len(u.days) > 1)
        opt = u.options[0]
        self.assertEqual(len(opt.estimates), 5)
        self.assertAlmostEqual(opt.cost, sum(e.point_cost for e in opt.estimates))
        self.assertAlmostEqual(opt.value, sum(e.net_value for e in opt.estimates))

    def test_required_events_recorded(self):
        ev = PromoEvent(id="a", name="プレミアムな日曜日", dates=(D(2026, 10, 4),),
                        entry_required=True, entry_uplift=0.38)
        units = build_entry_units(cfg(), schedule(ev), D(2026, 9, 15))
        u = next(u for u in units if D(2026, 10, 4) in u.days)
        self.assertEqual(u.entry_required_events, ["プレミアムな日曜日"])

    def test_mandatory_flag_from_config(self):
        ev = PromoEvent(id="matsuri", name="超PayPay祭", dates=(D(2026, 10, 20),),
                        entry_uplift=0.5)
        c = cfg(mandatory_event_ids=["matsuri"])
        units = build_entry_units(c, schedule(ev), D(2026, 9, 15))
        u = next(u for u in units if D(2026, 10, 20) in u.days)
        self.assertTrue(u.mandatory)

    def test_event_day_has_higher_value_than_plain_day(self):
        ev = PromoEvent(id="a", name="5のつく日", dates=(D(2026, 10, 15),),
                        traffic_multiplier=1.55, entry_uplift=0.42)
        units = build_entry_units(cfg(), schedule(ev), D(2026, 9, 15))
        ev_unit = next(u for u in units if D(2026, 10, 15) in u.days)
        plain = next(u for u in units if u.days == [D(2026, 10, 16)])
        best_ev = max(o.value for o in ev_unit.options)
        best_plain = max(o.value for o in plain.options)
        self.assertGreater(best_ev, best_plain * 2)


if __name__ == "__main__":
    unittest.main()
