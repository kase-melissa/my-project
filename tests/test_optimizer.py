import unittest
from datetime import date

from bonus_planner.config import AppConfig, BehaviorParams, StoreConfig
from bonus_planner.models import EntryOption, EntryUnit
from bonus_planner.optimizer import optimize

TODAY = date(2026, 9, 15)


def option(rate, cost, value, gmv=None):
    return EntryOption(
        bonus_rate=rate,
        cost=cost,
        value=value,
        incremental_gmv=gmv if gmv is not None else cost * 10,
        estimates=[],
    )


def unit(key, days, options, **kw):
    return EntryUnit(key=key, label=key, days=days, options=options, **kw)


def cfg(budget, min_roas=0.0, mandatory=None):
    return AppConfig(
        store=StoreConfig(
            monthly_point_budget=budget,
            min_roas=min_roas,
            min_net_value=0.0,
            mandatory_event_ids=mandatory or [],
        ),
        behavior=BehaviorParams(),
    )


class TestOptimizer(unittest.TestCase):
    def test_beats_greedy_by_ratio(self):
        # 価値/コスト比で貪欲に選ぶと A のみ(価値70k)になるが、
        # 最適解は B+C(価値109k)。DPならこれを取りこぼさない。
        units = [
            unit("A", [date(2026, 10, 1)], [option(0.02, 60000, 70000)]),
            unit("B", [date(2026, 10, 2)], [option(0.02, 50000, 55000)]),
            unit("C", [date(2026, 10, 3)], [option(0.02, 50000, 54000)]),
        ]
        plan = optimize(cfg(100000), units, "2026-10", TODAY)
        self.assertEqual({u.key for u, _ in plan.selected}, {"B", "C"})
        self.assertAlmostEqual(plan.total_value, 109000)

    def test_respects_budget(self):
        units = [
            unit(f"U{i}", [date(2026, 10, i + 1)], [option(0.02, 40000, 60000)])
            for i in range(10)
        ]
        plan = optimize(cfg(150000), units, "2026-10", TODAY)
        self.assertLessEqual(plan.total_cost, 150000)

    def test_picks_best_rate_per_unit(self):
        u = unit("A", [date(2026, 10, 1)], [
            option(0.01, 10000, 12000),
            option(0.04, 30000, 45000),
            option(0.05, 50000, 40000),
        ])
        plan = optimize(cfg(100000), [u], "2026-10", TODAY)
        self.assertEqual(len(plan.selected), 1)
        self.assertAlmostEqual(plan.selected[0][1].bonus_rate, 0.04)

    def test_only_one_option_per_unit(self):
        u = unit("A", [date(2026, 10, 1)], [
            option(0.01, 10000, 12000), option(0.02, 20000, 26000),
        ])
        plan = optimize(cfg(1000000), [u], "2026-10", TODAY)
        self.assertEqual(len(plan.selected), 1)

    def test_min_roas_filter_rejects_with_reason(self):
        u = unit("A", [date(2026, 10, 1)], [option(0.02, 10000, 5000, gmv=20000)])
        plan = optimize(cfg(1000000, min_roas=5.0), [u], "2026-10", TODAY)
        self.assertEqual(plan.selected, [])
        self.assertIn("ROAS", plan.rejected[0][2])

    def test_negative_value_rejected(self):
        u = unit("A", [date(2026, 10, 1)], [option(0.02, 10000, -500)])
        plan = optimize(cfg(1000000), [u], "2026-10", TODAY)
        self.assertEqual(plan.selected, [])
        self.assertIn("純増効果", plan.rejected[0][2])

    def test_budget_rejection_reason(self):
        units = [
            unit("A", [date(2026, 10, 1)], [option(0.02, 100000, 200000)]),
            unit("B", [date(2026, 10, 2)], [option(0.02, 100000, 10000)]),
        ]
        plan = optimize(cfg(100000), units, "2026-10", TODAY)
        self.assertEqual([u.key for u, _ in plan.selected], ["A"])
        self.assertIn("予算", plan.rejected[0][2])

    def test_blocked_units_excluded(self):
        u = unit("A", [date(2026, 10, 1)], [option(0.02, 1000, 99999)],
                 blocked_reason="エントリー締切を過ぎています")
        plan = optimize(cfg(1000000), [u], "2026-10", TODAY)
        self.assertEqual(plan.selected, [])
        self.assertEqual(len(plan.blocked), 1)

    def test_mandatory_unit_reserved_before_optimization(self):
        # 必須指定は採算に関わらず先に確保し、残予算で他を最適化する
        mandatory = unit("M", [date(2026, 10, 1)], [option(0.02, 80000, 1000)], mandatory=True)
        other = unit("O", [date(2026, 10, 2)], [option(0.02, 80000, 500000)])
        plan = optimize(cfg(100000), [mandatory, other], "2026-10", TODAY)
        keys = {u.key for u, _ in plan.selected}
        self.assertIn("M", keys)
        self.assertNotIn("O", keys)

    def test_period_unit_is_all_or_nothing(self):
        days = [date(2026, 10, d) for d in range(20, 26)]
        u = unit("P", days, [option(0.02, 90000, 120000)])
        plan = optimize(cfg(100000), [u], "2026-10", TODAY)
        self.assertEqual(len(plan.selected_days()), 6)

    def test_zero_budget_selects_nothing(self):
        u = unit("A", [date(2026, 10, 1)], [option(0.02, 1000, 99999)])
        plan = optimize(cfg(0), [u], "2026-10", TODAY)
        self.assertEqual(plan.selected, [])

    def test_roas_and_totals(self):
        u = unit("A", [date(2026, 10, 1)], [option(0.02, 10000, 20000, gmv=100000)])
        plan = optimize(cfg(1000000), [u], "2026-10", TODAY)
        self.assertAlmostEqual(plan.total_roas, 10.0)
        self.assertAlmostEqual(plan.budget_used_ratio, 0.01)


if __name__ == "__main__":
    unittest.main()
