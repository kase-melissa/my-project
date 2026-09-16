"""提案結果の出力(Markdown / CSV / JSON / HTMLカレンダー)."""

from __future__ import annotations

import csv
import html
import json
import statistics
import unicodedata
from datetime import date, timedelta
from pathlib import Path

from .behavior import DemandModel, build_day_context
from .calendar_rules import WEEKDAY_JA, format_day
from .config import AppConfig
from .economics import breakeven_uplift_ratio, perceived_total_rate
from .models import PlanResult, PromoSchedule

YEN = "{:,.0f}"
CELL_W = 12


def _yen(v: float) -> str:
    return YEN.format(v) + "円"


def display_width(text: str) -> int:
    """端末上の表示幅. 全角文字を2桁として数える."""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def _center(text: str, width: int) -> str:
    pad = max(0, width - display_width(text))
    left = pad // 2
    return " " * left + text + " " * (pad - left)


def _rjust(text: str, width: int) -> str:
    return " " * max(0, width - display_width(text)) + text


def _roas(v: float) -> str:
    return "∞" if v == float("inf") else f"{v:.1f}"


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------
def render_markdown(
    cfg: AppConfig,
    schedule: PromoSchedule,
    plan: PlanResult,
    scenarios: list | None = None,
    history: list | None = None,
) -> str:
    L: list[str] = []
    store = cfg.store
    L.append(f"# {schedule.month} ボーナスストア エントリー提案")
    L.append("")
    L.append(f"- ストア: **{store.store_name}**")
    L.append(f"- 作成基準日: {plan.generated_for.isoformat()}")
    L.append(
        f"- 計画対象: {schedule.first_day.isoformat()} 〜 "
        f"{schedule.planning_last_day.isoformat()}"
        + ("（爆買WEEKが月を跨ぐため11月分を含む）" if schedule.extends_to else "")
    )
    obj = "利益床つきの売上最大化" if plan.objective == "gmv" else "純利益の最大化"
    L.append(f"- 最適化目的: **{obj}**")
    L.append("")

    L += _section_assumptions(cfg, schedule, plan)
    L += _section_summary(cfg, plan)
    L += _section_deadlines(plan)
    L += _section_calendar(cfg, schedule, plan)
    L += _section_detail(cfg, schedule, plan)
    L += _section_rejected(plan)
    L += _section_sensitivity(scenarios)
    L += _section_validation(cfg, schedule, plan, history)
    L += _section_daily_rates(cfg, schedule, plan)
    L += _section_order_thresholds(cfg, schedule)
    L += _section_notes(schedule)
    L += _section_breakeven(cfg)
    return "\n".join(L)


def _section_order_thresholds(cfg: AppConfig, schedule: PromoSchedule) -> list[str]:
    """注文下限がどれだけ効いているかと、下限直下の注文の厚み.

    日程選択とは別の論点(価格・セット設計)なので、参考情報として分ける。
    """
    dist = cfg.store.distribution
    if not hasattr(dist, "near_miss"):
        return []

    L = ["## 10. 参考: 注文下限による目減りと、価格・セット設計の余地", ""]
    L.append(
        "販促カレンダーの注文下限は3,000〜25,000円に設定されている。"
        f"実測の平均注文単価は{_yen(dist.mean)}で、下限がこの近辺にあるため"
        "表示付与率のとおりには効かない。"
    )
    L.append("")
    L.append("| 施策 | 表示 | 注文下限 | 該当率 | 実効率 |")
    L.append("|---|---:|---:|---:|---:|")
    for b in schedule.benefits:
        if b.coupon_yen > 0:
            eff = dist.expected_coupon_rate(b.coupon_yen, b.min_order_yen)
            shown, floor = f"{b.coupon_yen:,.0f}円", b.min_order_yen
        elif b.tiers:
            eff = dist.expected_tiered_rate(b.tiers, b.user_cap_yen)
            shown = "/".join(f"{r:.0%}" for _t, r in sorted(b.tiers))
            floor = sorted(b.tiers)[0][0]
        else:
            eff = dist.expected_rate(b.rate, b.min_order_yen, b.user_cap_yen)
            shown, floor = f"{b.rate:.0%}", b.min_order_yen
        if floor <= 0:
            continue
        L.append(
            f"| {b.name} | {shown} | {floor:,.0f}円 | "
            f"{dist.qualifying_share(floor):.0%} | {eff:.2%} |"
        )
    L.append("")

    thresholds = sorted(
        {b.min_order_yen for b in schedule.benefits if b.min_order_yen > 0}
        | {t for b in schedule.benefits for t, _r in b.tiers}
    )
    L.append("### 注文下限の「あと一歩」")
    L.append("")
    L.append(
        "下限をわずかに下回る注文がどれだけあるか。"
        "まとめ買い誘導やセット設計で下限を越えられれば、そのぶん付与率が上がる。"
    )
    L.append("")
    rows = 0
    for t in thresholds:
        nm = dist.near_miss(t)
        if not nm["count"]:
            continue
        rows += 1
        L.append(
            f"- **下限 {t:,.0f}円**（該当 {nm['qualifying_share']:.0%}）: "
            f"{nm['floor']:,.0f}〜{t:,.0f}円 に **{nm['count']:,}件**"
        )
        for c in nm["clusters"][:3]:
            L.append(
                f"    - {c['price']:,.0f}円 × {c['count']:,}件"
                f"（あと **{c['gap']:,.0f}円**）"
            )
    if not rows:
        L.append("下限直下に目立つ注文はありません。")
    L.append("")
    L.append(
        "> これはエントリー日程とは別の施策。同梱・セット販売・送料無料ラインの"
        "見直しで下限を越えられれば、どの日にエントリーしても効果が上がる。"
    )
    L.append("")
    return L


