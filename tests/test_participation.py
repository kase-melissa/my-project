"""ボーナスストアPlus参加履歴のテスト."""

import math
import random
import tempfile
import unittest
from datetime import date
from pathlib import Path

from bonus_planner.config import AppConfig
from bonus_planner.participation import (
    daily_rates,
    estimate_share_response,
    load_participation,
    monthly_summary,
)
from bonus_planner.schedule import load_schedule
from bonus_planner.sensitivity import gated_rate_profile, monthly_share_factor

ROOT = Path(__file__).resolve().parent.parent
HISTORY = ROOT / "data" / "bsplus_participation.csv"
SCHEDULE = ROOT / "data" / "promo_schedule" / "2026-10.yaml"
CONFIG = ROOT / "config" / "config.yaml"
BEHAVIOR = ROOT / "config" / "behavior_priors.yaml"


def write(text: str, encoding: str = "utf-8") -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding=encoding)
    f.write(text)
    f.close()
    return f.name


class TestLoading(unittest.TestCase):
    def test_iso_dates(self):
        p = load_participation(write("date,store_rate\n2026-03-03,0.05\n2026-03-04,0.05\n"))
        self.assertEqual(p[date(2026, 3, 3)], 0.05)
        self.assertEqual(len(p), 2)

    def test_slash_dates(self):
        p = load_participation(write("date,store_rate\n2026/3/3,0.05\n2026/9/11,0.04\n"))
        self.assertEqual(p[date(2026, 3, 3)], 0.05)
        self.assertEqual(p[date(2026, 9, 11)], 0.04)

    def test_comments_are_skipped(self):
        p = load_participation(write("# メモ\ndate,store_rate\n2026-03-03,0.05\n"))
        self.assertEqual(len(p), 1)

    def test_cp932(self):
        p = load_participation(
            write("# 参加履歴\ndate,store_rate\n2026-03-03,0.05\n", encoding="cp932")
        )
        self.assertEqual(len(p), 1)

    def test_rejects_missing_date_column(self):
        with self.assertRaisesRegex(ValueError, "date"):
            load_participation(write("day,rate\n2026-03-03,0.05\n"))

    def test_rejects_negative_rate(self):
        with self.assertRaisesRegex(ValueError, "負"):
            load_participation(write("date,store_rate\n2026-03-03,-0.05\n"))

    def test_rejects_bad_date(self):
        with self.assertRaisesRegex(ValueError, "日付"):
            load_participation(write("date,store_rate\n2026-99-99,0.05\n"))

    def test_real_file(self):
        p = load_participation(HISTORY)
        self.assertEqual(len(p), 49)
        self.assertEqual(sum(1 for r in p.values() if r == 0.05), 46)
        self.assertEqual(sum(1 for r in p.values() if r == 0.04), 3)


class TestMonthlySummary(unittest.TestCase):
    def setUp(self):
        self.p = load_participation(HISTORY)

    def test_month_with_participation(self):
        s = monthly_summary(self.p, 2026, 6)
        self.assertEqual(s.entry_days, 10)
        self.assertEqual(s.days_covered, 30)
        self.assertAlmostEqual(s.share, 10 / 30)
        self.assertAlmostEqual(s.average_rate, 0.05)

    def test_month_without_records_is_non_participation(self):
        s = monthly_summary(self.p, 2025, 10)
        self.assertEqual(s.entry_days, 0)
        self.assertEqual(s.share, 0.0)
        self.assertEqual(s.average_rate, 0.0)

    def test_partial_month_uses_actual_days(self):
        s = monthly_summary(self.p, 2026, 9, days=14)
        self.assertEqual(s.entry_days, 3)
        self.assertEqual(s.days_covered, 14)
        self.assertAlmostEqual(s.share, 3 / 14)
        self.assertAlmostEqual(s.average_rate, 0.04)

    def test_partial_month_excludes_later_days(self):
        s = monthly_summary(self.p, 2026, 9, days=10)
        self.assertEqual(s.entry_days, 0)

    def test_daily_rates_length_and_values(self):
        rates = daily_rates(self.p, 2026, 9, days=14)
        self.assertEqual(len(rates), 14)
        self.assertEqual(rates[10], 0.04)  # 9/11
        self.assertEqual(rates[0], 0.0)


