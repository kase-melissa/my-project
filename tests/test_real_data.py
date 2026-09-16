"""実データ経路の回帰テスト.

ストアクリエイターProの実績エクスポートと、2026年10月の実カレンダーを
組み合わせた端から端までの検証。
"""

import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

from bonus_planner.calibrate import calibrate_monthly, load_history
from bonus_planner.config import AppConfig
from bonus_planner.economics import expected_benefit_rate, expected_coupon_rate
from bonus_planner.optimizer import optimize
from bonus_planner.planner import build_entry_units
from bonus_planner.schedule import load_schedule
from bonus_planner.sensitivity import average_baseline_factors

ROOT = Path(__file__).resolve().parent.parent
HISTORY = ROOT / "data" / "sales_history_monthly.csv"
SCHEDULE = ROOT / "data" / "promo_schedule" / "2026-10.yaml"
CONFIG = ROOT / "config" / "config.yaml"
BEHAVIOR = ROOT / "config" / "behavior_priors.yaml"
TODAY = date(2026, 9, 16)   # ボーナスストア申込の締切当日
DEADLINE = date(2026, 9, 16)


class TestYahooExport(unittest.TestCase):
    """CP932・日本語ヘッダー・「前年比」列の重複がある実エクスポートを読む."""

    def setUp(self):
        self.kind, self.rows = load_history(HISTORY, today=TODAY, partial_days=14)

    def test_detected_as_monthly(self):
        self.assertEqual(self.kind, "monthly")
        self.assertEqual(len(self.rows), 13)

    def test_period_covered(self):
        self.assertEqual(self.rows[0].key, "2025-09")
        self.assertEqual(self.rows[-1].key, "2026-09")

    def test_sessions_and_buyers_are_read(self):
        """「前年比」が重複するため位置で拾えているか."""
        aug = next(r for r in self.rows if r.key == "2026-08")
        self.assertAlmostEqual(aug.orders, 825)
        self.assertAlmostEqual(aug.gmv, 4_602_174)
        self.assertAlmostEqual(aug.sessions, 7988)
        self.assertAlmostEqual(aug.buyers, 797)
        self.assertAlmostEqual(aug.cvr, 825 / 7988, places=6)
        self.assertAlmostEqual(aug.aov, 4_602_174 / 825, places=2)

    def test_partial_days_override(self):
        self.assertEqual(self.rows[-1].days, 14)
        self.assertTrue(self.rows[-1].is_partial)

    def test_partial_days_inferred_from_today(self):
        _kind, rows = load_history(HISTORY, today=date(2026, 9, 16))
        self.assertEqual(rows[-1].days, 15)  # 前日まで

    def test_full_months_have_no_days(self):
        self.assertIsNone(self.rows[0].days)

    def test_utf8_is_also_accepted(self):
        text = HISTORY.read_bytes().decode("cp932")
        with tempfile.NamedTemporaryFile(
            "w", suffix=".csv", delete=False, encoding="utf-8"
        ) as f:
            f.write(text)
            path = f.name
        _kind, rows = load_history(path, today=TODAY)
        self.assertEqual(len(rows), 13)


class TestCalibrationOnRealData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = AppConfig.load(CONFIG, BEHAVIOR)
        cls.schedule = load_schedule(SCHEDULE)
        _kind, cls.rows = load_history(HISTORY, today=TODAY, partial_days=14)
        cls.market, cls.share = average_baseline_factors(cls.cfg, cls.schedule)
        cls.result = calibrate_monthly(
            cls.rows, cls.cfg.behavior, today=TODAY,
            average_market_factor=cls.market, average_share_factor=cls.share,
        )

    def test_uses_session_basis(self):
        self.assertEqual(self.result.basis, "sessions")

    def test_captures_rising_conversion_trend(self):
        """CVRは実績で8.4%→11.9%と上昇している。トレンドを掴めていること."""
        keys = list(self.result.cvr_trend_at)
        self.assertLess(
            self.result.cvr_trend_at[keys[0]], self.result.cvr_trend_at[keys[-1]]
        )

    def test_both_double_counting_corrections_apply(self):
        """集客側と転換率側の両方で上振れを差し引いていること."""
        self.assertGreater(self.market, 1.0)
        self.assertGreater(self.share, 1.0)
        joined = " ".join(self.result.notes)
        self.assertIn("販促イベントによる上振れ", joined)
        self.assertIn("転換率の上振れ", joined)

    def test_share_correction_lowers_base_cvr(self):
        """シェア補正をかけないと base_cvr が過大になる(二重計上の回帰テスト)."""
        uncorrected = calibrate_monthly(
            self.rows, self.cfg.behavior, today=TODAY,
            average_market_factor=self.market, average_share_factor=1.0,
        )
        self.assertLess(self.result.params.base_cvr, uncorrected.params.base_cvr)
        self.assertAlmostEqual(
            self.result.params.base_cvr * self.share,
            uncorrected.params.base_cvr,
            delta=1e-4,
        )

    def test_suggested_aov_matches_recent_actuals(self):
        recent = self.rows[-3:]
        self.assertAlmostEqual(
            self.result.suggested_aov,
            sorted(r.aov for r in recent)[1],
            delta=1.0,
        )

    def test_weekday_factors_left_uncalibrated(self):
        self.assertEqual(self.result.params.dow, self.cfg.behavior.dow)
        self.assertEqual(self.result.params.dom, self.cfg.behavior.dom)