def _section_sensitivity(scenarios: list | None) -> list[str]:
    """前提を振ったときの結論の振れ幅."""
    if not scenarios:
        return []
    from .sensitivity import fragile_days, robust_days

    L = ["## 7. 前提の感応度", ""]
    L.append(
        "付与率への反応は日次データがないと校正できない。"
        "前提が外れていたら結論が変わるのかを確認する。"
    )
    L.append("")
    L.append("| 前提 | 内容 | 市場規模の弾力性 | 2pt優位の効果 | エントリー日数 | 増分原資 | 増分GMV | 純増効果 |")
    L.append("|---|---|---:|---:|---:|---:|---:|---:|")
    for s in scenarios:
        L.append(
            f"| {s.name} | {s.description} | {s.market_elasticity:.2f} | "
            f"+{s.share_gain_at_reference:.0%} | {s.entry_days}日 | "
            f"{YEN.format(s.point_cost)} | {YEN.format(s.incremental_gmv)} | "
            f"{YEN.format(s.net_value)} |"
        )
    L.append("")

    robust = sorted(robust_days(scenarios))
    fragile = sorted(fragile_days(scenarios))
    L.append(
        f"- **どの前提でも選ばれる日（{len(robust)}日）**: "
        + ("、".join(format_day(d) for d in robust) if robust else "なし")
    )
    L.append(
        f"- **前提によって採否が変わる日（{len(fragile)}日）**: "
        + ("、".join(format_day(d) for d in fragile) if fragile else "なし")
    )
    L.append("")
    L.append(
        "> 前者は前提に依存しないので、そのまま実行してよい。"
        "後者は実績で反応を確かめる対象。"
    )
    L.append("")
    return L


