import math
import random
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from bonus_planner import calendar_rules as cal
from bonus_planner.calibrate import (
    DailyRow,
    MonthlyRow,
    calibrate_daily,
    calibrate_monthly,
    estimate_aov_sigma,
    load_history,
    theil_sen_slope,
)
from bonus_planner.config import BehaviorParams

PRIOR = BehaviorParams(month={})

# 合成データの正解値
TRUE_DAILY_INDEX = 40.0
TRUE_MONTHLY_GROWTH = 1.01
TRUE_SEASON = {
    1: 0.95, 2: 0.90, 3: 1.10, 4: 1.05, 5: 0.95, 6: 0.90,
    7: 1.15, 8: 1.10, 9: 1.00, 10: 1.05, 11: 0.95, 12: 1.20,
}
TRUE_AOV = 21000.0


def _weight(year: int, month: int, days: int | None = None) -> float:
    row = MonthlyRow(year, month, 1, 1, days)
    return sum(
        PRIOR.dow[cal.weekday_key(d)] * PRIOR.dom[cal.dom_bucket(d)]
        for d in row.covered_days()
    )


def synth_monthly(n=13, start=(2025, 9), noise=0.0, seed=11, partial_last=None):
    rng = random.Random(seed)
    rows = []
    y, m = start
    for i in range(n):
        level = TRUE_DAILY_INDEX * (TRUE_MONTHLY_GROWTH ** i) * TRUE_SEASON[m]
        days = partial_last if (i == n - 1 and partial_last) else None
        orders = level * _weight(y, m, days)
        if noise:
            orders *= math.exp(rng.gauss(0, noise))
        rows.append(MonthlyRow(y, m, round(orders, 2), round(orders * TRUE_AOV), days))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return rows


class TestTheilSen(unittest.TestCase):
    def test_recovers_slope(self):
        xs = [float(i) for i in range(10)]
        ys = [3.0 + 2.0 * x for x in xs]
        self.assertAlmostEqual(theil_sen_slope(xs, ys), 2.0)

    def test_robust_to_outlier(self):
        xs = [float(i) for i in range(10)]
        ys = [3.0 + 2.0 * x for x in xs]
        ys[5] = 999.0
        self.assertAlmostEqual(theil_sen_slope(xs, ys), 2.0, delta=0.2)

    def test_empty(self):
        self.assertEqual(theil_sen_slope([], []), 0.0)


class TestMonthlyCalibration(unittest.TestCase):
    def test_recovers_seasonality_without_shrinkage(self):
        result = calibrate_monthly(synth_monthly(), PRIOR, shrinkage=1.0)
        mean_true = sum(TRUE_SEASON.values()) / 12
        for m, truth in TRUE_SEASON.items():
            got = result.params.month[f"{m:02d}"]
            self.assertAlmostEqual(got, truth / mean_true, delta=0.06, msg=f"{m}月")

    def test_shrinkage_pulls_toward_one(self):
        full = calibrate_monthly(synth_monthly(), PRIOR, shrinkage=1.0)
        half = calibrate_monthly(synth_monthly(), PRIOR, shrinkage=0.5)
        for key in full.params.month:
            self.assertLessEqual(
                abs(half.params.month[key] - 1.0),
                abs(full.params.month[key] - 1.0) + 1e-6,
                msg=key,
            )

    def test_recovers_base_orders_level(self):
        rows = synth_monthly()
        result = calibrate_monthly(rows, PRIOR, shrinkage=1.0)
        # 直近月のトレンド水準 (季節性を除いた日次水準)
        expected = TRUE_DAILY_INDEX * (TRUE_MONTHLY_GROWTH ** (len(rows) - 1))
        self.assertAlmostEqual(result.params.base_orders, expected, delta=expected * 0.12)

    def test_recovers_aov(self):
        result = calibrate_monthly(synth_monthly(), PRIOR)
        self.assertAlmostEqual(result.suggested_aov, TRUE_AOV, delta=TRUE_AOV * 0.02)

    def test_tolerates_noise(self):
        result = calibrate_monthly(synth_monthly(noise=0.08), PRIOR, shrinkage=1.0)
        mean_true = sum(TRUE_SEASON.values()) / 12
        self.assertAlmostEqual(
            result.params.month["12"], TRUE_SEASON[12] / mean_true, delta=0.18
        )

    def test_does_not_touch_weekday_or_payday_factors(self):
        """月次データでは曜日・給料日サイクルは推定できない。仮値のまま残すこと."""
        result = calibrate_monthly(synth_monthly(), PRIOR)
        self.assertEqual(result.params.dow, PRIOR.dow)
        self.assertEqual(result.params.dom, PRIOR.dom)
        self.assertEqual(result.params.share_elasticity, PRIOR.share_elasticity)
        self.assertEqual(result.params.market_elasticity, PRIOR.market_elasticity)

    def test_note_states_what_is_uncalibrated(self):
        result = calibrate_monthly(synth_monthly(), PRIOR)
        self.assertIn("未校正", result.params.calibration_note)
        self.assertIn("曜日係数", result.params.calibration_note)

    def test_partial_month_is_scaled_by_days(self):
        full = calibrate_monthly(synth_monthly(), PRIOR, shrinkage=1.0)
        partial = calibrate_monthly(synth_monthly(partial_last=14), PRIOR, shrinkage=1.0)
        self.assertAlmostEqual(
            partial.params.base_orders, full.params.base_orders,
            delta=full.params.base_orders * 0.05,
        )

    def test_warns_when_last_month_is_current_and_days_missing(self):
        rows = synth_monthly(n=13, start=(2025, 9))
        result = calibrate_monthly(rows, PRIOR, today=date(2026, 9, 15))
        self.assertTrue(any("当月" in n for n in result.notes))

    def test_warns_on_short_series(self):
        result = calibrate_monthly(synth_monthly(n=4), PRIOR)
        self.assertTrue(any("ヶ月しかありません" in n for n in result.notes))

    def test_missing_months_keep_prior(self):
        result = calibrate_monthly(synth_monthly(n=4), PRIOR)
        self.assertTrue(any("実績のない月" in n for n in result.notes))

    def test_requires_two_months(self):
        with self.assertRaises(ValueError):
            calibrate_monthly(synth_monthly(n=1), PRIOR)


