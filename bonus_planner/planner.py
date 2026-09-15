"""販促スケジュールからエントリー候補(EntryUnit)を組み立てる."""

from __future__ import annotations

from datetime import date

from .behavior import DemandModel, build_day_context
from .calendar_rules import format_day
from .config import AppConfig
from .economics import customer_perceived_rate, evaluate_profit
from .models import (
    DayContext,
    DayEstimate,
    EntryOption,
    EntryUnit,
    PromoSchedule,
)


def _union_period_groups(schedule: PromoSchedule) -> dict[date, str]:
    """期間一括エントリーのイベントに属する日をグループ化する.

    超PayPay祭のように期間通しでしかエントリーできないイベントは
    all-or-nothing で評価する必要がある。期間が重なる場合は1つに統合する。
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

    for ev in schedule.events:
        if ev.entry_unit == "period" and len(ev.dates) > 1:
            first = ev.dates[0]
            for d in ev.dates[1:]:
                union(first, d)

    return {d: f"g{find(d).isoformat()}" for d in schedule.days()}


def estimate_day(
    cfg: AppConfig, model: DemandModel, ctx: DayContext, nominal_rate: float
) -> DayEstimate:
    """1日・1還元率の見積り."""
    store = cfg.store
    base_orders = model.base_orders(ctx)
    aov = store.aov * model.aov_multiplier(ctx)

    perceived = customer_perceived_rate(nominal_rate, aov, store.point_cap_per_order)
    uplift = model.entry_uplift(ctx, perceived)
    entry_orders = base_orders * (1.0 + uplift)

    r = evaluate_profit(
        store=store,
        base_orders=base_orders,
        base_aov=aov,
        entry_orders=entry_orders,
        entry_aov=aov,
        nominal_rate=nominal_rate,
    )
    return DayEstimate(
        day=ctx.day,
        bonus_rate=nominal_rate,
        base_orders=base_orders,
        base_gmv=r["base_gmv"],
        entry_orders=entry_orders,
        entry_gmv=r["entry_gmv"],
        effective_rate=r["effective_rate"],
        point_cost=r["point_cost"],
        incremental_gmv=r["incremental_gmv"],
        incremental_orders=r["incremental_orders"],
        gross_profit_delta=r["gross_profit_delta"],
        ltv_value=r["ltv_value"],
        net_value=r["net_value"],
        roas=r["roas"],
        uplift_ratio=uplift,
    )


def _unit_label(days: list[date], contexts: dict[date, DayContext]) -> str:
    names: list[str] = []
    for d in days:
        for e in contexts[d].events:
            if e.name not in names:
                names.append(e.name)
    if len(days) == 1:
        span = format_day(days[0])
    elif (days[-1] - days[0]).days + 1 == len(days):
        span = f"{format_day(days[0])}〜{format_day(days[-1])}"
    else:
        # 連続していない日をまとめた期間一括エントリー。「〜」でつなぐと誤解を生む
        span = f"{format_day(days[0])}ほか計{len(days)}日"
    return f"{span} {'/'.join(names)}" if names else f"{span} 平常日"


def build_entry_units(
    cfg: AppConfig, schedule: PromoSchedule, today: date
) -> list[EntryUnit]:
    """月内の全日について、エントリー単位と還元率ごとの見積りを作る."""
    model = DemandModel(cfg.behavior)
    contexts = {d: build_day_context(schedule, d) for d in schedule.days()}
    groups = _union_period_groups(schedule)

    by_group: dict[str, list[date]] = {}
    for d in schedule.days():
        by_group.setdefault(groups[d], []).append(d)

    units: list[EntryUnit] = []
    for key, days in sorted(by_group.items(), key=lambda kv: kv[1][0]):
        days.sort()

        # グループ内の全日で選べる還元率の積集合(期間一括は同一還元率が前提)
        rate_sets = [set(model.allowed_rates(contexts[d])) for d in days]
        common = set.intersection(*rate_sets) if rate_sets else set()
        if not common:
            # 期間内で必要還元率が食い違う場合は、最も高い下限に合わせる
            floor = max(model.required_min_rate(contexts[d]) for d in days)
            common = {max(floor, min((r for s in rate_sets for r in s), default=floor))}

        options: list[EntryOption] = []
        for rate in sorted(common):
            estimates = [estimate_day(cfg, model, contexts[d], rate) for d in days]
            options.append(
                EntryOption(
                    bonus_rate=rate,
                    cost=sum(e.point_cost for e in estimates),
                    value=sum(e.net_value for e in estimates),
                    incremental_gmv=sum(e.incremental_gmv for e in estimates),
                    estimates=estimates,
                )
            )

        required_events = [
            e.name
            for d in days
            for e in model.entry_required_events(contexts[d])
        ]
        deadlines = [
            e.entry_deadline
            for d in days
            for e in contexts[d].events
            if e.entry_deadline is not None
        ]
        deadline = min(deadlines) if deadlines else None

        blocked = None
        if deadline is not None and deadline < today:
            blocked = f"エントリー締切({deadline.isoformat()})を過ぎています"
        elif days[-1] < today:
            blocked = "対象日が過去です"

        mandatory = any(
            e.id in cfg.store.mandatory_event_ids
            for d in days
            for e in contexts[d].events
        )

        units.append(
            EntryUnit(
                key=key,
                label=_unit_label(days, contexts),
                days=days,
                options=options,
                entry_deadline=deadline,
                entry_required_events=sorted(set(required_events)),
                mandatory=mandatory,
                blocked_reason=blocked,
            )
        )

    return units