def _section_validation(
    cfg: AppConfig, schedule: PromoSchedule, plan: PlanResult, history: list | None
) -> list[str]:
    """モデルのベースライン予測を実績と突き合わせる.

    前年同月比ではなく **直近3ヶ月の日次GMV** と比べる。
    成長が速い事業では前年比が大きく振れるため、閾値による判定に使えない。
    日次に揃えれば月の日数差も成長トレンドのノイズも受けずに比較できる。
    """
    if not history:
        return []
    year, month = (int(x) for x in schedule.month.split("-"))

    # 基準線から対象月ぶんだけを取り出す(月跨ぎ分を除く)
    month_baseline = 0.0
    month_days: set = set()

    def collect(estimates):
        nonlocal month_baseline
        for est in estimates:
            if est.day.month == month:
                month_baseline += est.base_gmv
                month_days.add(est.day)

    for _u, opt in plan.selected:
        collect(opt.estimates)
    for _u, opt, _r in plan.rejected:
        collect(opt.estimates)
    for unit in plan.blocked:
        if unit.options:
            collect(unit.options[0].estimates)

    if not month_days:
        return []
    model_daily = month_baseline / len(month_days)

    recent = [r for r in history if r.gmv > 0][-3:]
    if not recent:
        return []
    actual_daily = statistics.mean(
        r.gmv / (r.days or r.days_in_month) for r in recent
    )

    L = ["## 8. 実績との突き合わせ", ""]
    L.append(
        "モデルのベースライン予測が実績とかけ離れていないかの確認。"
        "大きく外れていれば係数かモデルのどちらかが誤っている。"
    )
    L.append("")
    L.append("| 項目 | 日次GMV |")
    L.append("|---|---:|")
    for r in recent:
        days = r.days or r.days_in_month
        L.append(
            f"| {r.key} 実績（{days}日）| {_yen(r.gmv / days)} |"
        )
    L.append(f"| **直近3ヶ月の平均** | **{_yen(actual_daily)}** |")
    L.append(
        f"| {schedule.month} ベースライン予測 | {_yen(model_daily)} |"
    )
    ratio = model_daily / actual_daily if actual_daily else 0.0
    L.append(f"| 比率 | {ratio:.2f}倍 |")
    L.append("")
    if not 0.75 <= ratio <= 1.35:
        L.append(
            f"> **注意**: モデルの日次予測が直近実績の {ratio:.2f} 倍と乖離しています。"
            "`base_sessions`・`base_cvr`・`aov` の設定を確認してください。"
        )
    else:
        L.append("> 妥当な範囲です。")
    L.append("")

    # 前年同月は参考値として併記する(判定には使わない)
    last_year = [r for r in history if r.year == year - 1 and r.month == month]
    if last_year and last_year[0].gmv > 0:
        ly = last_year[0]
        ly_days = ly.days or ly.days_in_month
        L.append(
            f"参考: {ly.key} の実績は日次 {_yen(ly.gmv / ly_days)}"
            f"（月計 {_yen(ly.gmv)}）。"
            f"前年同月比では {month_baseline / ly.gmv:.2f}倍 になる。"
        )
        L.append("")
        if recent and recent[-1].sessions:
            first = history[0]
            if first.sessions and first.cvr:
                L.append(
                    f"成長の内訳: セッション {first.sessions:,.0f}→{recent[-1].sessions:,.0f}、"
                    f"転換率 {first.cvr:.1%}→{recent[-1].cvr:.1%}、"
                    f"平均注文単価 {_yen(first.aov)}→{_yen(recent[-1].aov)}"
                    f"（{first.key} → {recent[-1].key}）。"
                    "成長の大半は集客ではなく転換率と単価の改善による。"
                )
                L.append("")
    return L


def _section_assumptions(cfg: AppConfig, schedule: PromoSchedule, plan: PlanResult) -> list[str]:
    store = cfg.store
    L = ["## 1. 前提と校正状況", ""]
    L.append("この提案が何を所与としているか。読む前にここを確認すること。")
    L.append("")
    L.append("| 前提 | 値 |")
    L.append("|---|---|")
    L.append(
        f"| プロモーションパッケージ | "
        f"{'加入済み' if store.promo_package else '未加入'} "
        f"（感謝デー・プレミアムな日曜日・モールクーポン・爆買WEEKの開閉） |"
    )
    L.append(
        f"| 優良ストア | {'該当' if store.excellent_store else '非該当'} "
        f"（ボーナスストアPlus指定日が {'+3%' if store.excellent_store else '+2%'}） |"
    )
    L.append(
        "| 資金負担 | カレンダー掲載の上乗せは**モール負担**。"
        "自社原資はストアポイント1%＋自社設定率のみ |"
    )
    L.append(f"| 粗利率 | {store.gross_margin_rate:.0%} |")
    L.append(f"| 平均注文単価（水準） | {_yen(store.aov)} |")
    L.append(f"| 注文単価の分布 | {store.distribution.source} |")
    L.append(f"| 1注文あたり付与上限 | {_yen(store.point_cap_per_order)} |")
    L.append("")
    L.append("### 係数の校正状況")
    L.append("")
    L.append(f"- 行動モデル: {cfg.behavior.calibration_note}")
    L.append("")
    L.append("| 係数 | 状態 |")
    L.append("|---|---|")
    note = cfg.behavior.calibration_note
    # 校正済みかどうかは、calibrate が書き込む「〜で校正」の有無で判定する。
    # 注記には未校正の係数の説明も含まれるため、「未校正」の有無では判定できない。
    calibrated = "で校正" in note
    monthly_only = "月次実績" in note
    mark = "**実測で校正済み**" if calibrated else "初期仮値"
    L.append(f"| 基準セッション数 `base_sessions` | {mark} |")
    L.append(f"| 基準転換率 `base_cvr` | {mark} |")
    season = mark + (
        "（月次データは1ヶ月=1点のため弱い推定。縮小推定で過学習を抑制）"
        if calibrated and monthly_only else ""
    )
    L.append(f"| 月次季節係数 | {season} |")
    L.append(
        "| 平均注文単価 `aov` | config の設定値"
        "（`calibrate` が出す推奨値と突き合わせること） |"
    )
    if monthly_only:
        L.append("| 曜日係数 | **初期仮値**（日次データが必要） |")
        L.append("| 給料日サイクル係数 | **初期仮値**（日次データが必要） |")
    else:
        L.append(f"| 曜日係数 | {mark} |")
        L.append(f"| 給料日サイクル係数 | {mark} |")
    L.append(
        "| 市場規模の弾力性 `market_elasticity` | "
        "**初期仮値**（日次データ＋エントリー記録が必要） |"
    )
    L.append(
        "| シェアの反応 `share_gain_at_reference` | "
        "**初期仮値**（日次データ＋エントリー記録が必要） |"
    )
    if store.distribution.count:
        L.append(
            f"| 注文単価の分布 | **実測 {store.distribution.count:,}件**"
            "（対数正規の近似ではなく実測分布を使用） |"
        )
    else:
        L.append("| 注文単価のばらつき `aov_sigma` | **初期仮値**（注文明細が必要） |")
    L.append("")
    L.append(
        "> **金額の絶対値は目安として扱うこと。** 未校正の係数が残っているため、"
        "日ごとの優先順位づけには使えるが、増分GMVや純増効果の絶対額は"
        "実績での検証が必要。"
    )
    L.append("")
    if schedule.source_note:
        L.append(f"- スケジュール出典: {schedule.source_note}")
        L.append("")
    return L


