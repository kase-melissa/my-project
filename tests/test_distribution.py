"""注文単価分布のテスト."""

import json
import math
import tempfile
import unittest
from pathlib import Path

from bonus_planner.distribution import (
    EmpiricalDistribution,
    LognormalDistribution,
    build_distribution,
    load_order_values,
)

ROOT = Path(__file__).resolve().parent.parent
ORDERS = ROOT / "data" / "order_values.csv"


def brute_rate(values, rate, min_order=0.0, cap=None):
    """総当たりで期待付与率を出す（分布実装の答え合わせ用）."""
    c = cap if cap else float("inf")
    return sum(min(v * rate, c) for v in values if v >= min_order) / sum(values)


def brute_tiered(values, tiers, cap=None):
    c = cap if cap else float("inf")

    def rate_of(v):
        applicable = [r for t, r in tiers if v >= t]
        return max(applicable) if applicable else 0.0

    return sum(min(v * rate_of(v), c) for v in values) / sum(values)


SAMPLE = [1000.0, 1780.0, 2980.0, 3000.0, 4970.0, 5980.0, 12000.0, 19300.0, 25000.0, 60000.0]


class TestEmpirical(unittest.TestCase):
    def setUp(self):
        self.d = EmpiricalDistribution(SAMPLE)

    def test_mean_and_count(self):
        self.assertAlmostEqual(self.d.mean, sum(SAMPLE) / len(SAMPLE))
        self.assertEqual(self.d.count, len(SAMPLE))

    def test_no_limits_gives_nominal_rate(self):
        self.assertAlmostEqual(self.d.expected_rate(0.04), 0.04, places=12)

    def test_matches_brute_force(self):
        for rate, mn, cap in (
            (0.02, 3000, 5000), (0.05, 5000, 3000), (0.04, 0, 2000),
            (0.07, 20000, 3500), (0.03, 3000, None), (0.10, 0, 100),
        ):
            self.assertAlmostEqual(
                self.d.expected_rate(rate, mn, cap),
                brute_rate(SAMPLE, rate, mn, cap),
                places=12, msg=f"{rate}/{mn}/{cap}",
            )

    def test_cap_binding_on_every_order(self):
        """上限が全注文に効くとき、実効率は cap / 平均単価になる."""
        self.assertAlmostEqual(
            self.d.expected_rate(1.0, 0, 100), 100 / self.d.mean, places=12
        )

    def test_minimum_above_all_orders_gives_zero(self):
        self.assertEqual(self.d.expected_rate(0.05, 10_000_000), 0.0)

    def test_qualifying_share(self):
        self.assertAlmostEqual(self.d.qualifying_share(3000), 7 / 10)
        self.assertAlmostEqual(self.d.qualifying_share(0), 1.0)
        self.assertAlmostEqual(self.d.qualifying_share(10_000_000), 0.0)

    def test_coupon_rate(self):
        share = self.d.qualifying_share(5000)
        self.assertAlmostEqual(
            self.d.expected_coupon_rate(1000, 5000), 1000 * share / self.d.mean
        )

    def test_tiered_matches_brute_force(self):
        tiers = ((5000.0, 0.04), (20000.0, 0.07))
        for cap in (None, 3500, 200):
            self.assertAlmostEqual(
                self.d.expected_tiered_rate(tiers, cap),
                brute_tiered(SAMPLE, tiers, cap),
                places=12, msg=f"cap={cap}",
            )

    def test_tiered_excludes_orders_below_lowest_tier(self):
        """最下段に届かない注文には付かない（平均単価で段を決める誤りの回帰）."""
        tiers = ((5000.0, 0.04),)
        self.assertLess(
            self.d.expected_tiered_rate(tiers), self.d.expected_rate(0.04)
        )

    def test_memoization_does_not_change_results(self):
        first = self.d.expected_rate(0.02, 3000, 5000)
        second = self.d.expected_rate(0.02, 3000, 5000)
        self.assertEqual(first, second)
        self.assertAlmostEqual(first, brute_rate(SAMPLE, 0.02, 3000, 5000), places=12)

    def test_near_miss(self):
        nm = self.d.near_miss(3000)
        self.assertEqual(nm["count"], 1)  # 2,400〜3,000円に入るのは 2,980円 のみ
        self.assertEqual(nm["clusters"][0]["price"], 2980.0)
        self.assertAlmostEqual(nm["clusters"][0]["gap"], 20.0)

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            EmpiricalDistribution([])

    def test_profile_is_json_serializable(self):
        payload = json.dumps(self.d.profile(), ensure_ascii=False)
        self.assertIn("quantiles", payload)


class TestLognormal(unittest.TestCase):
    def test_no_limits_gives_nominal_rate(self):
        d = LognormalDistribution(20000, 0.55)
        self.assertAlmostEqual(d.expected_rate(0.04), 0.04, places=6)

    def test_cap_reduces_rate(self):
        d = LognormalDistribution(20000, 0.55)
        self.assertLess(d.expected_rate(0.04, 0, 500), 0.04)

    def test_minimum_reduces_rate(self):
        d = LognormalDistribution(20000, 0.55)
        self.assertLess(d.expected_rate(0.04, 100000), 0.01)

    def test_zero_sigma_is_deterministic(self):
        d = LognormalDistribution(200000, 0.0)
        self.assertAlmostEqual(d.expected_rate(0.05, 0, 5000), 0.025, places=6)

    def test_tiered(self):
        d = LognormalDistribution(20000, 0.55)
        tiers = ((5000.0, 0.04), (20000.0, 0.07))
        value = d.expected_tiered_rate(tiers)
        self.assertGreater(value, 0.04)
        self.assertLess(value, 0.07)


