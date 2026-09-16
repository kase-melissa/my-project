"""前提を振ったときに提案がどれだけ変わるかを見る.

市場規模の弾力性とシェアの反応は日次データがないと校正できない。
そこで「この前提が外れていたら結論が変わるのか」を並べて示す。
結論が変わらない部分は安心して実行でき、変わる部分は実績で確かめる対象になる。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

from .config import AppConfig
from .models import PromoSchedule
from .optimizer import optimize
from .planner import build_entry_units


@dataclass
class Scenario:
    name: str
    description: str
    market_elasticity: float
    share_gain_at_reference: float
    entry_days: int
    point_cost: float
    incremental_gmv: float
    net_value: float
    selected_days: set[date]


# (名前, 説明, 市場規模の弾力性, 2ポイント優位での注文増加率)
#
# 「実測点推定」は、参加履歴と月次実績の回帰から出た点推定値。
# 統計的には0と区別できず信頼できないが、**下振れの目安**として置く。
# これでも残る日は、前提がどう外れても実行してよい日になる。
VARIANTS = (
    ("実測点推定", "月次回帰の点推定（下振れの目安）", 0.60, 0.06),
    ("弱気", "付与率への反応が想定より鈍い場合", 0.60, 0.12),
    ("既定", "現在の設定", None, None),
    ("強気", "付与率への反応が想定より強い場合", 1.00, 0.30),
)


def run_scenarios(
    cfg: AppConfig, schedule: PromoSchedule, today: date, objective: str = "gmv"
) -> list[Scenario]:
    out: list[Scenario] = []
    for name, desc, market, share_gain in VARIANTS:
        behavior = cfg.behavior
        if market is not None:
            behavior = replace(
                behavior, market_elasticity=market, share_gain_at_reference=share_gain
            )
        variant = AppConfig(store=cfg.store, behavior=behavior)
        units = build_entry_units(variant, schedule, today)
        plan = optimize(variant, units, schedule.month, today, objective=objective)
        out.append(
            Scenario(
                name=name,
                description=desc,
                market_elasticity=behavior.market_elasticity,
                share_gain_at_reference=behavior.share_gain_at_reference,
                entry_days=len(plan.selected_days()),
                point_cost=plan.total_cost,
                incremental_gmv=plan.total_incremental_gmv,
                net_value=plan.total_net_value,
                selected_days=plan.selected_days(),
            )
        )
    return out


def robust_days(scenarios: list[Scenario]) -> set[date]:
    """どの前提でも選ばれる日. 前提に依存せず実行してよい."""
    if not scenarios:
        return set()
    common = set(scenarios[0].selected_days)
    for s in scenarios[1:]:
        common &= s.selected_days
    return common


def fragile_days(scenarios: list[Scenario]) -> set[date]:
    """前提によって採否が変わる日. 実績で確かめる対象."""
    if not scenarios:
        return set()
    union: set[date] = set()
    for s in scenarios:
        union |= s.selected_days
    return union - robust_days(scenarios)


def average_baseline_factors(
    cfg: AppConfig, schedule: PromoSchedule
) -> tuple[float, float]:
    """その月の市場規模係数とシェア係数の平均.

    実績の月次データには、販促イベントによる上振れがすでに含まれている。

      セッション … 全ストア対象の施策(5のつく日など)で押し上げられている
      転換率     … プロモーションパッケージ加入で開く施策で押し上げられている

    一方モデルは base_sessions に市場規模係数を、base_cvr にシェア係数を掛けて
    その上振れを作る。補正しないと同じ効果を二度乗せることになり、
    ベースライン予測が系統的に過大になる。

    シェア側は「過去にボーナスストアPlusには参加していなかった」前提で計算する。
    実際に参加していた月があれば、その分だけ補正が足りず過大評価が残る。

    戻り値: (市場規模係数の平均, シェア係数の平均)
    """
    from .behavior import DemandModel, build_day_context
    from .economics import rate_by_scope

    model = DemandModel(cfg.behavior, cfg.store)
    part = cfg.store.participation(bonus_store_plus=False)
    market: list[float] = []
    share: list[float] = []
    for day in schedule.days():
        ctx = build_day_context(schedule, day)
        mall_wide, gated = rate_by_scope(ctx.benefits, part, cfg.store)
        market.append(model.market_factor(mall_wide))
        share.append(model.share_factor(gated))
    if not market:
        return 1.0, 1.0
    return sum(market) / len(market), sum(share) / len(share)


def average_market_factor(cfg: AppConfig, schedule: PromoSchedule) -> float:
    """市場規模係数の平均だけを返す薄いラッパ."""
    return average_baseline_factors(cfg, schedule)[0]


def gated_rate_profile(cfg: AppConfig, schedule: PromoSchedule) -> list[float]:
    """その月の各日の「参加資格つき施策による上乗せ率」.

    ボーナスストアPlus不参加の状態で計算する(プロモーションパッケージ分のみ)。
    """
    from .behavior import build_day_context
    from .economics import rate_by_scope

    part = cfg.store.participation(bonus_store_plus=False)
    out = []
    for day in schedule.days():
        ctx = build_day_context(schedule, day)
        _mall_wide, gated = rate_by_scope(ctx.benefits, part, cfg.store)
        out.append(gated)
    return out


def monthly_share_factor(
    cfg: AppConfig, gated_profile: list[float], own_rates: list[float]
) -> float:
    """その月の平均シェア係数.

    実績の転換率には、参加資格つき施策と自社の参加による上振れが
    すでに含まれている。これで割り戻して「何も上乗せがない日」の
    水準に引き直す。

    過去の販促カレンダーは残っていないため、**各月も対象月と同じ販促構成
    だった**と仮定する。さらに、販促日と自社の参加日の重なり方は分からないため、
    **両者は独立**と見なして掛け合わせる。
    """
    from .behavior import DemandModel

    if not gated_profile or not own_rates:
        return 1.0
    model = DemandModel(cfg.behavior, cfg.store)
    dist = cfg.store.distribution
    cap = cfg.store.point_cap_per_order
    # 自社設定率は実効値に直す(注文下限は無いが1注文あたり上限はかかる)
    own_effective = {r: (dist.expected_rate(r, 0.0, cap) if r > 0 else 0.0)
                     for r in set(own_rates)}
    total = 0.0
    for g in gated_profile:
        for r in own_rates:
            total += model.share_factor(g + own_effective[r])
    return total / (len(gated_profile) * len(own_rates))