def _section_summary(cfg: AppConfig, plan: PlanResult) -> list[str]:
    L = ["## 2. サマリー", ""]
    L.append("| 指標 | 値 |")
    L.append("|---|---:|")
    L.append(f"| 推奨エントリー日数 | {len(plan.selected_days())}日 |")
    L.append(f"| 増分ポイント原資（自社負担） | {_yen(plan.total_cost)} |")
    L.append(f"| 月次予算 | {_yen(plan.budget)}（{plan.budget_used_ratio:.0%}消化） |")
    L.append(f"| 増分GMV | {_yen(plan.total_incremental_gmv)} |")
    L.append(f"| 増分ROAS | {_roas(plan.total_roas)} |")
    L.append(f"| 純増効果（増分粗利 − 増分原資 + LTV） | {_yen(plan.total_net_value)} |")
    L.append(f"| エントリーなしの期間GMV（基準線） | {_yen(plan.baseline_gmv)} |")
    L.append(
        f"| 提案実行後の期間GMV | {_yen(plan.baseline_gmv + plan.total_incremental_gmv)} |"
    )
    L.append("")
    if plan.total_cost > plan.budget:
        L.append(
            f"> **注意**: 必須指定だけで予算を {_yen(plan.total_cost - plan.budget)} "
            "超過しています。予算増額か必須指定の見直しが必要です。"
        )
        L.append("")
    L.append(
        "> 増分ポイント原資は「エントリー判断によって増える分」。"
        "常時かかるストアポイント1%は基準線側に含めており、ここには入っていません。"
    )
    L.append("")
    return L


def _section_deadlines(plan: PlanResult) -> list[str]:
    L = ["## 3. エントリー締切アラート", ""]
    pending = [(u, o) for u, o in plan.selected if u.entry_deadline is not None]
    pending.sort(key=lambda t: t[0].entry_deadline)  # type: ignore[arg-type]
    if not pending:
        L.append("締切が設定された推奨エントリーはありません。")
    else:
        L.append("| 締切 | 残り | 対象 | 自社還元率 |")
        L.append("|---|---:|---|---:|")
        for u, o in pending:
            left = (u.entry_deadline - plan.generated_for).days  # type: ignore[operator]
            mark = "🔴" if left <= 2 else ("🟡" if left <= 5 else "🟢")
            L.append(
                f"| {mark} {u.entry_deadline.isoformat()} | {left}日 | "  # type: ignore[union-attr]
                f"{u.label} | +{o.store_rate:.0%} |"
            )
        L.append("")
        L.append(
            "> 締切はPDFに記載がないため開催3日前の暫定値です。"
            "判明次第スケジュールYAMLの `entry_deadline` に記入してください。"
        )
    L.append("")
    return L


