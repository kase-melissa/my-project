import unittest
from datetime import date

from bonus_planner.config import AppConfig, BehaviorParams, StoreConfig
from bonus_planner.models import EntryOption, EntryUnit
from bonus_planner.optimizer import optimize

TODAY = date(2026, 9, 15)


def option(rate, cost, gmv, net=None):
    return EntryOption(
        store_rate=rate, cost=cost,
        net_value=net if net is not None else gmv * 0.1,
        incremental_gmv=gmv, estimates=[],
    )


def unit(key, days, options, **kw):
    return EntryUnit(key=key, label=key, days=days, options=options, **kw)


def cfg(budget, min_roas=0.0, mandatory=None):
    return AppConfig(
        store=StoreConfig(
            monthly_point_budget=budget, min_roas=min_roas, min_net_value=0.0,
            mandatory_benefit_ids=mandatory or [],
        ),
        behavior=BehaviorParams(),
    )


class TestObjective(unittest.TestCase):
    def test_gmv_objective_prefers_higher_gmv(self):
        u = unit("A", [date(2026, 10, 1)], [
            option(0.01, 50000, gmv=600000, net=50000),
            option(0.04, 50000, gmv=900000, net=40000),
        ])
        plan = optimize(cfg(100000), [u], "2026-10", TODAY, objective="gmv")
        self.assertAlmostEqual(plan.selected[0][1].store_rate, 0.04)

    def test_profit_objective_prefers_higher_net_value(self):
        u = unit("A", [date(2026, 10, 1)], [
            option(0.01, 50000, gmv=600000, net=50000),
            option(0.04, 50000, gmv=900000, net=40000),
        ])
        plan = optimize(cfg(100000), [u], "2026-10", TODAY, objective="profit")
        self.assertAlmostEqual(plan.selected[0][1].store_rate, 0.01)

    def test_rejects_unknown_objective(self):
        with self.assertRaises(ValueError):
            optimize(cfg(100000), [], "2026-10", TODAY, objective="revenue")

    def test_objective_recorded_in_result(self):
        plan = optimize(cfg(100000), [], "2026-10", TODAY, objective="profit")
        self.assertEqual(plan.objective, "profit")


class TestOptimizer(unittest.TestCase):
    def test_beats_greedy_by_ratio(self):
        # 費用対効果順に貪欲だと A のみ(GMV 70万)。最適は B+C(GMV 109万)。
        units = [
            unit("A", [date(2026, 10, 1)], [option(0.02, 60000, gmv=700000)]),
            unit("B", [date(2026, 10, 2)], [option(0.02, 50000, gmv=550000)]),
            unit("C", [date(2026, 10, 3)], [option(0.02, 50000, gmv=540000)]),
        ]
        plan = optimize(cfg(100000), units, "2026-10", TODAY)
        self.assertEqual({u.key for u, _ in plan.selected}, {"B", "C"})
        self.assertAlmostEqual(plan.total_incremental_gmv, 1090000)

    def test_respects_budget(self):
        units = [
            unit(f"U{i}", [date(2026, 10, i + 1)], [option(0.02, 40000, gmv=600000)])
            for i in range(10)
        ]
        plan = optimize(cfg(150000), units, "2026-10", TODAY)
        self.assertLessEqual(plan.total_cost, 150000)

    def test_zero_cost_option_is_always_affordable(self):
        """モール負担だけで開く日は原資ゼロでも採用できる."""
        u = unit("A", [date(2026, 10, 2)], [option(0.0, 0.0, gmv=200000)])
        plan = optimize(cfg(1000), [u], "2026-10", TODAY)
        self.assertEqual(len(plan.selected), 1)
        self.assertEqual(plan.total_cost, 0.0)

    def test_only_one_option_per_unit(self):
        u = unit("A", [date(2026, 10, 1)], [
            option(0.01, 10000, gmv=120000), option(0.02, 20000, gmv=260000),
        ])
        plan = optimize(cfg(1000000), [u], "2026-10", TODAY)
        self.assertEqual(len(plan.selected), 1)

    def test_min_roas_filter_rejects_with_reason(self):
        u = unit("A", [date(2026, 10, 1)], [option(0.02, 10000, gmv=20000)])
        plan = optimize(cfg(1000000, min_roas=5.0), [u], "2026-10", TODAY)
        self.assertEqual(plan.selected, [])
        self.assertIn("ROAS", plan.rejected[0][2])

    def test_zero_gmv_rejected(self):
        u = unit("A", [date(2026, 10, 1)], [option(0.0, 0.0, gmv=0.0, net=0.0)])
        plan = optimize(cfg(1000000), [u], "2026-10", TODAY)
        self.assertEqual(plan.selected, [])
        self.assertIn("増分GMV", plan.rejected[0][2])

    def test_negative_net_value_rejected(self):
        u = unit("A", [date(2026, 10, 1)], [option(0.02, 10000, gmv=100000, net=-500)])
        plan = optimize(cfg(1000000), [u], "2026-10", TODAY)
        self.assertEqual(plan.selected, [])
        self.assertIn("純増効果", plan.rejected[0][2])

    def test_budget_rejection_reason(self):
        units = [
            unit("A", [date(2026, 10, 1)], [option(0.02, 100000, gmv=2000000)]),
            unit("B", [date(2026, 10, 2)], [option(0.02, 100000, gmv=100000)]),
        ]
        plan = optimize(cfg(100000), units, "2026-10", TODAY)
        self.assertEqual([u.key for u, _ in plan.selected], ["A"])
        self.assertIn("予算", plan.rejected[0][2])

    def test_blocked_units_excluded(self):
        u = unit("A", [date(2026, 10, 1)], [option(0.02, 1000, gmv=999999)],
                 blocked_reason="エントリー締切を過ぎています")
        plan = optimize(cfg(1000000), [u], "2026-10", TODAY)
        self.assertEqual(plan.selected, [])
        self.assertEqual(len(plan.blocked), 1)

    def test_mandatory_unit_reserved_first(self):
        mandatory = unit("M", [date(2026, 10, 1)], [option(0.02, 80000, gmv=100000)],
                         mandatory=True)
        other = unit("O", [date(2026, 10, 2)], [option(0.02, 80000, gmv=5000000)])
        plan = optimize(cfg(100000), [mandatory, other], "2026-10", TODAY)
        keys = {u.key for u, _ in plan.selected}
        self.assertIn("M", keys)
        self.assertNotIn("O", keys)

    def test_period_unit_is_all_or_nothing(self):
        days = [date(2026, 10, d) for d in range(29, 32)]
        u = unit("P", days, [option(0.02, 90000, gmv=1200000)])
        plan = optimize(cfg(100000), [u], "2026-10", TODAY)
        self.assertEqual(len(plan.selected_days()), 3)

    def test_totals_and_roas(self):
        u = unit("A", [date(2026, 10, 1)], [option(0.02, 10000, gmv=100000, net=20000)])
        plan = optimize(cfg(1000000), [u], "2026-10", TODAY)
        self.assertAlmostEqual(plan.total_roas, 10.0)
        self.assertAlmostEqual(plan.budget_used_ratio, 0.01)
        self.assertAlmostEqual(plan.total_net_value, 20000)


if __name__ == "__main__":
    unittest.main()
