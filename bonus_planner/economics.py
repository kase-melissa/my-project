"""ポイント原資と付与率の計算.

注文単価を対数正規分布 V ~ LogNormal(mu, sigma), E[V] = aov で近似し、
「注文下限金額」と「付与上限」を考慮した期待付与率を閉形式で求める。

犬猫用家電のような高単価商材では、この2つが逆方向に効く。

    注文下限金額  下限未満の注文には付かない   → 低単価側で効果が消える
    付与上限      1注文あたりの上限で頭打ち    → 高単価側で効果が消える

平均単価だけで判断するとどちらも見落とす。分布で積分するのが要点。
"""

from __future__ import annotations

import math

from .config import StoreConfig
from .models import ELIGIBILITY_ALL, Benefit, Participation

INF = float("inf")


def _phi(x: float) -> float:
    """標準正規分布の累積分布関数."""
    if x <= -40.0:
        return 0.0
    if x >= 40.0:
        return 1.0
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _lognormal_mu(aov: float, sigma: float) -> float:
    return math.log(aov) - sigma * sigma / 2.0


def _partial_expectation(aov: float, sigma: float, lo: float, hi: float) -> float:
    """E[V ; lo <= V <= hi] を返す."""
    if hi <= lo:
        return 0.0
    mu = _lognormal_mu(aov, sigma)
    z_hi = INF if hi == INF else (math.log(hi) - mu - sigma * sigma) / sigma
    z_lo = -INF if lo <= 0 else (math.log(lo) - mu - sigma * sigma) / sigma
    return aov * (_phi(z_hi) - _phi(z_lo))


def _survival(aov: float, sigma: float, x: float) -> float:
    """P(V > x) を返す."""
    if x == INF:
        return 0.0
    if x <= 0:
        return 1.0
    mu = _lognormal_mu(aov, sigma)
    return 1.0 - _phi((math.log(x) - mu) / sigma)


def expected_benefit_rate(
    rate: float,
    aov: float,
    sigma: float,
    min_order_yen: float = 0.0,
    cap_yen: float | None = None,
) -> float:
    """注文下限と付与上限を考慮した期待付与率.

    E[ min(V*rate, cap) * 1{V >= min_order} ] / E[V]

    >>> round(expected_benefit_rate(0.04, 20000, 0.55), 4)
    0.04
    """
    if rate <= 0 or aov <= 0:
        return 0.0
    if sigma <= 0:
        # ばらつきなしなら決定的に評価
        if aov < min_order_yen:
            return 0.0
        value = rate * aov
        if cap_yen is not None and cap_yen > 0:
            value = min(value, cap_yen)
        return value / aov

    cap = INF if cap_yen is None or cap_yen <= 0 else cap_yen
    lo = max(min_order_yen, 0.0)
    threshold = INF if cap == INF else cap / rate  # これを超える注文単価で上限が効く

    if lo >= threshold:
        # 下限を満たす注文はすべて上限に達している
        return cap * _survival(aov, sigma, lo) / aov

    capped_part = 0.0 if cap == INF else cap * _survival(aov, sigma, threshold)
    linear_part = rate * _partial_expectation(aov, sigma, lo, threshold)
    return (linear_part + capped_part) / aov


def expected_coupon_rate(
    coupon_yen: float, aov: float, sigma: float, min_order_yen: float = 0.0
) -> float:
    """定額クーポンを実効的な率に換算する."""
    if coupon_yen <= 0 or aov <= 0:
        return 0.0
    if sigma <= 0:
        return coupon_yen / aov if aov >= min_order_yen else 0.0
    qualify = 1.0 if min_order_yen <= 0 else _survival(aov, sigma, min_order_yen)
    return coupon_yen * qualify / aov


def benefit_rate(benefit: Benefit, store: StoreConfig) -> float:
    """1施策の期待付与率(自社の注文単価分布のもとで)."""
    if benefit.coupon_yen > 0:
        return expected_coupon_rate(
            benefit.coupon_yen, store.aov, store.aov_sigma, benefit.min_order_yen
        )
    rate = benefit.rate_for_basket(store.aov)
    return expected_benefit_rate(
        rate, store.aov, store.aov_sigma, benefit.min_order_yen, benefit.user_cap_yen
    )


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
        total += expected_benefit_rate(
            own_rate, store.aov, store.aov_sigma, 0.0, store.point_cap_per_order
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
    eff = expected_benefit_rate(
        own_rate, store.aov, store.aov_sigma, 0.0, store.point_cap_per_order
    )
    eff *= 1.0 + store.point_fee_rate
    denom = store.gross_margin_rate - eff
    if denom <= 0:
        return INF
    return eff / denom