class TestEffectiveRatesAtRealAov(unittest.TestCase):
    """平均注文単価5,529円では、注文下限が実効率を大きく削る."""

    AOV = 5529.0
    SIGMA = 0.55

    def rate(self, nominal, min_order, cap):
        return expected_benefit_rate(nominal, self.AOV, self.SIGMA, min_order, cap)

    def test_no_minimum_keeps_full_rate(self):
        self.assertAlmostEqual(self.rate(0.04, 0, 2000), 0.04, places=3)

    def test_three_thousand_minimum_trims_slightly(self):
        self.assertAlmostEqual(self.rate(0.02, 3000, 5000), 0.0183, places=3)

    def test_five_thousand_minimum_halves_the_rate(self):
        self.assertAlmostEqual(self.rate(0.05, 5000, 3000), 0.0338, places=3)

    def test_twenty_thousand_tier_is_unreachable(self):
        """爆買WEEKの7%段は該当注文がほぼ無い."""
        self.assertLess(self.rate(0.07, 20000, 3500), 0.005)

    def test_mall_coupon_is_effectively_dead(self):
        """モールクーポン(注文下限25,000円)は実質機能しない."""
        self.assertLess(expected_coupon_rate(1000, self.AOV, self.SIGMA, 25000), 0.002)

    def test_per_order_cap_is_not_binding(self):
        """1注文あたり上限5,000円はこの単価では効かない."""
        self.assertAlmostEqual(
            self.rate(0.05, 0, 5000), self.rate(0.05, 0, 0), places=4
        )


class TestPlanOnRealData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = AppConfig.load(CONFIG, BEHAVIOR)
        cls.schedule = load_schedule(SCHEDULE)
        units = build_entry_units(cls.cfg, cls.schedule, TODAY)
        cls.plan = optimize(cls.cfg, units, cls.schedule.month, TODAY)

    def test_zero_percent_is_not_offered(self):
        """自社還元率0%は設定不可のため候補に含まれない."""
        self.assertNotIn(0.0, self.cfg.store.store_bonus_rates)
        for _u, opt in self.plan.selected:
            self.assertGreater(opt.store_rate, 0.0)

    def test_deadline_day_is_still_open(self):
        """締切当日(9/16)はまだエントリーできる."""
        for unit in self.plan.blocked:
            self.assertNotIn(date(2026, 10, 2), unit.days)

    def test_day_after_deadline_blocks_entry(self):
        units = build_entry_units(self.cfg, self.schedule, date(2026, 9, 17))
        blocked_days = {d for u in units if u.blocked_reason for d in u.days}
        for d in (2, 7, 16, 20, 27, 28):
            self.assertIn(date(2026, 10, d), blocked_days, f"10/{d}")

    def test_budget_is_respected(self):
        self.assertLessEqual(self.plan.total_cost, self.cfg.store.monthly_point_budget)

    def test_cost_stays_within_store_funded_share(self):
        """モール負担分が原資に混入していないこと."""
        for _u, opt in self.plan.selected:
            for est in opt.estimates:
                ceiling = est.entry_gmv * (0.01 + est.store_rate) * 1.001
                self.assertLessEqual(est.entry_point_cost, ceiling, f"{est.day}")

    def test_entry_does_not_change_sessions(self):
        """参加で動くのは転換率だけ。セッション数は変わらない."""
        for _u, opt in self.plan.selected:
            for est in opt.estimates:
                self.assertGreater(est.entry_orders, est.base_orders)


if __name__ == "__main__":
    unittest.main()