def _section_calendar(cfg: AppConfig, schedule: PromoSchedule, plan: PlanResult) -> list[str]:
    L = ["## 4. エントリー推奨カレンダー", "", "```"]
    L.append(_ascii_calendar(cfg, schedule, plan))
    L.append("```")
    L.append("")
    L.append(
        "凡例: 各セルは `日付 / 顧客が受け取る総付与率 / 自社設定還元率`。"
        "`--` はエントリー見送り。"
    )
    L.append("")
    return L


def _ascii_calendar(cfg: AppConfig, schedule: PromoSchedule, plan: PlanResult) -> str:
    model = DemandModel(cfg.behavior, cfg.store)
    lines = [" ".join(_center(w, CELL_W) for w in WEEKDAY_JA)]
    first = schedule.first_day
    cur = first - timedelta(days=first.weekday())
    last = schedule.planning_last_day
    while cur <= last:
        top, bottom = [], []
        for i in range(7):
            d = cur + timedelta(days=i)
            if d < first or d > last:
                top.append(" " * CELL_W)
                bottom.append(" " * CELL_W)
                continue
            rate = plan.rate_for(d)
            ctx = build_day_context(schedule, d)
            part = cfg.store.participation(rate is not None)
            total = perceived_total_rate(ctx.benefits, part, cfg.store)
            if rate is not None:
                total += next(
                    e.effective_store_rate
                    for _, o in plan.selected
                    for e in o.estimates
                    if e.day == d
                )
            label = f"{d.day}日 {total:.0%}"
            mark = f"[+{rate:.0%}]" if rate is not None else "--"
            top.append(_center(label, CELL_W))
            bottom.append(_center(mark, CELL_W))
        lines.append(" ".join(top))
        lines.append(" ".join(bottom))
        cur += timedelta(days=7)
    return "\n".join(lines)


def _section_detail(cfg: AppConfig, schedule: PromoSchedule, plan: PlanResult) -> list[str]:
    L = ["## 5. 推奨エントリー明細", ""]
    if not plan.selected:
        L.append(
            "推奨できるエントリー日がありません。採算基準（`min_roas`）か"
            "予算設定を見直してください。"
        )
        L.append("")
        return L
    L.append(
        "| 対象日 | 参加で開く上乗せ | 自社率 | 総付与率 | 注文(不参加→参加) "
        "| 増分GMV | 増分原資 | ROAS | 純増効果 |"
    )
    L.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    for u, o in plan.selected:
        for est in o.estimates:
            gated = "、".join(
                b.name
                for b in schedule.benefits_on(est.day)
                if b.requires_entry and b.is_active(plan.participation)
            ) or "なし（自社上乗せのみ）"
            L.append(
                f"| {format_day(est.day)} | {gated} | +{est.store_rate:.0%} | "
                f"{est.entry_total_rate:.1%} | "
                f"{est.base_orders:.0f}→{est.entry_orders:.0f} | "
                f"{YEN.format(est.incremental_gmv)} | {YEN.format(est.point_cost)} | "
                f"{_roas(est.roas)} | {YEN.format(est.net_value)} |"
            )
    L.append("")
    return L


def _section_rejected(plan: PlanResult) -> list[str]:
    L = ["## 6. 見送り判断とその理由", ""]
    if not plan.rejected:
        L.append("見送った候補はありません。")
        L.append("")
    else:
        L.append("| 対象 | 最良案 | 増分原資 | ROAS | 純増効果 | 見送り理由 |")
        L.append("|---|---:|---:|---:|---:|---|")
        for u, o, reason in plan.rejected:
            L.append(
                f"| {u.label} | +{o.store_rate:.0%} | {YEN.format(o.cost)} | "
                f"{_roas(o.roas)} | {YEN.format(o.net_value)} | {reason} |"
            )
        L.append("")
    if plan.blocked:
        L.append("### エントリー不可（締切超過など）")
        L.append("")
        L.append("| 対象 | 理由 |")
        L.append("|---|---|")
        for u in plan.blocked:
            L.append(f"| {u.label} | {u.blocked_reason} |")
        L.append("")
    return L


