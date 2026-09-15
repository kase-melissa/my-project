"""提案結果の出力(Markdown / CSV / JSON / HTMLカレンダー)."""

from __future__ import annotations

import csv
import html
import json
import unicodedata
from datetime import date, timedelta
from pathlib import Path

from .calendar_rules import WEEKDAY_JA, format_day
from .config import AppConfig
from .economics import breakeven_uplift_ratio
from .models import PlanResult, PromoSchedule

YEN = "{:,.0f}"


def _yen(v: float) -> str:
    return YEN.format(v) + "円"


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------
def render_markdown(cfg: AppConfig, schedule: PromoSchedule, plan: PlanResult) -> str:
    L: list[str] = []
    store = cfg.store
    L.append(f"# {schedule.month} ボーナスストア エントリー提案")
    L.append("")
    L.append(f"- ストア: **{store.store_name}**")
    L.append(f"- 作成基準日: {plan.generated_for.isoformat()}")
    L.append(f"- 行動モデル: {cfg.behavior.calibration_note}")
    L.append("")

    # ---- サマリー
    L.append("## 1. サマリー")
    L.append("")
    L.append("| 指標 | 値 |")
    L.append("|---|---:|")
    L.append(f"| 推奨エントリー日数 | {len(plan.selected_days())}日 |")
    L.append(f"| ポイント原資(想定) | {_yen(plan.total_cost)} |")
    L.append(f"| 月次予算 | {_yen(plan.budget)} ({plan.budget_used_ratio:.0%}消化) |")
    L.append(f"| 増分GMV(想定) | {_yen(plan.total_incremental_gmv)} |")
    L.append(f"| 増分ROAS | {plan.total_roas:.1f} |")
    L.append(f"| 純増効果(粗利増 - 原資 + LTV) | {_yen(plan.total_value)} |")
    L.append(f"| エントリーなしの月間GMV(基準線) | {_yen(plan.baseline_gmv)} |")
    total_gmv = plan.baseline_gmv + plan.total_incremental_gmv
    L.append(f"| 提案実行後の月間GMV(想定) | {_yen(total_gmv)} |")
    L.append("")
    if plan.total_cost > plan.budget:
        L.append(
            f"> **注意**: 必須指定イベントだけで予算を "
            f"{_yen(plan.total_cost - plan.budget)} 超過しています。"
            "予算増額か必須指定の見直しが必要です。"
        )
        L.append("")

    # ---- 締切アラート
    L.append("## 2. エントリー締切アラート")
    L.append("")
    pending = [
        (u, o) for u, o in plan.selected if u.entry_deadline is not None
    ]
    pending.sort(key=lambda t: t[0].entry_deadline)  # type: ignore[arg-type]
    if not pending:
        L.append("締切が設定された推奨エントリーはありません。")
    else:
        L.append("| 締切 | 残り | 対象 | 還元率 |")
        L.append("|---|---:|---|---:|")
        for u, o in pending:
            days_left = (u.entry_deadline - plan.generated_for).days  # type: ignore[operator]
            mark = "🔴" if days_left <= 2 else ("🟡" if days_left <= 5 else "🟢")
            L.append(
                f"| {mark} {u.entry_deadline.isoformat()} | {days_left}日 | "  # type: ignore[union-attr]
                f"{u.label} | +{o.bonus_rate:.0%} |"
            )
    L.append("")

    # ---- カレンダー
    L.append("## 3. エントリー推奨カレンダー")
    L.append("")
    L.append("```")
    L.append(_ascii_calendar(schedule, plan))
    L.append("```")
    L.append("")
    L.append("凡例: `[+4%]` = 推奨エントリー(還元率) / `----` = エントリー見送り")
    L.append("")

    # ---- 明細
    L.append("## 4. 推奨エントリー明細")
    L.append("")
    if not plan.selected:
        L.append("推奨できるエントリー日がありません。採算基準か予算設定を見直してください。")
    else:
        L.append("| 対象日 | イベント | 還元率 | 実効 | 注文(無→有) | 増分GMV | 原資 | ROAS | 純増効果 |")
        L.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
        for u, o in plan.selected:
            for est in o.estimates:
                ev = "、".join(
                    e.name for e in schedule.events_on(est.day)
                ) or "平常日"
                L.append(
                    f"| {format_day(est.day)} | {ev} | +{est.bonus_rate:.0%} | "
                    f"{est.effective_rate:.2%} | "
                    f"{est.base_orders:.0f}→{est.entry_orders:.0f} | "
                    f"{YEN.format(est.incremental_gmv)} | {YEN.format(est.point_cost)} | "
                    f"{est.roas:.1f} | {YEN.format(est.net_value)} |"
                )
    L.append("")

    # ---- 見送り
    L.append("## 5. 見送り判断とその理由")
    L.append("")
    if not plan.rejected:
        L.append("見送った候補はありません。")
    else:
        L.append("| 対象 | 最良案 | 原資 | ROAS | 純増効果 | 見送り理由 |")
        L.append("|---|---:|---:|---:|---:|---|")
        for u, o, reason in plan.rejected:
            L.append(
                f"| {u.label} | +{o.bonus_rate:.0%} | {YEN.format(o.cost)} | "
                f"{o.roas:.1f} | {YEN.format(o.value)} | {reason} |"
            )
    L.append("")

    if plan.blocked:
        L.append("## 6. エントリー不可(締切超過など)")
        L.append("")
        L.append("| 対象 | 理由 |")
        L.append("|---|---|")
        for u in plan.blocked:
            L.append(f"| {u.label} | {u.blocked_reason} |")
        L.append("")

    # ---- 損益分岐の目安
    L.append("## 7. 損益分岐の目安")
    L.append("")
    L.append(
        f"粗利率{store.gross_margin_rate:.0%}・平均単価{_yen(store.aov)}・"
        f"1注文あたり上限{_yen(store.point_cap_per_order)}の前提で、"
        "各還元率が元を取るのに必要な注文増加率:"
    )
    L.append("")
    L.append("| 還元率 | 必要な注文増加率 |")
    L.append("|---:|---:|")
    for r in cfg.behavior.candidate_rates:
        be = breakeven_uplift_ratio(store, r, store.aov)
        val = "達成不能(粗利率を超過)" if be == float("inf") else f"+{be:.1%}"
        L.append(f"| +{r:.0%} | {val} |")
    L.append("")
    L.append("> LTV(新規客の将来リピート)は保守的に除外した数値です。")
    L.append("")

    L.append("## 8. 前提")
    L.append("")
    L.append("- ポイント原資は、エントリーしなくても発生した自然注文分にも課金される前提で計上しています。")
    L.append("- イベントが重複する日は、効果を単純に掛け合わせず逓減させて合算しています。")
    L.append("- エントリー必須イベントは、エントリーしない場合に上乗せ分がまるごと機会損失になります。")
    if schedule.source_note:
        L.append(f"- スケジュール出典: {schedule.source_note}")
    L.append("")
    return "\n".join(L)


