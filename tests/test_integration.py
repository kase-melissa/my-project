"""サンプル一式でCLIを端から端まで動かす."""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from bonus_planner.cli import main
from bonus_planner.config import AppConfig
from bonus_planner.optimizer import optimize
from bonus_planner.planner import build_entry_units
from bonus_planner.report import display_width, render_html, render_markdown
from bonus_planner.schedule import load_schedule

ROOT = Path(__file__).resolve().parent.parent
SCHEDULE = ROOT / "data" / "promo_schedule" / "2026-10.yaml"
CONFIG = ROOT / "config" / "config.yaml"
BEHAVIOR = ROOT / "config" / "behavior_priors.yaml"
TODAY = "2026-09-15"


def run_plan(out_dir, extra=None):
    args = [
        "plan", "--schedule", str(SCHEDULE), "--config", str(CONFIG),
        "--behavior", str(BEHAVIOR), "--today", TODAY, "--out", str(out_dir), "--quiet",
    ]
    return main(args + (extra or []))


class TestCli(unittest.TestCase):
    def test_plan_writes_all_formats(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(run_plan(td), 0)
            stem = Path(td) / "bonus_store_plan_2026-10"
            for ext in ("md", "csv", "json", "html"):
                self.assertTrue(stem.with_suffix(f".{ext}").exists(), ext)

    def test_csv_columns_are_typed(self):
        # 見送り理由を数値列に混ぜない(Excelで集計できなくなる)
        import csv as _csv

        with tempfile.TemporaryDirectory() as td:
            run_plan(td)
            with (Path(td) / "bonus_store_plan_2026-10.csv").open(encoding="utf-8-sig") as f:
                rows = list(_csv.DictReader(f))
        self.assertTrue(rows)
        for r in rows:
            if r["entry"] == "YES":
                self.assertEqual(r["reason"], "")
                float(r["net_value"])
            else:
                self.assertEqual(r["net_value"], "")
                self.assertNotEqual(r["reason"], "")

    def test_json_payload_shape(self):
        with tempfile.TemporaryDirectory() as td:
            run_plan(td)
            data = json.loads((Path(td) / "bonus_store_plan_2026-10.json").read_text("utf-8"))
            self.assertEqual(data["month"], "2026-10")
            self.assertIn("summary", data)
            self.assertGreater(len(data["entries"]), 0)
            for e in data["entries"]:
                self.assertGreater(e["bonus_rate"], 0)
                self.assertGreater(len(e["days"]), 0)

    def test_budget_is_respected(self):
        with tempfile.TemporaryDirectory() as td:
            run_plan(td, ["--budget", "200000"])
            data = json.loads((Path(td) / "bonus_store_plan_2026-10.json").read_text("utf-8"))
            self.assertLessEqual(data["summary"]["point_cost"], 200000)

    def test_bigger_budget_never_reduces_value(self):
        values = []
        for b in ("200000", "500000", "1500000"):
            with tempfile.TemporaryDirectory() as td:
                run_plan(td, ["--budget", b])
                data = json.loads(
                    (Path(td) / "bonus_store_plan_2026-10.json").read_text("utf-8")
                )
                values.append(data["summary"]["net_value"])
        self.assertEqual(values, sorted(values))

    def test_check_command(self):
        self.assertEqual(
            main(["check", "--schedule", str(SCHEDULE), "--config", str(CONFIG),
                  "--behavior", str(BEHAVIOR), "--today", TODAY]),
            0,
        )

    def test_check_flags_overdue_deadlines(self):
        # 締切を過ぎた状態で実行すると警告(終了コード1)
        rc = main(["check", "--schedule", str(SCHEDULE), "--config", str(CONFIG),
                   "--behavior", str(BEHAVIOR), "--today", "2026-10-20"])
        self.assertEqual(rc, 1)

    def test_missing_file_returns_error_code(self):
        self.assertEqual(
            main(["check", "--schedule", "does-not-exist.yaml", "--config", str(CONFIG),
                  "--behavior", str(BEHAVIOR)]),
            2,
        )


class TestPlanInvariants(unittest.TestCase):
    def setUp(self):
        self.cfg = AppConfig.load(CONFIG, BEHAVIOR)
        self.schedule = load_schedule(SCHEDULE)
        self.today = date.fromisoformat(TODAY)
        units = build_entry_units(self.cfg, self.schedule, self.today)
        self.plan = optimize(self.cfg, units, self.schedule.month, self.today)

    def test_selected_rates_meet_event_minimums(self):
        for unit, opt in self.plan.selected:
            for day in unit.days:
                for ev in self.schedule.events_on(day):
                    self.assertGreaterEqual(opt.bonus_rate, ev.min_bonus_rate)

    def test_selected_rates_respect_event_whitelist(self):
        for unit, opt in self.plan.selected:
            for day in unit.days:
                for ev in self.schedule.events_on(day):
                    if ev.allowed_rates:
                        self.assertIn(opt.bonus_rate, ev.allowed_rates)

    def test_every_entry_required_event_is_decided(self):
        # エントリー必須イベントは「採用」か「理由つきで見送り」のどちらかに必ず現れる
        decided = set(self.plan.selected_days())
        for u, _, _ in self.plan.rejected:
            decided.update(u.days)
        for u in self.plan.blocked:
            decided.update(u.days)
        for ev in self.schedule.events:
            if ev.entry_required:
                for d in ev.dates:
                    self.assertIn(d, decided, f"{ev.name} {d} が未判断")

    def test_selected_meet_roas_floor(self):
        for _, opt in self.plan.selected:
            self.assertGreaterEqual(opt.roas, self.cfg.store.min_roas)

    def test_no_day_selected_twice(self):
        days = [d for u, _ in self.plan.selected for d in u.days]
        self.assertEqual(len(days), len(set(days)))

    def test_grand_finale_is_selected(self):
        # 日曜 x 5のつく日 x 超PayPay祭グランドフィナーレ の10/25は最優先で入るはず
        self.assertIn(date(2026, 10, 25), self.plan.selected_days())

    def test_markdown_has_all_sections(self):
        md = render_markdown(self.cfg, self.schedule, self.plan)
        for heading in ("サマリー", "締切アラート", "カレンダー", "明細",
                        "見送り判断", "損益分岐", "前提"):
            self.assertIn(heading, md)

    def test_calendar_rows_are_aligned(self):
        # 曜日見出しは全角なので、文字数ではなく端末上の表示幅で揃っている必要がある
        md = render_markdown(self.cfg, self.schedule, self.plan)
        block = md.split("```")[1].strip("\n").split("\n")
        widths = {display_width(line) for line in block}
        self.assertEqual(len(widths), 1, f"カレンダーの行幅が揃っていない: {widths}")

    def test_html_is_self_contained(self):
        html = render_html(self.cfg, self.schedule, self.plan)
        self.assertIn("<!doctype html>", html)
        self.assertNotIn("http://", html)


if __name__ == "__main__":
    unittest.main()
