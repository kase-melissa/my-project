"""ポイント原資と付与率の計算.

注文下限金額と付与上限がどれだけ効くかは、注文単価の **分布** で決まる。
分布そのものは `distribution.py` が持ち、ここはそれを使う側に徹する。

犬猫用家電のような商材では、この2つが逆方向に効く。

    注文下限金額  下限未満の注文には付かない   → 低単価側で効果が消える
    付与上限      1注文あたりの上限で頭打ち    → 高単価側で効果が消える

平均単価だけで判断するとどちらも見落とす。
"""

from __future__ import annotations

from .config import StoreConfig
from .distribution import LognormalDistribution, OrderValueDistribution
from .models import ELIGIBILITY_ALL, Benefit, Participation

INF = float("inf")


def expected_benefit_rate(
    rate: float,
    aov: float,
    sigma: float,
    min_order_yen: float = 0.0,
    cap_yen: float | None = None,
) -> float:
    """対数正規近似での期待付与率.

    注文明細が無いときのフォールバック。実測分布がある場合は
    `StoreConfig.distribution` 経由で `OrderValueDistribution.expected_rate` を使う。

    >>> round(expected_benefit_rate(0.04, 20000, 0.55), 4)
    0.04
    """
    if rate <= 0 or aov <= 0:
        return 0.0
    return LognormalDistribution(aov, sigma).expected_rate(rate, min_order_yen, cap_yen)


def expected_coupon_rate(
    coupon_yen: float, aov: float, sigma: float, min_order_yen: float = 0.0
) -> float:
    """対数正規近似で、定額クーポンを実効的な率に換算する."""
    if coupon_yen <= 0 or aov <= 0:
        return 0.0
    return LognormalDistribution(aov, sigma).expected_coupon_rate(
        coupon_yen, min_order_yen
    )


def benefit_rate(benefit: Benefit, store: StoreConfig) -> float:
    """1施策の期待付与率(自社の注文単価分布のもとで)."""
    dist: OrderValueDistribution = store.distribution
    if benefit.coupon_yen > 0:
        return dist.expected_coupon_rate(benefit.coupon_yen, benefit.min_order_yen)
    if benefit.tiers:
        # 段階付与は注文ごとに段を判定する。平均単価で段を決めて全注文に
        # 適用すると、下位の段にも届かない注文にまで付与率を掛けてしまう。
        return dist.expected_tiered_rate(benefit.tiers, benefit.user_cap_yen)
    return dist.expected_rate(benefit.rate, benefit.min_order_yen, benefit.user_cap_yen)


def perceived_total_rate(
    benefits: list[Benefit], part: Participation, store: StoreConfig
) -> float:
    """その日に顧客が実際に受け取る総付与率(需要のドライバー).

    注文下限・付与上限を織り込むため、カレンダー表示の「最大付与率」より低くなる。
    """
    return sum(benefit_rate(b, store) for b in benefits if b.is_active(part))


def rate_by_scope(
    benefits: list[Benefit], part: Participation, store: StoreConfig
) -> tuple[float, float]:
    """その日の付与率を「全ストア共通」と「自社だけの上乗せ」に分ける.

    この分離が需要モデルの土台になる。

    全ストア共通(mall_wide)
        5のつく日・ファーストデイ・定常施策など。競合も同じ条件なので
        自社のシェアは動かない。動くのはモール全体の来訪者数(市場規模)。
    自社だけの上乗せ(gated)
        プロモーションパッケージ加入やボーナスストアPlus参加で初めて開く分。
        持っていない競合に対する優位になるので、シェアを動かす。

    戻り値: (全ストア共通の率, 自社だけの上乗せ率)
    """
    mall_wide = 0.0
    gated = 0.0
    for b in benefits:
        if not b.is_active(part):
            continue
        rate = benefit_rate(b, store)
        if b.eligibility == ELIGIBILITY_ALL:
            mall_wide += rate
        else:
            gated += rate
    return mall_wide, gated


def nominal_total_rate(
    benefits: list[Benefit], part: Participation, basket_yen: float
) -> float:
    """カレンダー表示ベースの総付与率(注文下限・付与上限を無視).

    販促カレンダーの「最大付与率」行と突き合わせるために使う。
    クーポンは率ではないため含めない。
    """
    return sum(
        b.rate_for_basket(basket_yen)
        for b in benefits
        if b.is_active(part) and b.coupon_yen <= 0
    )


def store_funded_rate(
    benefits: list[Benefit], part: Participation, store: StoreConfig, own_rate: float
) -> float:
    """自社が負担する実効還元率.

    カレンダー掲載の自社負担施策(ストアポイント等)＋ボーナスストアPlusの自社設定率。
    モール負担の施策はここに入らない。
    """
    total = sum(
        benefit_rate(b, store)
        for b in benefits
        if b.is_active(part) and b.funding == "store"
    )
    if own_rate > 0:
        total += store.distribution.expected_rate(
            own_rate, 0.0, store.point_cap_per_order
        )
    return total


def point_cost(orders: float, aov: float, effective_rate: float, fee_rate: float = 0.0) -> float:
    """ポイント原資の総額.

    エントリーした日は「エントリーしなくても発生した注文」にも原資がかかる。
    この自然発生分へのコスト(カニバリ)を必ず含める。
    """
    return orders * aov * effective_rate * (1.0 + fee_rate)


def breakeven_uplift_ratio(store: StoreConfig, own_rate: float) -> float:
    """自社還元率 own_rate が元を取るのに必要な注文増加率.

    粗利率 m、自社実効還元率 e のとき  (1+u)*m - (1+u)*e = m  →  u = e / (m - e)
    (LTVは保守的に無視、常時かかるストアポイント分も除外した限界の評価)
    """
    eff = store.distribution.expected_rate(own_rate, 0.0, store.point_cap_per_order)
    eff *= 1.0 + store.point_fee_rate
    denom = store.gross_margin_rate - eff
    if denom <= 0:
        return INF
    return eff / denom
