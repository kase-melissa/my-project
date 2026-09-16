"""実データ一式でCLIを端から端まで動かす."""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from bonus_planner.behavior import build_day_context
from bonus_planner.cli import main
from bonus_planner.config import AppConfig
from bonus_planner.economics import perceived_total_rate, rate_by_scope
from bonus_planner.optimizer import optimize
from bonus_planner.planner import build_entry_units
from bonus_planner.report import display_width, render_html, render_markdown
from bonus_planner.schedule import load_schedule

ROOT = Path(__file__).resolve().parent.parent
SCHEDULE = ROOT / "data" / "promo_schedule" / "2026-10.yaml"
CONFIG = ROOT / "config" / "config.yaml"
BEHAVIOR = ROOT / "config" / "behavior_priors.yaml"
TODAY = "2026-09-15"
BSPLUS_DAYS = {2, 7, 16, 20, 27, 28}


def run_plan(out_dir, extra=None):
    args = [
        "plan", "--schedule", str(SCHEDULE), "--config", str(CONFIG),
        "--behavior", str(BEHAVIOR), "--today", TODAY, "--out", str(out_dir), "--quiet",
    ]
    return main(args + (extra or []))


def load_result(out_dir):
    return json.loads((Path(out_dir) / "bonus_store_plan_2026-10.json").read_text("utf-8"))