def _section_daily_rates(cfg: AppConfig, schedule: PromoSchedule, plan: PlanResult) -> list[str]:
    """日別に「参加で何が開くか」を一覧にする. エントリー価値の根拠."""
    model = DemandModel(cfg.behavior, cfg.store)
    L = ["## 9. 日別の付与率内訳", ""]
    L.append(
        "「参加で開く分」がゼロの日は、エントリーしてもモール負担の上乗せが付かず、"
        "自社のポイントを配るだけになる。"
    )
    L.append("")
    L.append("| 日 | 有効な施策 | 不参加時 | 参加で開く分 | 推奨 |")
    L.append("|---|---|---:|---:|---|")
    out_part = cfg.store.participation(False)
    in_part = cfg.store.participation(True)
    for d in schedule.days():
        ctx = build_day_context(schedule, d)
        out_rate = perceived_total_rate(ctx.benefits, out_part, cfg.store)
        unlocked = perceived_total_rate(ctx.benefits, in_part, cfg.store) - out_rate
        names = "、".join(
            b.name for b in ctx.benefits
            if b.is_active(in_part) and b.id not in (
                "store_point", "line_daily", "paypay_credit", "lyp_premium"
            )
        ) or "定常施策のみ"
        rate = plan.rate_for(d)
        rec = f"**+{rate:.0%} でエントリー**" if rate is not None else "見送り"
        L.append(
            f"| {format_day(d)} | {names} | {out_rate:.1%} | "
            f"{'+' if unlocked > 0 else ''}{unlocked:.1%} | {rec} |"
        )
    L.append("")
    return L


def _section_notes(schedule: PromoSchedule) -> list[str]:
    if not schedule.notes:
        return []
    L = ["## 11. 運用上の注意", ""]
    for n in schedule.notes:
        if n.days:
            days = "、".join(format_day(d) for d in n.days)
            L.append(f"- **{days}**: {n.text}")
        else:
            L.append(f"- {n.text}")
    L.append("")
    return L


def _section_breakeven(cfg: AppConfig) -> list[str]:
    store = cfg.store
    L = ["## 12. 損益分岐の目安", ""]
    L.append(
        f"粗利率{store.gross_margin_rate:.0%}・平均単価{_yen(store.aov)}・"
        f"1注文あたり上限{_yen(store.point_cap_per_order)}の前提で、"
        "自社設定還元率が元を取るのに必要な注文増加率:"
    )
    L.append("")
    L.append("| 自社還元率 | 必要な注文増加率 |")
    L.append("|---:|---:|")
    for r in sorted(store.store_bonus_rates):
        if r <= 0:
            continue
        be = breakeven_uplift_ratio(store, r)
        val = "達成不能（粗利率を超過）" if be == float("inf") else f"+{be:.1%}"
        L.append(f"| +{r:.0%} | {val} |")
    L.append("")
    L.append("> LTV（新規客の将来リピート）は保守的に除外した数値です。")
    L.append("")
    return L


# --------------------------------------------------------------------------
# CSV / JSON
# --------------------------------------------------------------------------
CSV_FIELDS = [
    "date", "weekday", "benefits", "entry", "store_rate", "total_rate_no_entry",
    "total_rate_with_entry", "unlocked_mall_rate", "entry_deadline",
    "base_orders", "entry_orders", "incremental_gmv", "point_cost", "roas",
    "net_value", "reason",
]


