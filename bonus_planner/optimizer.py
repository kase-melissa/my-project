"""月次ポイント原資の予算制約下でエントリー日程を選ぶ.

各エントリー単位につき「見送る」か「参加して還元率をひとつ選ぶ」の択一なので、
multiple-choice knapsack として解く(予算を離散化した動的計画法)。
貪欲法と違い「安い日を2つ取るより高い日を1つ取る方が良い」ケースを取りこぼさない。

目的関数は2つから選べる:
    gmv    … 利益床つきの売上最大化(既定)。ROAS下限と純増効果の下限を満たす
             範囲で増分GMVを最大化する。売上最大化の意図を保ちつつ赤字を防ぐ。
    profit … 純増効果(増分粗利 - 増分原資 + LTV)の最大化。
"""

from __future__ import annotations

from datetime import date

from .config import AppConfig
from .models import EntryOption, EntryUnit, PlanResult

BUCKET_YEN = 1000.0  # 予算離散化の刻み
OBJECTIVES = ("gmv", "profit")


def _viable_options(
    cfg: AppConfig, unit: EntryUnit
) -> tuple[list[EntryOption], list[tuple[EntryOption, str]]]:
    """採算基準を満たす選択肢と、外れた選択肢(理由付き)に分ける."""
    ok: list[EntryOption] = []
    ng: list[tuple[EntryOption, str]] = []
    for opt in unit.options:
        if opt.incremental_gmv <= 0:
            ng.append((opt, "増分GMVが見込めない"))
        elif opt.net_value <= cfg.store.min_net_value:
            ng.append((opt, f"純増効果が基準({cfg.store.min_net_value:,.0f}円)以下"))
        elif opt.roas < cfg.store.min_roas:
            ng.append((opt, f"ROAS {opt.roas:.1f} が下限{cfg.store.min_roas:.1f}未満"))
        else:
            ok.append(opt)
    return ok, ng


def _best(options: list[EntryOption], objective: str) -> EntryOption:
    return max(options, key=lambda o: (o.objective_value(objective), -o.cost))


def optimize(
    cfg: AppConfig,
    units: list[EntryUnit],
    month: str,
    today: date,
    objective: str = "gmv",
) -> PlanResult:
    if objective not in OBJECTIVES:
        raise ValueError(f"objective は {OBJECTIVES} のいずれかです: {objective}")

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
        if unit.mandatory:
            # 必須指定は採算に関わらず先に確保し、残予算で他を最適化する。
            # ROAS下限や純増効果の基準を満たす案が無くても、
            # 増分GMVが見込める案があるならその中で最も安いものを採る
            # (「入ること」自体が目的で、自社上乗せは最小限にしたい)。
            pool = ok or [o for o, _why in ng if o.incremental_gmv > 0]
            if pool:
                opt = _best(pool, objective) if ok else min(pool, key=lambda o: o.cost)
                selected.append((unit, opt))
                budget -= opt.cost
                continue
        if not ok:
            if ng:
                best_rejected = max(ng, key=lambda t: t[0].objective_value(objective))
                rejected.append((unit, best_rejected[0], best_rejected[1]))
            continue
        candidates.append((unit, ok))

    over_budget = budget < 0
    if over_budget:
        budget = 0.0

    chosen = _solve_knapsack(candidates, budget, objective)

    for unit, ok in candidates:
        opt = chosen.get(unit.key)
        if opt is not None:
            selected.append((unit, opt))
        else:
            rejected.append(
                (unit, _best(ok, objective), "月次ポイント原資の予算上限により見送り")
            )

    selected.sort(key=lambda t: t[0].days[0])
    rejected.sort(key=lambda t: t[0].days[0])

    return PlanResult(
        month=month,
        generated_for=today,
        objective=objective,
        participation=cfg.store.participation(bonus_store_plus=True),
        selected=selected,
        rejected=rejected,
        blocked=blocked,
        budget=cfg.store.monthly_point_budget,
        total_cost=sum(o.cost for _, o in selected),
        total_incremental_gmv=sum(o.incremental_gmv for _, o in selected),
        total_net_value=sum(o.net_value for _, o in selected),
        baseline_gmv=baseline_gmv,
    )


def _solve_knapsack(
    candidates: list[tuple[EntryUnit, list[EntryOption]]],
    budget: float,
    objective: str,
) -> dict[str, EntryOption]:
    """multiple-choice knapsack を DP で解き、単位キー -> 選択肢 を返す."""
    if not candidates or budget <= 0:
        return {}

    cap = int(budget // BUCKET_YEN)
    if cap <= 0:
        return {}

    # dp[c] = 予算c(バケット)以内で得られる最大の目的関数値(見送り可能なので下限は0)
    dp = [0.0] * (cap + 1)
    choice: list[list[EntryOption | None]] = []

    for _unit, options in candidates:
        new_dp = list(dp)
        row: list[EntryOption | None] = [None] * (cap + 1)
        for opt in options:
            cost_b = int(-(-opt.cost // BUCKET_YEN))  # 切り上げ
            cost_b = max(cost_b, 0)
            if cost_b > cap:
                continue
            value = opt.objective_value(objective)
            for c in range(cap, cost_b - 1, -1):
                cand = dp[c - cost_b] + value
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
            c -= max(int(-(-opt.cost // BUCKET_YEN)), 0)
    return result