class TestLoading(unittest.TestCase):
    def _write(self, text, encoding="utf-8"):
        f = tempfile.NamedTemporaryFile(
            "w", suffix=".csv", delete=False, encoding=encoding
        )
        f.write(text)
        f.close()
        return f.name

    def test_named_column(self):
        vals = load_order_values(self._write("id,total_price\nA,1000\nB,2000\n"))
        self.assertEqual(vals, [1000.0, 2000.0])

    def test_totalprice_column_case_insensitive(self):
        vals = load_order_values(self._write("OrderId,Id,TotalPrice\n1,a,2680\n2,b,1780\n"))
        self.assertEqual(sorted(vals), [1780.0, 2680.0])

    def test_single_column_without_header(self):
        vals = load_order_values(self._write("1000\n2000\n3000\n"))
        self.assertEqual(sorted(vals), [1000.0, 2000.0, 3000.0])

    def test_cp932_is_accepted(self):
        vals = load_order_values(self._write("金額\n1500\n2500\n", encoding="cp932"))
        self.assertEqual(sorted(vals), [1500.0, 2500.0])

    def test_thousands_separator(self):
        vals = load_order_values(self._write('total_price\n"1,780"\n"19,300"\n'))
        self.assertEqual(sorted(vals), [1780.0, 19300.0])

    def test_skips_non_positive_and_garbage(self):
        vals = load_order_values(self._write("total_price\n1000\n0\n-5\nabc\n\n2000\n"))
        self.assertEqual(sorted(vals), [1000.0, 2000.0])

    def test_rejects_missing_price_column(self):
        with self.assertRaisesRegex(ValueError, "金額列"):
            load_order_values(self._write("a,b\n1,2\n"))

    def test_rejects_file_with_no_values(self):
        with self.assertRaises(ValueError):
            load_order_values(self._write("total_price\n0\n-1\n"))


class TestBuildDistribution(unittest.TestCase):
    def test_uses_empirical_when_file_exists(self):
        d = build_distribution(ORDERS, 5529, 0.55)
        self.assertIsInstance(d, EmpiricalDistribution)
        self.assertGreater(d.count, 7000)

    def test_falls_back_to_lognormal_without_file(self):
        d = build_distribution(None, 5529, 0.55)
        self.assertIsInstance(d, LognormalDistribution)
        self.assertAlmostEqual(d.mean, 5529)

    def test_falls_back_when_file_is_missing(self):
        d = build_distribution("does-not-exist.csv", 5529, 0.55)
        self.assertIsInstance(d, LognormalDistribution)


class TestRealOrderData(unittest.TestCase):
    """実データでの回帰テスト.

    対数正規の近似が成り立たないことを数値で固定する。
    """

    @classmethod
    def setUpClass(cls):
        cls.d = EmpiricalDistribution(load_order_values(ORDERS))

    def test_distribution_is_discrete_not_lognormal(self):
        """SKU価格に張り付いており、上位10価格で半分以上を占める."""
        from collections import Counter

        top10 = sum(n for _v, n in Counter(self.d.values).most_common(10))
        self.assertGreater(top10 / self.d.count, 0.5)

    def test_qualifying_share_at_3000(self):
        self.assertAlmostEqual(self.d.qualifying_share(3000), 0.484, delta=0.005)

    def test_lognormal_overstates_the_3000_threshold(self):
        """仮値σ=0.55の対数正規は該当率を3割ほど過大に見積もる."""
        ln = LognormalDistribution(5529, 0.55)
        self.assertGreater(ln.qualifying_share(3000) - self.d.qualifying_share(3000), 0.25)

    def test_bonus_store_plus_effective_rate(self):
        self.assertAlmostEqual(self.d.expected_rate(0.02, 3000, 5000), 0.0162, delta=0.0005)

    def test_bakugai_week_top_tier_is_unreachable(self):
        self.assertLess(self.d.expected_rate(0.07, 20000, 3500), 0.005)

    def test_mall_coupon_is_effectively_dead(self):
        self.assertLess(self.d.expected_coupon_rate(1000, 25000), 0.002)

    def test_per_order_cap_is_not_binding(self):
        self.assertAlmostEqual(
            self.d.expected_rate(0.05, 0, 5000), self.d.expected_rate(0.05), places=6
        )

    def test_log_sigma_is_much_wider_than_the_old_assumption(self):
        self.assertAlmostEqual(self.d.log_sigma(), 0.815, delta=0.01)

    def test_near_miss_clusters_just_below_20000(self):
        nm = self.d.near_miss(20000)
        prices = [c["price"] for c in nm["clusters"]]
        self.assertIn(19300.0, prices)
        self.assertGreater(nm["count"], 400)


if __name__ == "__main__":
    unittest.main()