class TestMonthlyShareFactor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = AppConfig.load(CONFIG, BEHAVIOR)
        cls.schedule = load_schedule(SCHEDULE)
        cls.profile = gated_rate_profile(cls.cfg, cls.schedule)
        cls.p = load_participation(HISTORY)

    def test_profile_has_one_value_per_day(self):
        self.assertEqual(len(self.profile), len(self.schedule.days()))
        self.assertTrue(any(g > 0 for g in self.profile))

    def test_participation_raises_the_factor(self):
        """参加した月は、参加していない月より補正係数が大きい."""
        without = monthly_share_factor(
            self.cfg, self.profile, daily_rates(self.p, 2025, 10)
        )
        with_part = monthly_share_factor(
            self.cfg, self.profile, daily_rates(self.p, 2026, 6)
        )
        self.assertGreater(with_part, without)
        self.assertGreater(without, 1.0)  # プロモパッケージ分で1を超える

    def test_no_participation_matches_gated_only(self):
        factor = monthly_share_factor(self.cfg, self.profile, [0.0] * 31)
        expected = sum(
            __import__("bonus_planner.behavior", fromlist=["DemandModel"])
            .DemandModel(self.cfg.behavior, self.cfg.store)
            .share_factor(g)
            for g in self.profile
        ) / len(self.profile)
        self.assertAlmostEqual(factor, expected, places=10)

    def test_empty_inputs_are_neutral(self):
        self.assertEqual(monthly_share_factor(self.cfg, [], [0.0]), 1.0)
        self.assertEqual(monthly_share_factor(self.cfg, self.profile, []), 1.0)


class TestShareResponseEstimate(unittest.TestCase):
    def test_recovers_a_known_effect(self):
        """参加率が時期と無関係な合成データなら推定できる."""
        rng = random.Random(5)
        series = []
        true_c = 0.40
        for i in range(24):
            p = [0.0, 0.5, 0.25, 0.75][i % 4]  # 時期と無相関
            cvr = 0.10 * math.exp(0.01 * i + true_c * p)
            series.append((f"m{i}", cvr, p, 0.05))
        est = estimate_share_response(series)
        self.assertTrue(est.identifiable, est.reason)
        self.assertAlmostEqual(est.coefficient, true_c, delta=0.05)

    def test_detects_confounding_with_time(self):
        """参加率が時期と完全相関していれば推定不能と返す."""
        series = [
            (f"m{i}", 0.08 * math.exp(0.03 * i), 0.0 if i < 6 else 0.25, 0.05)
            for i in range(13)
        ]
        est = estimate_share_response(series)
        self.assertFalse(est.identifiable)
        self.assertIn("相関", est.reason)

    def test_detects_insignificant_coefficient(self):
        rng = random.Random(9)
        series = [
            (f"m{i}", 0.10 * math.exp(rng.gauss(0, 0.15)), [0.0, 0.5, 0.25, 0.75][i % 4], 0.05)
            for i in range(24)
        ]
        est = estimate_share_response(series)
        self.assertFalse(est.identifiable)

    def test_constant_participation_is_not_identifiable(self):
        series = [(f"m{i}", 0.10, 0.25, 0.05) for i in range(13)]
        est = estimate_share_response(series)
        self.assertFalse(est.identifiable)
        self.assertIn("全月で同じ", est.reason)

    def test_requires_enough_months(self):
        with self.assertRaises(ValueError):
            estimate_share_response([("m0", 0.1, 0.0, 0.0)] * 3)

    def test_implied_share_gain_is_non_negative(self):
        series = [
            (f"m{i}", 0.10 * math.exp(-0.05 * (i % 4 == 1)), [0.0, 0.5, 0.25, 0.75][i % 4], 0.05)
            for i in range(24)
        ]
        est = estimate_share_response(series)
        if est.implied_share_gain is not None:
            self.assertGreaterEqual(est.implied_share_gain, 0.0)


class TestRealDataIsNotIdentifiable(unittest.TestCase):
    """実データでは参加開始が成長トレンドと重なり、分離できない."""

    @classmethod
    def setUpClass(cls):
        import calendar as _cal

        from bonus_planner.calibrate import load_history

        cls.p = load_participation(HISTORY)
        _kind, rows = load_history(
            ROOT / "data" / "sales_history_monthly.csv",
            today=date(2026, 9, 16), partial_days=14,
        )
        cls.series = []
        for r in rows:
            s = monthly_summary(cls.p, r.year, r.month, r.days)
            cls.series.append((r.key, r.cvr or 0.0, s.share, s.average_rate))
        cls.est = estimate_share_response(cls.series)

    def test_not_identifiable(self):
        self.assertFalse(self.est.identifiable)

    def test_confounding_is_high(self):
        self.assertGreater(self.est.correlation_with_time, 0.8)

    def test_coefficient_is_insignificant(self):
        self.assertLess(abs(self.est.t_value), 1.0)

    def test_trend_alone_explains_most_of_the_gap(self):
        """参加率を入れてもトレンドがほとんど変わらない."""
        self.assertAlmostEqual(
            self.est.trend_per_month, self.est.trend_without_control, delta=0.01
        )

    def test_implied_share_gain_is_below_the_default(self):
        """点推定は既定値0.20より低い。ただし有意ではない."""
        self.assertIsNotNone(self.est.implied_share_gain)
        self.assertLess(self.est.implied_share_gain, 0.20)


if __name__ == "__main__":
    unittest.main()
