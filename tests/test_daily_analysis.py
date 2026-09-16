"""日別実績からの係数推定の回帰テスト.

`tools/analyze_daily.py` は config/behavior_priors.yaml に入っている
係数の出どころなので、実データで再現できることを固定しておく。
数値がずれたら、priors 側も合わせて更新すること。
"""

import math
import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from bonus_planner.config import BehaviorParams, load_yaml  # noqa: E402
from bonus_planner.participation import load_participation  # noqa: E402

DAILY = ROOT / "data" / "sales_history_daily.csv"
PARTICIPATION = ROOT / "data" / "bsplus_participation.csv"
PRIORS = ROOT / "config" / "behavior_priors.yaml"

analyze_daily = __import__("analyze_daily")

# 全ストア共通の付与率: 定常施策のみ 7% → 5のつく日 11%
LN_RATIO = math.log(0.11 / 0.07)


@unittest.skipUnless(DAILY.exists(), "日別実績が無い")
class TestDailyExport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.days = analyze_daily.load_daily(DAILY)
        cls.part = load_participation(PARTICIPATION)

    def test_reads_cp932_export(self):
        self.assertEqual(len(self.days), 92)
        self.assertEqual(self.days[0].day, date(2026, 6, 16))
        self.assertEqual(self.days[-1].day, date(2026, 9, 15))

    def test_days_are_consecutive(self):
        for a, b in zip(self.days, self.days[1:]):
            self.assertEqual((b.day - a.day).days, 1, f"{a.day} の次が {b.day}")

    def test_has_sessions_and_orders(self):
        for r in self.days:
            self.assertGreater(r.sessions, 0)
            self.assertGreater(r.orders, 0)
            self.assertLess(r.cvr, 1.0)

    def test_participation_days_are_a_minority(self):
        """参加日と非参加日の両方がないと比較にならない."""
        n = sum(1 for r in self.days if self.part.get(r.day, 0) > 0)
        self.assertEqual(n, 25)
        self.assertEqual(len(self.days) - n, 67)

    def test_five_days_never_overlap_participation(self):
        """5のつく日の効果が参加効果と混ざらないこと.

        この2つが重なっていたら intent_elasticity は識別できない。
        """
        for r in self.days:
            if r.day.day in (5, 15, 25):
                self.assertEqual(self.part.get(r.day, 0.0), 0.0, r.day)


@unittest.skipUnless(DAILY.exists(), "日別実績が無い")
class TestEstimates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        days = analyze_daily.load_daily(DAILY)
        cls.part = part = load_participation(PARTICIPATION)
        cls.days = days
        cls.ses = analyze_daily.fit(days, part, lambda r: r.sessions)
        cls.cvr = analyze_daily.fit(days, part, lambda r: r.cvr)
        cls.ordr = analyze_daily.fit(days, part, lambda r: r.orders)
        cls.prior = BehaviorParams()

    def test_orders_coefficients_are_the_sum_of_the_two_channels(self):
        """注文数 = セッション × 転換率 が回帰係数のレベルでも成り立つ."""
        for name in ("5のつく日", "参加率", "曜日:sun"):
            self.assertAlmostEqual(
                self.ordr[name][0], self.ses[name][0] + self.cvr[name][0], places=6
            )

    def test_market_elasticity_in_range(self):
        b, _se, t = self.ses["5のつく日"]
        self.assertGreater(t, 2.0)
        self.assertTrue(0.8 <= b / LN_RATIO <= 1.4, b / LN_RATIO)

    def test_intent_elasticity_exceeds_one(self):
        """全ストア共通の付与率は転換率も動かす(モデルに必要な経路)."""
        b, _se, t = self.cvr["5のつく日"]
        self.assertGreater(t, 2.0)
        self.assertGreater(b / LN_RATIO, 1.0)

    def test_share_response_is_identifiable(self):
        """月次では解けなかった参加効果が、日別では t>2 で出ること."""
        _b, _se, t = self.cvr["参加率"]
        self.assertGreater(t, 2.0)

    def test_share_gain_estimate_brackets_the_default(self):
        """既定の share_gain_at_reference が推定レンジに入っていること.

        参加日にモール負担の+2%が開いていたかは記録に残っていないため、
        優位を「自社分のみ」と「+2%込み」の両方で換算し、幅として扱う。
        """
        b, _se, _t = self.cvr["参加率"]
        rates = [v for v in self.part.values() if v > 0]
        avg = sum(rates) / len(rates)
        lift = math.exp(b * avg) - 1
        bounds = sorted(
            lift / ((avg + extra) / self.prior.reference_advantage)
            ** self.prior.share_elasticity
            for extra in (0.0, 0.02)
        )
        self.assertLessEqual(bounds[0], 0.29)
        self.assertGreaterEqual(bounds[1], 0.17)

    def test_sunday_is_far_stronger_than_the_original_prior(self):
        dow = analyze_daily._factors(self.ordr, "曜日", analyze_daily.DOW)
        self.assertGreater(dow["sun"], 1.5)
        self.assertGreater(dow["sun"], self.prior.dow["sun"] * 1.2)

    def test_five_day_control_matters_for_the_payday_buckets(self):
        """5のつく日を制御しないと、5/15/25を含むビンが押し上げられる."""
        from bonus_planner.participation import _ols

        days, part = self.days, self.part
        controlled = analyze_daily._factors(self.ordr, "月内", analyze_daily.BUCKETS)

        # 5のつく日の列を落として同じ回帰を回す
        X, names = analyze_daily.design(days, part)
        drop = names.index("5のつく日")
        reduced = [row[:drop] + row[drop + 1:] for row in X]
        y = [math.log(r.orders) for r in days]
        beta, se = _ols(reduced, y)
        loose = {n: (b, s, b / s if s else 0.0)
                 for n, b, s in zip(names[:drop] + names[drop + 1:], beta, se)}
        uncontrolled = analyze_daily._factors(loose, "月内", analyze_daily.BUCKETS)

        # 5・15・25 を含むビンはすべて、制御しないほうが高く出る
        for bucket in ("d01_05", "d11_15", "d25_end"):
            self.assertGreater(
                uncontrolled[bucket], controlled[bucket],
                f"{bucket}: 制御なし {uncontrolled[bucket]:.3f} "
                f"vs 制御あり {controlled[bucket]:.3f}",
            )


@unittest.skipUnless(PRIORS.exists(), "priors が無い")
class TestPriorsMatchEstimates(unittest.TestCase):
    """behavior_priors.yaml が推定結果と整合していること."""

    @classmethod
    def setUpClass(cls):
        cls.p = BehaviorParams.from_dict(load_yaml(PRIORS)["behavior"])

    def test_daily_calibration_is_recorded(self):
        self.assertIn("日別実績", self.p.calibration_note)

    def test_sunday_factor_is_the_shrunk_estimate(self):
        self.assertAlmostEqual(self.p.dow["sun"], 1.599, places=3)

    def test_intent_channel_is_on(self):
        self.assertGreater(self.p.intent_elasticity, 1.0)

    def test_share_gain_is_inside_the_measured_range(self):
        self.assertTrue(0.17 <= self.p.share_gain_at_reference <= 0.29)
