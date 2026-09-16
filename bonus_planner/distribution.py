"""注文単価の分布.

販促カレンダーの「注文下限金額」と「付与上限」がどれだけ効くかは、
平均注文単価ではなく **分布の形** で決まる。

    注文下限金額  下限未満の注文には付かない   → 低単価側で効果が消える
    付与上限      1注文あたりの上限で頭打ち    → 高単価側で効果が消える

自社の注文明細があれば実測分布をそのまま使う。無ければ対数正規で近似する。

## 実測分布を優先する理由

このストアの注文単価はユニーク価格が350種しかなく、上位10価格で全体の
56%を占める。SKU価格に張り付いた離散分布で、連続分布の近似が成り立たない。
対数正規で近似すると注文下限3,000円の該当率を 48% → 59% と11ポイント過大に
見積もり、ボーナスストアPlusの価値を1割ほど水増ししてしまう。
"""

from __future__ import annotations

import bisect
import csv
import io
import math
import statistics
from collections import Counter
from pathlib import Path

INF = float("inf")

# 注文明細CSVで金額列として受け付ける列名
PRICE_COLUMNS = ("total_price", "totalprice", "price", "amount", "金額", "購入金額")


def _normalize_cap(cap_yen: float | None) -> float:
    if cap_yen is None or cap_yen <= 0:
        return INF
    return cap_yen


