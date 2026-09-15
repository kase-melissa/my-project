"""月次ポイント原資の予算制約下でエントリー日程を選ぶ.

各エントリー単位につき「見送る」か「還元率をひとつ選ぶ」の択一なので、
multiple-choice knapsack として解く(予算を離散化した動的計画法)。
貪欲法と違い「安い日を2つ取るより高い日を1つ取る方が良い」ケースを取りこぼさない。
"""

from __future__ import annotations

from datetime import date

from .config import AppConfig
from .models import EntryOption, EntryUnit, PlanResult

BUCKET_YEN = 1000.0  # 予算離散化の刻み


def _viable_options(
    cfg: AppConfig, unit: EntryUnit
) -> tuple[list[EntryOption], list[tuple[EntryOption, str]]]:
    """採算基準を満たす選択肢と、外れた選択肢(理由付き)に分ける."""
    ok: list[EntryOption] = []
    ng: list[tuple[EntryOption, str]] = []
    for opt in unit.options:
        if opt.value <= cfg.store.min_net_value:
            ng.append((opt, f"純増効果が基準({cfg.store.min_net_value:,.0f}円)以下"))
        elif opt.roas < cfg.store.min_roas:
            ng.append((opt, f"ROAS {opt.roas:.1f} が下限{cfg.store.min_roas:.1f}未満"))
        else:
            ok.append(opt)
    return ok, ng


def _best(options: list[EntryOption]) -> EntryOption:
    return max(options, key=lambda o: (o.value, -o.cost))


def optimize(
    cfg: AppConfig,
    units: list[EntryUnit],
    month: str,
    today: date,
) -> PlanResult:
    budget = cfg.store.monthly_point_budget
    selected: list[tuple[EntryUnit, EntryOption]] = []
    rejected: list[tuple[EntryUnit, EntryOption, str]] = []
    blocked = [u for u in units if u.blocked_reason is not None]

    baseline_gmv = 0.0
    for u in units:
        if u.options:
            baseline_gmv += sum(e.base_gmv for e in u.options[0].estimates)

    candidates: list[tuple[EntryUnit, list[EntryOption]]] = []
    for unit in units:
        if unit.blocked_reason is not None:
            continue
        ok, ng = _viable_options(cfg, unit)
        if not ok:
            if ng:
                best_rejected = max(ng, key=lambda t: t[0].value)
                rejected.append((unit, best_rejected[0], best_rejected[1]))
            continue
        if unit.mandatory:
            # 経営判断で必須指定された単位は先に確保し、残予算で最適化する
            opt = _best(ok)
            selected.append((unit, opt))
            budget -= opt.cost
            continue
        candidates.append((unit, ok))

    if budget < 0:
        # 必須指定だけで予算超過。超過を許容しつつ警告は report 側で出す。
        budget = 0.0

    chosen = _solve_knapsack(candidates, budget)

    for unit, ok in candidates:
        opt = chosen.get(unit.key)
        if opt is not None:
            selected.append((unit, opt))
        else:
            best = _best(ok)
            rejected.append((unit, best, "月次ポイント原資の予算上限により見送り"))

    selected.sort(key=lambda t: t[0].days[0])
    rejected.sort(key=lambda t: t[0].days[0])

    total_cost = sum(o.cost for _, o in selected)
    return PlanResult(
        month=month,
        generated_for=today,
        selected=selected,
        rejected=rejected,
        blocked=blocked,
        budget=cfg.store.monthly_point_budget,
        total_cost=total_cost,
        total_incremental_gmv=sum(o.incremental_gmv for _, o in selected),
        total_value=sum(o.value for _, o in selected),
        baseline_gmv=baseline_gmv,
    )


def _solve_knapsack(
    candidates: list[tuple[EntryUnit, list[EntryOption]]], budget: float
) -> dict[str, EntryOption]:
    """multiple-choice knapsack を DP で解き、単位キー -> 選択肢 を返す."""
    if not candidates or budget <= 0:
        return {}

    cap = int(budget // BUCKET_YEN)
    if cap <= 0:
        return {}

    # dp[c] = 予算c(バケット)以内で得られる最大価値(見送り可能なので下限は0)
    dp = [0.0] * (cap + 1)
    # choice[i][c] = i番目の単位で選んだ選択肢(None=見送り)
    choice: list[list[EntryOption | None]] = []

    for _unit, options in candidates:
        new_dp = list(dp)
        row: list[EntryOption | None] = [None] * (cap + 1)
        for opt in options:
            cost_b = int(-(-opt.cost // BUCKET_YEN))  # 切り上げ
            if cost_b > cap:
                continue
            for c in range(cap, cost_b - 1, -1):
                cand = dp[c - cost_b] + opt.value
                if cand > new_dp[c]:
                    new_dp[c] = cand
                    row[c] = opt
        choice.append(row)
        dp = new_dp

    # 復元
    best_c = max(range(cap + 1), key=lambda c: dp[c])
    result: dict[str, EntryOption] = {}
    c = best_c
    for i in range(len(candidates) - 1, -1, -1):
        opt = choice[i][c]
        if opt is not None:
            result[candidates[i][0].key] = opt
            c -= int(-(-opt.cost // BUCKET_YEN))
    return result