def write_csv(path: Path, cfg: AppConfig, schedule: PromoSchedule, plan: PlanResult) -> None:
    out_part = cfg.store.participation(False)
    in_part = cfg.store.participation(True)
    selected_days = plan.selected_days()
    rows: list[dict[str, str]] = []

    reasons: dict[date, str] = {}
    for u, _o, reason in plan.rejected:
        for d in u.days:
            reasons[d] = reason
    for u in plan.blocked:
        for d in u.days:
            reasons[d] = u.blocked_reason or ""

    for d in schedule.days():
        ctx = build_day_context(schedule, d)
        out_rate = perceived_total_rate(ctx.benefits, out_part, cfg.store)
        unlocked = perceived_total_rate(ctx.benefits, in_part, cfg.store) - out_rate
        est = plan.estimate_for(d)
        row = {
            "date": d.isoformat(),
            "weekday": WEEKDAY_JA[d.weekday()],
            "benefits": "|".join(
                b.name for b in ctx.benefits if b.is_active(in_part)
            ),
            "entry": "YES" if d in selected_days else "NO",
            "store_rate": f"{est.store_rate:.4f}" if est else "",
            "total_rate_no_entry": f"{out_rate:.4f}",
            "total_rate_with_entry": f"{est.entry_total_rate:.4f}" if est else "",
            "unlocked_mall_rate": f"{unlocked:.4f}",
            "entry_deadline": "",
            "base_orders": f"{est.base_orders:.1f}" if est else "",
            "entry_orders": f"{est.entry_orders:.1f}" if est else "",
            "incremental_gmv": f"{est.incremental_gmv:.0f}" if est else "",
            "point_cost": f"{est.point_cost:.0f}" if est else "",
            "roas": (_roas(est.roas) if est else ""),
            "net_value": f"{est.net_value:.0f}" if est else "",
            "reason": "" if est else reasons.get(d, ""),
        }
        rows.append(row)

    for u, _o in plan.selected:
        if u.entry_deadline:
            for d in u.days:
                for r in rows:
                    if r["date"] == d.isoformat():
                        r["entry_deadline"] = u.entry_deadline.isoformat()

    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(rows)