CELL_W = 8


def display_width(text: str) -> int:
    """端末上の表示幅. 全角文字を2桁として数える."""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def _center(text: str, width: int) -> str:
    """表示幅ベースで中央寄せする(str.center は全角を1桁と数えてしまう)."""
    pad = max(0, width - display_width(text))
    left = pad // 2
    return " " * left + text + " " * (pad - left)


def _ascii_calendar(schedule: PromoSchedule, plan: PlanResult) -> str:
    """月間カレンダーをテキストで描く(全セルを同じ表示幅に揃える)."""
    lines = [" ".join(_center(w, CELL_W) for w in WEEKDAY_JA)]
    first = schedule.first_day
    cur = first - timedelta(days=first.weekday())
    while cur <= schedule.last_day:
        cells = []
        for i in range(7):
            d = cur + timedelta(days=i)
            if d < schedule.first_day or d > schedule.last_day:
                cells.append(" " * CELL_W)
                continue
            rate = plan.rate_for(d)
            tag = f"[+{rate:.0%}]" if rate is not None else "----"
            cells.append(f"{d.day:2d}{tag:>{CELL_W - 2}}")
        lines.append(" ".join(cells))
        cur += timedelta(days=7)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CSV / JSON
# --------------------------------------------------------------------------
def write_csv(path: Path, schedule: PromoSchedule, plan: PlanResult) -> None:
    selected = plan.selected_days()
    rows = []
    for u, o in plan.selected:
        for est in o.estimates:
            rows.append(
                {
                    "date": est.day.isoformat(),
                    "weekday": WEEKDAY_JA[est.day.weekday()],
                    "events": "|".join(e.name for e in schedule.events_on(est.day)),
                    "entry": "YES",
                    "bonus_rate": f"{est.bonus_rate:.4f}",
                    "effective_rate": f"{est.effective_rate:.4f}",
                    "entry_deadline": u.entry_deadline.isoformat() if u.entry_deadline else "",
                    "base_orders": f"{est.base_orders:.1f}",
                    "entry_orders": f"{est.entry_orders:.1f}",
                    "incremental_gmv": f"{est.incremental_gmv:.0f}",
                    "point_cost": f"{est.point_cost:.0f}",
                    "roas": f"{est.roas:.2f}",
                    "net_value": f"{est.net_value:.0f}",
                    "reason": "",
                }
            )
    for u, o, reason in plan.rejected:
        for est in o.estimates:
            if est.day in selected:
                continue
            rows.append(
                {
                    "date": est.day.isoformat(),
                    "weekday": WEEKDAY_JA[est.day.weekday()],
                    "events": "|".join(e.name for e in schedule.events_on(est.day)),
                    "entry": "NO",
                    "bonus_rate": "",
                    "effective_rate": "",
                    "entry_deadline": u.entry_deadline.isoformat() if u.entry_deadline else "",
                    "base_orders": f"{est.base_orders:.1f}",
                    "entry_orders": "",
                    "incremental_gmv": "",
                    "point_cost": "",
                    "roas": "",
                    "net_value": "",
                    "reason": reason,
                }
            )
    rows.sort(key=lambda r: (r["date"], r["entry"]))
    # 列は固定。行が0件でもヘッダーだけは同じ形で出す
    fields = [
        "date", "weekday", "events", "entry", "bonus_rate", "effective_rate",
        "entry_deadline", "base_orders", "entry_orders", "incremental_gmv",
        "point_cost", "roas", "net_value", "reason",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def write_json(path: Path, schedule: PromoSchedule, plan: PlanResult) -> None:
    payload = {
        "month": plan.month,
        "generated_for": plan.generated_for.isoformat(),
        "summary": {
            "entry_days": len(plan.selected_days()),
            "point_cost": round(plan.total_cost),
            "budget": round(plan.budget),
            "incremental_gmv": round(plan.total_incremental_gmv),
            "roas": round(plan.total_roas, 2),
            "net_value": round(plan.total_value),
            "baseline_gmv": round(plan.baseline_gmv),
        },
        "entries": [
            {
                "label": u.label,
                "days": [d.isoformat() for d in u.days],
                "bonus_rate": o.bonus_rate,
                "entry_deadline": u.entry_deadline.isoformat() if u.entry_deadline else None,
                "entry_required_events": u.entry_required_events,
                "point_cost": round(o.cost),
                "incremental_gmv": round(o.incremental_gmv),
                "roas": round(o.roas, 2),
                "net_value": round(o.value),
            }
            for u, o in plan.selected
        ],
        "rejected": [
            {
                "label": u.label,
                "days": [d.isoformat() for d in u.days],
                "best_rate": o.bonus_rate,
                "reason": reason,
            }
            for u, o, reason in plan.rejected
        ],
        "blocked": [
            {"label": u.label, "reason": u.blocked_reason} for u in plan.blocked
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# HTML カレンダー
# --------------------------------------------------------------------------
def render_html(cfg: AppConfig, schedule: PromoSchedule, plan: PlanResult) -> str:
    cells: list[str] = []
    first = schedule.first_day
    cur = first - timedelta(days=first.weekday())
    while cur <= schedule.last_day:
        for i in range(7):
            d = cur + timedelta(days=i)
            if d < schedule.first_day or d > schedule.last_day:
                cells.append('<div class="cell empty"></div>')
                continue
            rate = plan.rate_for(d)
            evs = schedule.events_on(d)
            ev_html = "".join(
                f'<span class="ev">{html.escape(e.name)}</span>' for e in evs
            )
            cls = "cell on" if rate is not None else "cell off"
            badge = (
                f'<span class="rate">+{rate:.0%}</span>'
                if rate is not None
                else '<span class="rate none">見送り</span>'
            )
            cells.append(
                f'<div class="{cls}"><div class="dnum">{d.day}</div>'
                f"{badge}<div class=\"evs\">{ev_html}</div></div>"
            )
        cur += timedelta(days=7)

    heads = "".join(f'<div class="head">{w}</div>' for w in WEEKDAY_JA)
    rows = "".join(
        f'<tr><td>{html.escape(u.label)}</td>'
        f"<td>{u.entry_deadline.isoformat() if u.entry_deadline else '-'}</td>"
        f"<td>+{o.bonus_rate:.0%}</td><td>{YEN.format(o.cost)}</td>"
        f"<td>{o.roas:.1f}</td><td>{YEN.format(o.value)}</td></tr>"
        for u, o in plan.selected
    )
    return f"""<!doctype html>
<html lang="ja"><meta charset="utf-8">
<title>{html.escape(schedule.month)} ボーナスストア エントリー提案</title>
<style>
 body{{font-family:system-ui,"Hiragino Sans","Noto Sans JP",sans-serif;margin:24px;background:#fafafa;color:#1a1a1a}}
 h1{{font-size:20px}} h2{{font-size:16px;margin-top:28px}}
 .kpis{{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}}
 .kpi{{background:#fff;border:1px solid #e3e3e3;border-radius:8px;padding:10px 14px;min-width:140px}}
 .kpi b{{display:block;font-size:18px}} .kpi span{{font-size:11px;color:#666}}
 .cal{{display:grid;grid-template-columns:repeat(7,1fr);gap:6px}}
 .head{{text-align:center;font-size:12px;color:#666;padding:4px}}
 .cell{{background:#fff;border:1px solid #e3e3e3;border-radius:6px;min-height:78px;padding:6px}}
 .cell.on{{background:#eef7ee;border-color:#8ec18e}}
 .cell.empty{{background:transparent;border:none}}
 .dnum{{font-size:12px;color:#555}}
 .rate{{display:inline-block;font-weight:700;font-size:13px;color:#1d6b1d}}
 .rate.none{{color:#aaa;font-weight:400;font-size:11px}}
 .evs{{margin-top:4px;display:flex;flex-direction:column;gap:2px}}
 .ev{{font-size:10px;background:#fff3d6;border-radius:3px;padding:1px 3px}}
 table{{border-collapse:collapse;width:100%;background:#fff;font-size:13px}}
 th,td{{border:1px solid #e3e3e3;padding:6px 8px;text-align:right}}
 th:first-child,td:first-child{{text-align:left}}
</style>
<h1>{html.escape(schedule.month)} ボーナスストア エントリー提案 — {html.escape(cfg.store.store_name)}</h1>
<div class="kpis">
 <div class="kpi"><b>{len(plan.selected_days())}日</b><span>推奨エントリー日数</span></div>
 <div class="kpi"><b>{_yen(plan.total_cost)}</b><span>ポイント原資(予算{plan.budget_used_ratio:.0%})</span></div>
 <div class="kpi"><b>{_yen(plan.total_incremental_gmv)}</b><span>増分GMV</span></div>
 <div class="kpi"><b>{plan.total_roas:.1f}</b><span>増分ROAS</span></div>
 <div class="kpi"><b>{_yen(plan.total_value)}</b><span>純増効果</span></div>
</div>
<h2>エントリー推奨カレンダー</h2>
<div class="cal">{heads}{''.join(cells)}</div>
<h2>エントリー申込一覧</h2>
<table><tr><th>対象</th><th>締切</th><th>還元率</th><th>原資</th><th>ROAS</th><th>純増効果</th></tr>{rows}</table>
</html>"""
