import math
import random
import unittest
from datetime import date, timedelta

from bonus_planner import calendar_rules as cal
from bonus_planner.calibrate import (
    HistoryRow,
    calibrate,
    estimate_aov_sigma,
    event_factors,
)
from bonus_planner.config import BehaviorParams

TRUE_BASE = 50.0
TRUE_DOW = {"mon": 0.90, "tue": 0.90, "wed": 0.95, "thu": 0.95,
            "fri": 1.00, "sat": 1.10, "sun": 1.20}
TRUE_FIVE_TRAFFIC = 1.50
TRUE_FIVE_UPLIFT = 0.40


def synth(days=540, noise=0.0, seed=7):
    rng = random.Random(seed)
    rows, day = [], date(2025, 1, 1)
    for _ in range(days):
        expected = TRUE_BASE * TRUE_DOW[cal.weekday_key(day)]
        entered = False
        rate = 0.0
        if cal.is_five_day(day):
            expected *= TRUE_FIVE_TRAFFIC
            entered = day.day == 15  # 15日だけエントリーし、5日/25日は未エントリー
            if entered:
                rate = 0.04
                expected *= 1.0 + TRUE_FIVE_UPLIFT
        if noise:
            expected *= math.exp(rng.gauss(0, noise))
        rows.append(HistoryRow(day, round(expected, 2), expected * 20000, entered, rate, []))
        day += timedelta(days=1)
    return rows


class TestCalibrate(unittest.TestCase):
    def test_recovers_factors_without_noise(self):
        prior = BehaviorParams(dom={k: 1.0 for k in BehaviorParams().dom}, month={})
        params, _ = calibrate(synth(), prior)
        self.assertAlmostEqual(params.base_orders, TRUE_BASE, delta=1.5)
        self.assertAlmostEqual(params.dow["sun"] / params.dow["mon"], 1.20 / 0.90, delta=0.05)
        self.assertAlmostEqual(params.five_day_traffic, TRUE_FIVE_TRAFFIC, delta=0.08)
        self.assertAlmostEqual(params.five_day_uplift, TRUE_FIVE_UPLIFT, delta=0.08)

    def test_tolerates_noise(self):
        prior = BehaviorParams(dom={k: 1.0 for k in BehaviorParams().dom}, month={})
        params, _ = calibrate(synth(noise=0.15), prior)
        self.assertAlmostEqual(params.five_day_traffic, TRUE_FIVE_TRAFFIC, delta=0.15)

    def test_five_day_excluded_from_plain_days(self):
        # 5のつく日を平常日に混ぜると曜日係数が上振れする。除外されていることを確認。
        prior = BehaviorParams(dom={k: 1.0 for k in BehaviorParams().dom}, month={})
        params, _ = calibrate(synth(), prior)
        mean_dow = sum(params.dow.values()) / len(params.dow)
        self.assertAlmostEqual(mean_dow, 1.0, delta=0.01)

    def test_dow_normalized_to_mean_one(self):
        prior = BehaviorParams(month={})
        params, _ = calibrate(synth(noise=0.1), prior)
        self.assertAlmostEqual(sum(params.dow.values()) / 7, 1.0, delta=0.01)

    def test_note_records_period_and_sample_size(self):
        params, _ = calibrate(synth(days=90), BehaviorParams(month={}))
        self.assertIn("2025-01-01", params.calibration_note)
        self.assertIn("校正", params.calibration_note)

    def test_insufficient_samples_keeps_prior(self):
        prior = BehaviorParams(five_day_traffic=1.99, month={})
        rows = [
            HistoryRow(date(2025, 1, 1) + timedelta(days=i), 50, 1_000_000, False, 0.0, [])
            for i in range(20)
        ]
        params, notes = calibrate(rows, prior)
        self.assertEqual(params.five_day_traffic, 1.99)
        self.assertTrue(any("5のつく日" in n for n in notes))

    def test_empty_history_rejected(self):
        with self.assertRaises(Exception):
            calibrate([], BehaviorParams())


class TestEventFactors(unittest.TestCase):
    def test_returns_none_when_samples_insufficient(self):
        # サンプル不足を 0.0 で返すと「効果なし」と誤読される
        rows = [
            HistoryRow(date(2025, 1, 1) + timedelta(days=i), 50, 1_000_000,
                       True, 0.04, ["超PayPay祭"])
            for i in range(5)
        ]
        f = event_factors(rows, BehaviorParams())["超PayPay祭"]
        self.assertIsNone(f["traffic_multiplier"])
        self.assertIsNone(f["entry_uplift"])
        self.assertEqual(f["samples"], 5)


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