class OrderValueDistribution:
    """注文単価分布の共通インターフェース.

    同じ (還元率, 注文下限, 付与上限) の組がプランナーから
    日 x 還元率 x 施策の回数だけ呼ばれるため、結果をメモ化する。
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[float, float, float], float] = {}
        self._tier_cache: dict[tuple, float] = {}

    # -- 実装側が用意するもの ----------------------------------------
    @property
    def mean(self) -> float:
        raise NotImplementedError

    @property
    def source(self) -> str:
        raise NotImplementedError

    @property
    def count(self) -> int | None:
        """実測なら件数、近似なら None."""
        return None

    def qualifying_share(self, min_order_yen: float) -> float:
        """注文下限を満たす注文の割合."""
        raise NotImplementedError

    def _mass_between(self, lo: float, hi: float) -> float:
        """lo <= V < hi の注文が全GMVに占める割合."""
        raise NotImplementedError

    def _prob_between(self, lo: float, hi: float) -> float:
        """P(lo <= V < hi)."""
        raise NotImplementedError

    def _expected_rate(self, rate: float, min_order_yen: float, cap_yen: float) -> float:
        """区間 [min_order, ∞) に一律 rate を適用したときの期待付与率."""
        return self._rate_in_range(rate, min_order_yen, INF, cap_yen)

    def _rate_in_range(
        self, rate: float, lo: float, hi: float, cap_yen: float
    ) -> float:
        """区間 [lo, hi) の注文に rate を適用したときの期待付与率.

        上限が効き始める注文単価 cap/rate で区間を割り、
        手前は比例、以降は定額として足し合わせる。
        """
        if rate <= 0 or hi <= lo:
            return 0.0
        threshold = INF if cap_yen == INF else cap_yen / rate
        linear = rate * self._mass_between(lo, min(hi, threshold))
        if cap_yen == INF or threshold >= hi:
            return linear
        capped = cap_yen * self._prob_between(max(lo, threshold), hi) / self.mean
        return linear + capped

    def expected_tiered_rate(
        self, tiers: tuple[tuple[float, float], ...], cap_yen: float | None = None
    ) -> float:
        """段階付与の期待付与率.

        注文ごとにその金額で段を判定する。平均単価で段を決めて全注文に
        適用すると、下位の段にも届かない注文にまで付与率を掛けてしまう。

        買い回り型(複数注文の合計で段が決まる)は、1注文だけで判定する分
        保守的な見積りになる。
        """
        if not tiers:
            return 0.0
        key = ("tiers", tiers, _normalize_cap(cap_yen))
        cached = self._tier_cache.get(key)
        if cached is not None:
            return cached
        cap = _normalize_cap(cap_yen)
        ordered = sorted(tiers)
        total = 0.0
        for i, (lo, rate) in enumerate(ordered):
            hi = ordered[i + 1][0] if i + 1 < len(ordered) else INF
            total += self._rate_in_range(rate, lo, hi, cap)
        self._tier_cache[key] = total
        return total

    # -- 共通 ---------------------------------------------------------
    def expected_rate(
        self, rate: float, min_order_yen: float = 0.0, cap_yen: float | None = None
    ) -> float:
        """期待付与率 = E[ min(V*rate, cap) * 1{V >= min_order} ] / E[V]."""
        if rate <= 0 or self.mean <= 0:
            return 0.0
        key = (rate, max(min_order_yen, 0.0), _normalize_cap(cap_yen))
        cached = self._cache.get(key)
        if cached is None:
            cached = self._expected_rate(*key)
            self._cache[key] = cached
        return cached

    def expected_coupon_rate(
        self, coupon_yen: float, min_order_yen: float = 0.0
    ) -> float:
        """定額クーポンを実効的な率に換算する."""
        if coupon_yen <= 0 or self.mean <= 0:
            return 0.0
        return coupon_yen * self.qualifying_share(min_order_yen) / self.mean


# --------------------------------------------------------------------------
# 実測分布
# --------------------------------------------------------------------------
class EmpiricalDistribution(OrderValueDistribution):
    """注文明細から作る実測分布.

    ソート済みの金額列と累積和を持ち、bisect で区間和を O(log n) で求める。
    """

    def __init__(self, values: list[float], label: str = "注文明細") -> None:
        super().__init__()
        positive = sorted(float(v) for v in values if v > 0)
        if not positive:
            raise ValueError("注文明細に正の金額がありません")
        self._values = positive
        self._label = label
        # prefix[i] = values[0..i-1] の合計
        self._prefix: list[float] = [0.0]
        for v in positive:
            self._prefix.append(self._prefix[-1] + v)
        self._total = self._prefix[-1]
        self._mean = self._total / len(positive)

    # -- 基本統計 -----------------------------------------------------
    @property
    def mean(self) -> float:
        return self._mean

    @property
    def source(self) -> str:
        return f"{self._label}（実測 {len(self._values):,}件）"

    @property
    def count(self) -> int:
        return len(self._values)

    @property
    def values(self) -> list[float]:
        return self._values

    @property
    def total(self) -> float:
        return self._total

    def quantile(self, p: float) -> float:
        idx = min(int(p * len(self._values)), len(self._values) - 1)
        return self._values[idx]

    def log_sigma(self) -> float:
        """対数標準偏差. 対数正規で近似したときの参考値."""
        return statistics.stdev(math.log(v) for v in self._values)

    # -- 区間集計 -----------------------------------------------------
    def _index_at(self, threshold: float) -> int:
        """threshold 以上の最初の位置."""
        return bisect.bisect_left(self._values, threshold)

    def _sum_between(self, lo: float, hi: float) -> float:
        """lo <= V < hi の金額合計."""
        if hi <= lo:
            return 0.0
        a = self._index_at(lo)
        b = len(self._values) if hi == INF else self._index_at(hi)
        return self._prefix[b] - self._prefix[a]

    def _count_at_least(self, threshold: float) -> int:
        if threshold == INF:
            return 0
        return len(self._values) - self._index_at(threshold)

    def qualifying_share(self, min_order_yen: float) -> float:
        if min_order_yen <= 0:
            return 1.0
        return self._count_at_least(min_order_yen) / len(self._values)

    def _mass_between(self, lo: float, hi: float) -> float:
        return self._sum_between(lo, hi) / self._total

    def _prob_between(self, lo: float, hi: float) -> float:
        if hi <= lo:
            return 0.0
        a = self._index_at(lo)
        b = len(self._values) if hi == INF else self._index_at(hi)
        return (b - a) / len(self._values)

    # -- 注文下限の「あと一歩」分析 -----------------------------------
    def near_miss(
        self, threshold: float, floor_ratio: float = 0.8
    ) -> dict[str, float | int | list]:
        """注文下限をわずかに下回る注文をまとめる.

        まとめ買い誘導やセット設計で下限を越えられるかを判断する材料。
        """
        lo = threshold * floor_ratio
        near = [v for v in self._values if lo <= v < threshold]
        clusters = Counter(near).most_common(5)
        return {
            "threshold": threshold,
            "floor": lo,
            "count": len(near),
            "amount": sum(near),
            "qualifying_share": self.qualifying_share(threshold),
            "clusters": [
                {"price": price, "count": n, "gap": threshold - price}
                for price, n in clusters
            ],
        }

    def profile(self, thresholds: tuple[float, ...] = (3000, 5000, 20000, 25000)) -> dict:
        """明細そのものを配らなくても再現・レビューできる要約統計."""
        return {
            "count": len(self._values),
            "mean": round(self._mean, 1),
            "median": round(statistics.median(self._values), 1),
            "min": self._values[0],
            "max": self._values[-1],
            "log_sigma": round(self.log_sigma(), 4),
            "quantiles": {
                f"p{int(p * 100)}": round(self.quantile(p), 1)
                for p in (0.1, 0.25, 0.5, 0.75, 0.9, 0.99)
            },
            "qualifying_share": {
                str(int(t)): round(self.qualifying_share(t), 4) for t in thresholds
            },
            "near_miss": {
                str(int(t)): {
                    k: v for k, v in self.near_miss(t).items() if k != "clusters"
                }
                | {"clusters": self.near_miss(t)["clusters"]}
                for t in thresholds
            },
        }


# --------------------------------------------------------------------------
# 対数正規による近似(明細が無いときのフォールバック)
# --------------------------------------------------------------------------
def _phi(x: float) -> float:
    """標準正規分布の累積分布関数."""
    if x <= -40.0:
        return 0.0
    if x >= 40.0:
        return 1.0
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


class LognormalDistribution(OrderValueDistribution):
    """平均と対数標準偏差だけで近似する分布.

    注文明細が無いときのフォールバック。SKU価格に張り付いた離散分布では
    誤差が大きいため、明細が手に入ったら実測に置き換えること。
    """

    def __init__(self, aov: float, sigma: float) -> None:
        super().__init__()
        if aov <= 0:
            raise ValueError("aov は正の数である必要があります")
        self._mean = aov
        self._sigma = max(sigma, 0.0)
        self._mu = math.log(aov) - self._sigma * self._sigma / 2.0

    @property
    def mean(self) -> float:
        return self._mean

    @property
    def source(self) -> str:
        return f"対数正規による近似（平均{self._mean:,.0f}円・σ={self._sigma}）"

    def qualifying_share(self, min_order_yen: float) -> float:
        if min_order_yen <= 0:
            return 1.0
        return self._survival(min_order_yen)

    def _survival(self, x: float) -> float:
        """P(V > x)."""
        if x == INF:
            return 0.0
        if x <= 0:
            return 1.0
        if self._sigma <= 0:
            return 1.0 if self._mean > x else 0.0
        return 1.0 - _phi((math.log(x) - self._mu) / self._sigma)

    def _partial_expectation(self, lo: float, hi: float) -> float:
        """E[V ; lo <= V <= hi]."""
        if hi <= lo:
            return 0.0
        if self._sigma <= 0:
            return self._mean if lo <= self._mean <= hi else 0.0
        s = self._sigma
        z_hi = INF if hi == INF else (math.log(hi) - self._mu - s * s) / s
        z_lo = -INF if lo <= 0 else (math.log(lo) - self._mu - s * s) / s
        return self._mean * (_phi(z_hi) - _phi(z_lo))

    def _mass_between(self, lo: float, hi: float) -> float:
        return self._partial_expectation(lo, hi) / self._mean

    def _prob_between(self, lo: float, hi: float) -> float:
        if hi <= lo:
            return 0.0
        return max(self._survival(lo) - self._survival(hi), 0.0)

    def _expected_rate(self, rate: float, min_order_yen: float, cap_yen: float) -> float:
        if self._sigma <= 0:
            if self._mean < min_order_yen:
                return 0.0
            return min(rate * self._mean, cap_yen) / self._mean
        return self._rate_in_range(rate, min_order_yen, INF, cap_yen)


# --------------------------------------------------------------------------
# 読み込み
# --------------------------------------------------------------------------
def load_order_values(path: str | Path) -> list[float]:
    """注文明細CSVから金額列を読む.

    受け付ける形式:
      - `total_price` 等の金額列を持つCSV(他の列があってもよい)
      - 金額だけの1列CSV
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"注文明細が見つかりません: {p}")

    raw = p.read_bytes()
    text = None
    for enc in ("utf-8-sig", "cp932"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError(f"文字コードを判別できませんでした: {p}")

    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise ValueError(f"注文明細が空です: {p}")

    header = [c.strip() for c in rows[0]]
    lowered = [c.lower() for c in header]
    index = next((lowered.index(c) for c in PRICE_COLUMNS if c in lowered), None)
    if index is None:
        if len(header) != 1:
            raise ValueError(
                f"注文明細に金額列が見つかりません。{PRICE_COLUMNS} のいずれか、"
                f"または金額だけの1列にしてください: {header}"
            )
        index = 0
        # ヘッダー行が数値なら、それもデータとして扱う
        try:
            float(header[0])
            rows.insert(1, [header[0]])
        except ValueError:
            pass

    values: list[float] = []
    for r in rows[1:]:
        if not r or index >= len(r):
            continue
        token = r[index].strip().replace(",", "")
        if not token:
            continue
        try:
            v = float(token)
        except ValueError:
            continue
        if v > 0:
            values.append(v)
    if not values:
        raise ValueError(f"注文明細に有効な金額がありません: {p}")
    return values


def build_distribution(
    order_values_file: str | Path | None, aov: float, sigma: float
) -> OrderValueDistribution:
    """明細があれば実測分布、無ければ対数正規で近似する."""
    if order_values_file:
        path = Path(order_values_file)
        if path.exists():
            return EmpiricalDistribution(load_order_values(path), label=path.name)
    return LognormalDistribution(aov, sigma)
