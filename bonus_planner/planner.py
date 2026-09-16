"""販促スケジュールからエントリー候補(EntryUnit)を組み立てる.

判断の単位は「ボーナスストアPlusに参加するか」＋「自社設定還元率をいくらにするか」。
プロモーションパッケージ加入と優良ストア該当は月単位の前提条件なので、
ここでは所与として扱う。
"""

from __future__ import annotations

from datetime import date

from .behavior import DemandModel, build_day_context
from .calendar_rules import format_day
from .config import AppConfig
from .economics import (
    expected_benefit_rate,
    perceived_total_rate,
    point_cost,
    store_funded_rate,
)
from .models import DayContext, DayEstimate, EntryOption, EntryUnit, PromoSchedule


def _union_period_groups(schedule: PromoSchedule) -> dict[date, str]:
    """期間一括エントリーの施策に属する日をグループ化する.

    爆買WEEKのように期間通しでしかエントリーできない施策は all-or-nothing で
    評価する必要がある。期間が重なる場合は1つに統合する。
    """
    parent: dict[date, date] = {d: d for d in schedule.days()}

    def find(x: date) -> date:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: date, b: date) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for b in schedule.benefits:
        if b.entry_unit == "period" and len(b.days) > 1:
            first = b.days[0]
            for d in b.days[1:]:
                union(first, d)

    return {d: f"g{find(d).isoformat()}" for d in schedule.days()}


def estimate_day(
    cfg: AppConfig, model: DemandModel, ctx: DayContext, own_rate: float
) -> DayEstimate:
    """1日・1自社還元率の見積り.

    基準線は「参加しない場合」。参加しなくても常時施策とプロモパッケージ施策は
    効いているので、基準線はゼロではない。
    """
    store = cfg.store
    base_part = store.participation(bonus_store_plus=False)
    entry_part = store.participation(bonus_store_plus=True)

    own_effective = expected_benefit_rate(
        own_rate, store.aov, store.aov_sigma, 0.0, store.point_cap_per_order
    )

    base_total = perceived_total_rate(ctx.benefits, base_part, store)
    entry_total = perceived_total_rate(ctx.benefits, entry_part, store) + own_effective
    unlocked = entry_total - base_total - own_effective

    # 全ストア共通の付与率は参加有無で変わらない(市場規模は同じ)。
    # 変わるのは自社だけの上乗せ = シェア。
    mall_wide, base_gated = model.scoped_rates(ctx, base_part)
    _, entry_gated = model.scoped_rates(ctx, entry_part)

    # 参加してもセッション数は変わらない(モール全体の集客は同じ)。
    # 変わるのは転換率。
    sessions = model.sessions(ctx, base_part)
    base_orders = sessions * model.cvr(ctx, base_part)
    entry_orders = sessions * model.cvr(ctx, entry_part, own_effective)

    aov = store.aov
    base_gmv = base_orders * aov
    entry_gmv = entry_orders * aov
    incremental_gmv = entry_gmv - base_gmv
    incremental_orders = entry_orders - base_orders

    # 自社負担は「カレンダー掲載の自社負担施策＋自社設定率」。モール負担は入らない。
    base_store_rate = store_funded_rate(ctx.benefits, base_part, store, 0.0)
    entry_store_rate = store_funded_rate(ctx.benefits, entry_part, store, own_rate)

    base_cost = point_cost(base_orders, aov, base_store_rate, store.point_fee_rate)
    entry_cost = point_cost(entry_orders, aov, entry_store_rate, store.point_fee_rate)
    incremental_cost = entry_cost - base_cost

    gross_profit_delta = incremental_gmv * store.gross_margin_rate - incremental_cost
    ltv_value = (
        max(incremental_orders, 0.0)
        * store.new_customer_ratio
        * store.ltv_uplift_per_new_customer
    )
    net_value = gross_profit_delta + ltv_value

    return DayEstimate(
        day=ctx.day,
        store_rate=own_rate,
        base_total_rate=base_total,
        entry_total_rate=entry_total,
        unlocked_mall_rate=unlocked,
        base_orders=base_orders,
        base_gmv=base_gmv,
        entry_orders=entry_orders,
        entry_gmv=entry_gmv,
        effective_store_rate=own_effective,
        base_point_cost=base_cost,
        entry_point_cost=entry_cost,
        point_cost=incremental_cost,
        incremental_gmv=incremental_gmv,
        incremental_orders=incremental_orders,
        gross_profit_delta=gross_profit_delta,
        ltv_value=ltv_value,
        net_value=net_value,
        roas=incremental_gmv / incremental_cost if incremental_cost > 0 else float("inf"),
        uplift_ratio=(entry_orders / base_orders - 1.0) if base_orders > 0 else 0.0,
    )


def _unit_label(days: list[date], contexts: dict[date, DayContext]) -> str:
    """その単位で「エントリーによって開く施策」を見出しにする."""
    names: list[str] = []
    for d in days:
        for b in contexts[d].benefits:
            if b.requires_entry and b.name not in names:
                names.append(b.name)
    if not names:
        for d in days:
            for b in contexts[d].benefits:
                if b.requires_promo_package and b.name not in names:
                    names.append(b.name)

    if len(days) == 1:
        span = format_day(days[0])
    elif (days[-1] - days[0]).days + 1 == len(days):
        span = f"{format_day(days[0])}〜{format_day(days[-1])}"
    else:
        span = f"{format_day(days[0])}ほか計{len(days)}日"
    return f"{span} {'/'.join(names)}" if names else f"{span} 上乗せなし"


def build_entry_units(
    cfg: AppConfig, schedule: PromoSchedule, today: date
) -> list[EntryUnit]:
    """計画対象期間の全日について、エントリー単位と還元率ごとの見積りを作る."""
    model = DemandModel(cfg.behavior, cfg.store)
    contexts = {d: build_day_context(schedule, d) for d in schedule.days()}
    groups = _union_period_groups(schedule)

    by_group: dict[str, list[date]] = {}
    for d in schedule.days():
        by_group.setdefault(groups[d], []).append(d)

    units: list[EntryUnit] = []
    for key, days in sorted(by_group.items(), key=lambda kv: kv[1][0]):
        days.sort()

        options: list[EntryOption] = []
        for rate in sorted(set(cfg.store.store_bonus_rates)):
            estimates = [estimate_day(cfg, model, contexts[d], rate) for d in days]
            options.append(
                EntryOption(
                    store_rate=rate,
                    cost=sum(e.point_cost for e in estimates),
                    net_value=sum(e.net_value for e in estimates),
                    incremental_gmv=sum(e.incremental_gmv for e in estimates),
                    estimates=estimates,
                )
            )

        gated = sorted({
            b.name
            for d in days
            for b in contexts[d].benefits
            if b.requires_entry and b.is_active(cfg.store.participation(True))
        })
        deadlines = [
            b.entry_deadline
            for d in days
            for b in contexts[d].benefits
            if b.entry_deadline is not None and b.requires_entry
        ]
        deadline = min(deadlines) if deadlines else None

        blocked = None
        if deadline is not None and deadline < today:
            blocked = f"エントリー締切({deadline.isoformat()})を過ぎています"
        elif days[-1] < today:
            blocked = "対象日が過去です"

        mandatory = any(
            b.id in cfg.store.mandatory_benefit_ids
            for d in days
            for b in contexts[d].benefits
        )

        units.append(
            EntryUnit(
                key=key,
                label=_unit_label(days, contexts),
                days=days,
                options=options,
                entry_deadline=deadline,
                entry_required_benefits=gated,
                mandatory=mandatory,
                blocked_reason=blocked,
            )
        )

    return units