class TestCli(unittest.TestCase):
    def test_plan_writes_all_formats(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(run_plan(td), 0)
            stem = Path(td) / "bonus_store_plan_2026-10"
            for ext in ("md", "csv", "json", "html"):
                self.assertTrue(stem.with_suffix(f".{ext}").exists(), ext)

    def test_json_payload_shape(self):
        with tempfile.TemporaryDirectory() as td:
            run_plan(td)
            data = load_result(td)
            self.assertEqual(data["month"], "2026-10")
            self.assertEqual(data["objective"], "gmv")
            self.assertTrue(data["participation"]["promo_package"])
            self.assertFalse(data["participation"]["excellent_store"])
            self.assertGreater(len(data["entries"]), 0)

    def test_budget_is_respected(self):
        with tempfile.TemporaryDirectory() as td:
            run_plan(td, ["--budget", "200000"])
            self.assertLessEqual(load_result(td)["summary"]["point_cost"], 200000)

    def test_bigger_budget_never_reduces_gmv(self):
        values = []
        for b in ("100000", "300000", "1000000"):
            with tempfile.TemporaryDirectory() as td:
                run_plan(td, ["--budget", b])
                values.append(load_result(td)["summary"]["incremental_gmv"])
        self.assertEqual(values, sorted(values))

    def test_profit_objective_yields_higher_net_value(self):
        results = {}
        for obj in ("gmv", "profit"):
            with tempfile.TemporaryDirectory() as td:
                run_plan(td, ["--objective", obj])
                results[obj] = load_result(td)["summary"]
        self.assertGreaterEqual(results["profit"]["net_value"], results["gmv"]["net_value"])
        self.assertGreaterEqual(
            results["gmv"]["incremental_gmv"], results["profit"]["incremental_gmv"]
        )

    def test_check_command(self):
        self.assertEqual(
            main(["check", "--schedule", str(SCHEDULE), "--config", str(CONFIG),
                  "--behavior", str(BEHAVIOR), "--today", TODAY]), 0,
        )

    def test_check_flags_overdue_deadlines(self):
        rc = main(["check", "--schedule", str(SCHEDULE), "--config", str(CONFIG),
                   "--behavior", str(BEHAVIOR), "--today", "2026-10-20"])
        self.assertEqual(rc, 1)

    def test_missing_file_returns_error_code(self):
        self.assertEqual(
            main(["check", "--schedule", "does-not-exist.yaml", "--config", str(CONFIG),
                  "--behavior", str(BEHAVIOR)]), 2,
        )


class TestPlanInvariants(unittest.TestCase):
    def setUp(self):
        self.cfg = AppConfig.load(CONFIG, BEHAVIOR)
        self.schedule = load_schedule(SCHEDULE)
        self.today = date.fromisoformat(TODAY)
        units = build_entry_units(self.cfg, self.schedule, self.today)
        self.plan = optimize(self.cfg, units, self.schedule.month, self.today)

    def test_all_bsplus_days_are_entered(self):
        """参加でモール負担の+2%が開く6日は、原資ほぼゼロで取れるので必ず入るはず."""
        selected = {d.day for d in self.plan.selected_days() if d.month == 10}
        self.assertTrue(
            BSPLUS_DAYS <= selected,
            f"未採用のボーナスストアPlus指定日: {sorted(BSPLUS_DAYS - selected)}",
        )

    def test_mall_funded_benefits_never_enter_cost(self):
        """原資は自社負担分(ストアポイント1%+自社設定率)の増分に収まること."""
        for _u, opt in self.plan.selected:
            for est in opt.estimates:
                ceiling = est.entry_gmv * (0.01 + est.store_rate) * 1.001
                self.assertLessEqual(est.entry_point_cost, ceiling, f"{est.day}")

    def test_cost_is_marginal(self):
        for _u, opt in self.plan.selected:
            for est in opt.estimates:
                self.assertAlmostEqual(
                    est.point_cost, est.entry_point_cost - est.base_point_cost, places=4
                )

    def test_every_day_is_decided(self):
        decided = set(self.plan.selected_days())
        for u, _o, _r in self.plan.rejected:
            decided.update(u.days)
        for u in self.plan.blocked:
            decided.update(u.days)
        for d in self.schedule.days():
            self.assertIn(d, decided, f"{d} が未判断")

    def test_selected_meet_roas_floor(self):
        for _u, opt in self.plan.selected:
            self.assertGreaterEqual(opt.roas, self.cfg.store.min_roas)

    def test_no_day_selected_twice(self):
        days = [d for u, _ in self.plan.selected for d in u.days]
        self.assertEqual(len(days), len(set(days)))

    def test_bakugai_week_is_all_or_nothing(self):
        days = {date(2026, 10, 30), date(2026, 10, 31), date(2026, 11, 1)}
        chosen = self.plan.selected_days() & days
        self.assertIn(len(chosen), (0, 3), f"爆買WEEKが部分採用されている: {chosen}")

    def test_excellent_store_row_is_inactive(self):
        """優良ストア非該当なので +3% 行は開かない."""
        for d in (date(2026, 10, d) for d in BSPLUS_DAYS):
            ctx = build_day_context(self.schedule, d)
            _mall, gated = rate_by_scope(
                ctx.benefits, self.cfg.store.participation(True), self.cfg.store
            )
            self.assertLess(gated, 0.03, f"{d}: 優良ストア分が算入されている")

    def test_store_point_is_in_baseline_not_in_entry_decision(self):
        """ストアポイント1%は参加有無に関わらずかかるので基準線側にある."""
        for _u, opt in self.plan.selected:
            for est in opt.estimates:
                self.assertGreater(est.base_point_cost, 0)


class TestReport(unittest.TestCase):
    def setUp(self):
        self.cfg = AppConfig.load(CONFIG, BEHAVIOR)
        self.schedule = load_schedule(SCHEDULE)
        today = date.fromisoformat(TODAY)
        units = build_entry_units(self.cfg, self.schedule, today)
        self.plan = optimize(self.cfg, units, self.schedule.month, today)
        self.md = render_markdown(self.cfg, self.schedule, self.plan)

    def test_has_all_sections(self):
        for heading in ("前提と校正状況", "サマリー", "締切アラート", "カレンダー",
                        "推奨エントリー明細", "見送り判断", "日別の付与率内訳",
                        "運用上の注意", "損益分岐"):
            self.assertIn(heading, self.md)

    def test_states_uncalibrated_coefficients(self):
        self.assertIn("初期仮値", self.md)
        self.assertIn("曜日係数", self.md)
        self.assertIn("market_elasticity", self.md)
        self.assertNotIn("rate_elasticity", self.md)

    def test_calibration_status_reflects_calibrated_model(self):
        """校正済みモデルでは集客・転換率が「校正済み」と表示されること."""
        from dataclasses import replace

        from bonus_planner.config import AppConfig as _AppConfig

        calibrated = _AppConfig(
            store=self.cfg.store,
            behavior=replace(
                self.cfg.behavior,
                calibration_note="月次実績13ヶ月(2025-09〜2026-09)で校正。曜日係数は未校正(仮値)",
            ),
        )
        md = render_markdown(calibrated, self.schedule, self.plan)
        self.assertIn("| 基準セッション数 `base_sessions` | **実測で校正済み** |", md)
        self.assertIn("| 基準転換率 `base_cvr` | **実測で校正済み** |", md)
        self.assertIn("| 曜日係数 | **初期仮値**", md)

    def test_states_funding_assumption(self):
        self.assertIn("モール負担", self.md)

    def test_warns_about_no_preannouncement(self):
        self.assertIn("訴求NG", self.md)

    def test_calendar_rows_are_aligned(self):
        block = self.md.split("```")[1].strip("\n").split("\n")
        widths = {display_width(line) for line in block}
        self.assertEqual(len(widths), 1, f"カレンダーの行幅が揃っていない: {widths}")

    def test_html_is_self_contained(self):
        html = render_html(self.cfg, self.schedule, self.plan)
        self.assertIn("<!doctype html>", html)
        self.assertNotIn("http://", html)

    def test_csv_columns_are_typed(self):
        import csv as _csv
        with tempfile.TemporaryDirectory() as td:
            run_plan(td)
            with (Path(td) / "bonus_store_plan_2026-10.csv").open(encoding="utf-8-sig") as f:
                rows = list(_csv.DictReader(f))
        self.assertEqual(len(rows), 32)
        for r in rows:
            if r["entry"] == "YES":
                self.assertEqual(r["reason"], "")
                float(r["incremental_gmv"])
            else:
                self.assertEqual(r["incremental_gmv"], "")
                self.assertNotEqual(r["reason"], "")


if __name__ == "__main__":
    unittest.main()
