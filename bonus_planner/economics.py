"""ポイント原資と利益の計算.

ボーナスストアの経済性で一番効くのは「1注文あたり付与ポイント上限」。
犬猫用家電のような高単価商材では、表示還元率どおりの原資はかからない代わりに、
還元率を上げても顧客の体感メリットが頭打ちになる。
ここでは注文単価を対数正規分布で近似し、実効還元率を閉形式で求める。
"""

from __future__ import annotations

import math

from .config import StoreConfig


def _phi(x: float) -> float:
    """標準正規分布の累積分布関数."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def effective_point_rate(
    nominal_rate: float,
    aov: float,
    sigma: float,
    point_cap_per_order: float,
) -> float:
    """1注文あたり上限を考慮した実効還元率を返す.

    E[min(V * r, cap)] / E[V],  V ~ LogNormal(mu, sigma), E[V] = aov

    >>> round(effective_point_rate(0.04, 18000, 0.55, 5000), 4)
    0.04
    """
    if nominal_rate <= 0 or aov <= 0:
        return 0.0
    if point_cap_per_order is None or point_cap_per_order <= 0 or math.isinf(point_cap_per_order):
        return nominal_rate
    if sigma <= 0:
        # 単価がばらつかない場合は決定的に評価
        return min(nominal_rate, point_cap_per_order / aov)

    mu = math.log(aov) - sigma * sigma / 2.0
    threshold = point_cap_per_order / nominal_rate  # これを超える注文単価で上限が効く
    log_t = math.log(threshold)
    z1 = (log_t - mu - sigma * sigma) / sigma
    z2 = (log_t - mu) / sigma

    expected_cost = nominal_rate * aov * _phi(z1) + point_cap_per_order * (1.0 - _phi(z2))
    return expected_cost / aov


def point_cost(orders: float, aov: float, effective_rate: float, fee_rate: float = 0.0) -> float:
    """その日のポイント原資総額.

    エントリーした日は「エントリーしなくても発生した注文」にも原資がかかる。
    この自然発生分へのコスト(カニバリ)を必ず含めるのが重要。
    """
    return orders * aov * effective_rate * (1.0 + fee_rate)


def customer_perceived_rate(
    nominal_rate: float, aov: float, point_cap_per_order: float
) -> float:
    """平均的な注文における顧客の体感還元率.

    上限で頭打ちになる分、還元率を上げてもCVRが比例して伸びない根拠になる。
    """
    if nominal_rate <= 0 or aov <= 0:
        return 0.0
    if point_cap_per_order is None or point_cap_per_order <= 0:
        return nominal_rate
    return min(nominal_rate, point_cap_per_order / aov)


def evaluate_profit(
    store: StoreConfig,
    base_orders: float,
    base_aov: float,
    entry_orders: float,
    entry_aov: float,
    nominal_rate: float,
) -> dict[str, float]:
    """エントリー有無の差分から、その日の純増効果を求める."""
    eff_rate = effective_point_rate(
        nominal_rate, entry_aov, store.aov_sigma, store.point_cap_per_order
    )
    base_gmv = base_orders * base_aov
    entry_gmv = entry_orders * entry_aov
    incremental_gmv = entry_gmv - base_gmv
    incremental_orders = entry_orders - base_orders

    cost = point_cost(entry_orders, entry_aov, eff_rate, store.point_fee_rate)
    gross_profit_delta = incremental_gmv * store.gross_margin_rate - cost
    ltv_value = (
        max(incremental_orders, 0.0)
        * store.new_customer_ratio
        * store.ltv_uplift_per_new_customer
    )

    return {
        "effective_rate": eff_rate,
        "base_gmv": base_gmv,
        "entry_gmv": entry_gmv,
        "incremental_gmv": incremental_gmv,
        "incremental_orders": incremental_orders,
        "point_cost": cost,
        "gross_profit_delta": gross_profit_delta,
        "ltv_value": ltv_value,
        "net_value": gross_profit_delta + ltv_value,
        "roas": incremental_gmv / cost if cost > 0 else 0.0,
    }


def breakeven_uplift_ratio(store: StoreConfig, nominal_rate: float, aov: float) -> float:
    """損益分岐となる注文増加率.

    エントリーによって注文が何%増えれば元が取れるかを示す。
    粗利率 m、実効還元率 e のとき  (1+u)*m - (1+u)*e = m  →  u = e / (m - e)
    (LTVは保守的に無視)
    """
    eff = effective_point_rate(nominal_rate, aov, store.aov_sigma, store.point_cap_per_order)
    eff *= 1.0 + store.point_fee_rate
    denom = store.gross_margin_rate - eff
    if denom <= 0:
        return float("inf")
    return eff / denom