def write_json(path: Path, plan: PlanResult) -> None:
    payload = {
        "month": plan.month,
        "generated_for": plan.generated_for.isoformat(),
        "objective": plan.objective,
        "participation": {
            "promo_package": plan.participation.promo_package,
            "excellent_store": plan.participation.excellent_store,
        },
        "summary": {
            "entry_days": len(plan.selected_days()),
            "point_cost": round(plan.total_cost),
            "budget": round(plan.budget),
            "incremental_gmv": round(plan.total_incremental_gmv),
            "roas": None if plan.total_roas == float("inf") else round(plan.total_roas, 2),
            "net_value": round(plan.total_net_value),
            "baseline_gmv": round(plan.baseline_gmv),
        },
        "entries": [
            {
                "label": u.label,
                "days": [d.isoformat() for d in u.days],
                "store_rate": o.store_rate,
                "entry_deadline": u.entry_deadline.isoformat() if u.entry_deadline else None,
                "unlocked_benefits": u.entry_required_benefits,
                "point_cost": round(o.cost),
                "incremental_gmv": round(o.incremental_gmv),
                "roas": None if o.roas == float("inf") else round(o.roas, 2),
                "net_value": round(o.net_value),
            }
            for u, o in plan.selected
        ],
        "rejected": [
            {
                "label": u.label,
                "days": [d.isoformat() for d in u.days],
                "best_store_rate": o.store_rate,
                "reason": reason,
            }
            for u, o, reason in plan.rejected
        ],
        "blocked": [{"label": u.label, "reason": u.blocked_reason} for u in plan.blocked],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# HTML カレンダー
# --------------------------------------------------------------------------
def render_html(cfg: AppConfig, schedule: PromoSchedule, plan: PlanResult) -> str:
    out_part = cfg.store.participation(False)
    in_part = cfg.store.participation(True)
    cells: list[str] = []
    first = schedule.first_day
    last = schedule.planning_last_day
    cur = first - timedelta(days=first.weekday())
    while cur <= last:
        for i in range(7):
            d = cur + timedelta(days=i)
            if d < first or d > last:
                cells.append('<div class="cell empty"></div>')
                continue
            ctx = build_day_context(schedule, d)
            out_rate = perceived_total_rate(ctx.benefits, out_part, cfg.store)
            unlocked = perceived_total_rate(ctx.benefits, in_part, cfg.store) - out_rate
            est = plan.estimate_for(d)
            names = "".join(
                f'<span class="ev">{html.escape(b.name)}</span>'
                for b in ctx.benefits
                if b.is_active(in_part)
                and b.id not in ("store_point", "line_daily", "paypay_credit", "lyp_premium")
            )
            cls = "cell on" if est else ("cell gated" if unlocked > 0 else "cell off")
            total = est.entry_total_rate if est else out_rate
            badge = (
                f'<span class="rate">+{est.store_rate:.0%} でエントリー</span>'
                if est
                else '<span class="rate none">見送り</span>'
            )
            unlock = (
                f'<span class="unlock">参加で +{unlocked:.1%}</span>' if unlocked > 0 else ""
            )
            month_mark = "11/" if d.month != first.month else ""
            cells.append(
                f'<div class="{cls}"><div class="dnum">{month_mark}{d.day}</div>'
                f'<div class="total">{total:.1%}</div>{badge}{unlock}'
                f'<div class="evs">{names}</div></div>'
            )
        cur += timedelta(days=7)

    heads = "".join(f'<div class="head">{w}</div>' for w in WEEKDAY_JA)
    rows = "".join(
        f"<tr><td>{html.escape(u.label)}</td>"
        f"<td>{u.entry_deadline.isoformat() if u.entry_deadline else '-'}</td>"
        f"<td>+{o.store_rate:.0%}</td><td>{YEN.format(o.cost)}</td>"
        f"<td>{_roas(o.roas)}</td><td>{YEN.format(o.net_value)}</td></tr>"
        for u, o in plan.selected
    )
    return f"""<!doctype html>
<html lang="ja"><meta charset="utf-8">
<title>{html.escape(schedule.month)} ボーナスストア エントリー提案</title>
<style>
 body{{font-family:system-ui,"Hiragino Sans","Noto Sans JP",sans-serif;margin:24px;background:#fafafa;color:#1a1a1a}}
 h1{{font-size:20px}} h2{{font-size:16px;margin-top:28px}}
 .kpis{{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}}
 .kpi{{background:#fff;border:1px solid #e3e3e3;border-radius:8px;padding:10px 14px;min-width:150px}}
 .kpi b{{display:block;font-size:18px}} .kpi span{{font-size:11px;color:#666}}
 .cal{{display:grid;grid-template-columns:repeat(7,1fr);gap:6px}}
 .head{{text-align:center;font-size:12px;color:#666;padding:4px}}
 .cell{{background:#fff;border:1px solid #e3e3e3;border-radius:6px;min-height:96px;padding:6px}}
 .cell.on{{background:#eef7ee;border-color:#6aa96a}}
 .cell.gated{{background:#fffbe9;border-color:#e0c26a}}
 .cell.empty{{background:transparent;border:none}}
 .dnum{{font-size:11px;color:#555}}
 .total{{font-size:17px;font-weight:700;color:#c0392b}}
 .rate{{display:block;font-weight:700;font-size:11px;color:#1d6b1d;margin-top:2px}}
 .rate.none{{color:#aaa;font-weight:400}}
 .unlock{{display:block;font-size:10px;color:#a07000}}
 .evs{{margin-top:4px;display:flex;flex-direction:column;gap:2px}}
 .ev{{font-size:10px;background:#f0f0f0;border-radius:3px;padding:1px 3px}}
 table{{border-collapse:collapse;width:100%;background:#fff;font-size:13px}}
 th,td{{border:1px solid #e3e3e3;padding:6px 8px;text-align:right}}
 th:first-child,td:first-child{{text-align:left}}
 .caution{{background:#fff3f3;border-left:4px solid #c0392b;padding:8px 12px;font-size:12px}}
</style>
<h1>{html.escape(schedule.month)} ボーナスストア エントリー提案 — {html.escape(cfg.store.store_name)}</h1>
<div class="caution">
 カレンダー掲載の上乗せは<b>モール負担</b>前提。自社原資はストアポイント1%＋自社設定率のみ。
 曜日係数・給料日サイクル・付与率への反応は<b>初期仮値</b>のため、金額の絶対値は目安。
</div>
<div class="kpis">
 <div class="kpi"><b>{len(plan.selected_days())}日</b><span>推奨エントリー日数</span></div>
 <div class="kpi"><b>{_yen(plan.total_cost)}</b><span>増分ポイント原資（予算{plan.budget_used_ratio:.0%}）</span></div>
 <div class="kpi"><b>{_yen(plan.total_incremental_gmv)}</b><span>増分GMV</span></div>
 <div class="kpi"><b>{_roas(plan.total_roas)}</b><span>増分ROAS</span></div>
 <div class="kpi"><b>{_yen(plan.total_net_value)}</b><span>純増効果</span></div>
</div>
<h2>エントリー推奨カレンダー</h2>
<p style="font-size:12px;color:#666">大きい数字＝顧客が受け取る総付与率。黄色＝参加でモール負担の上乗せが開く日。</p>
<div class="cal">{heads}{''.join(cells)}</div>
<h2>エントリー申込一覧</h2>
<table><tr><th>対象</th><th>締切</th><th>自社還元率</th><th>増分原資</th><th>ROAS</th><th>純増効果</th></tr>{rows}</table>
</html>"""