class TestDailyCalibration(unittest.TestCase):
    def test_recovers_weekday_shape(self):
        true_dow = {"mon": 0.90, "tue": 0.90, "wed": 0.95, "thu": 0.95,
                    "fri": 1.00, "sat": 1.10, "sun": 1.20}
        rows, day = [], date(2025, 1, 1)
        for _ in range(400):
            rows.append(DailyRow(day, 50 * true_dow[cal.weekday_key(day)], 0))
            day += timedelta(days=1)
        params, _ = calibrate_daily(rows, BehaviorParams(
            dom={k: 1.0 for k in PRIOR.dom}, month={}))
        self.assertAlmostEqual(
            params.dow["sun"] / params.dow["mon"], 1.20 / 0.90, delta=0.05
        )

    def test_notes_rate_elasticity_uncalibrated(self):
        rows = [
            DailyRow(date(2025, 1, 1) + timedelta(days=i), 50, 1_000_000)
            for i in range(120)
        ]
        _, notes = calibrate_daily(rows, PRIOR)
        self.assertTrue(any("弾力性" in n for n in notes))


class TestLoadHistory(unittest.TestCase):
    def _write(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
        f.write(text)
        f.close()
        return Path(f.name)

    def test_detects_monthly(self):
        kind, rows = self._load("month,orders,gmv\n2025-09,1200,24000000\n2025-10,1300,26000000\n")
        self.assertEqual(kind, "monthly")
        self.assertEqual(len(rows), 2)

    def test_detects_daily(self):
        kind, rows = self._load("date,orders,gmv\n2025-09-01,40,800000\n2025-09-02,45,900000\n")
        self.assertEqual(kind, "daily")
        self.assertEqual(len(rows), 2)

    def test_parses_partial_days_column(self):
        _, rows = self._load("month,orders,gmv,days\n2026-09,600,12000000,14\n2026-08,1300,26000000,\n")
        by_month = {r.month: r for r in rows}
        self.assertEqual(by_month[9].days, 14)
        self.assertTrue(by_month[9].is_partial)
        self.assertIsNone(by_month[8].days)

    def test_rejects_days_beyond_month_length(self):
        with self.assertRaisesRegex(ValueError, "矛盾"):
            self._load("month,orders,gmv,days\n2026-09,600,12000000,31\n")

    def test_rejects_unknown_first_column(self):
        with self.assertRaisesRegex(ValueError, "1列目"):
            self._load("period,orders\n2025-09,1200\n")

    def test_rejects_missing_orders(self):
        with self.assertRaisesRegex(ValueError, "orders"):
            self._load("month,gmv\n2025-09,24000000\n")

    def _load(self, text):
        return load_history(self._write(text))


class TestAovSigma(unittest.TestCase):
    def test_estimates_log_sd(self):
        rng = random.Random(3)
        vals = [math.exp(rng.gauss(math.log(20000), 0.5)) for _ in range(3000)]
        self.assertAlmostEqual(estimate_aov_sigma(vals), 0.5, delta=0.05)

    def test_requires_enough_orders(self):
        with self.assertRaises(ValueError):
            estimate_aov_sigma([10000.0] * 10)


if __name__ == "__main__":
    unittest.main()
