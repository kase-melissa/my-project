"""コマンドラインインターフェース."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import yaml

from .calibrate import calibrate_daily, calibrate_monthly, load_history
from .config import AppConfig
from .optimizer import OBJECTIVES, optimize
from .planner import build_entry_units
from .report import render_html, render_markdown, write_csv, write_json
from .sensitivity import average_market_factor, run_scenarios
from .schedule import load_schedule

DEFAULT_CONFIG = "config/config.yaml"
DEFAULT_BEHAVIOR = "config/behavior_priors.yaml"


def _today(value: str | None) -> date:
    return date.fromisoformat(value) if value else date.today()


def cmd_plan(args: argparse.Namespace) -> int:
    cfg = AppConfig.load(args.config, args.behavior)
    if args.budget is not None:
        cfg.store.monthly_point_budget = args.budget
    if args.min_roas is not None:
        cfg.store.min_roas = args.min_roas
    cfg.store.validate()

    schedule = load_schedule(args.schedule)
    today = _today(args.today)

    units = build_entry_units(cfg, schedule, today)
    plan = optimize(cfg, units, schedule.month, today, objective=args.objective)

    scenarios = None if args.no_sensitivity else run_scenarios(
        cfg, schedule, today, objective=args.objective
    )

    history = None
    if args.history:
        kind, rows = load_history(args.history)
        if kind == "monthly":
            history = rows
        else:
            print(
                "[注意] 前年同月との突き合わせは月次実績でのみ行います",
                file=sys.stderr,
            )

    md = render_markdown(cfg, schedule, plan, scenarios=scenarios, history=history)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"bonus_store_plan_{schedule.month}"

    (out_dir / f"{stem}.md").write_text(md, encoding="utf-8")
    write_csv(out_dir / f"{stem}.csv", cfg, schedule, plan)
    write_json(out_dir / f"{stem}.json", plan)
    (out_dir / f"{stem}.html").write_text(render_html(cfg, schedule, plan), encoding="utf-8")

    if not args.quiet:
        print(md)
    print(f"\n出力先: {out_dir}/{stem}.{{md,csv,json,html}}", file=sys.stderr)
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    cfg = AppConfig.load(args.config, args.behavior)
    kind, rows = load_history(args.history)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    if kind == "monthly":
        avg_market = 1.0
        if args.schedule:
            avg_market = average_market_factor(cfg, load_schedule(args.schedule))
            print(
                f"販促スケジュールから求めた市場規模係数の平均: {avg_market:.3f}倍"
                "（実績に含まれるイベント上振れを差し引きます）\n"
            )
        else:
            avg_market = 0.0  # 未指定を示す。calibrate 側で注意を出す
        result = calibrate_monthly(
            rows, cfg.behavior, shrinkage=args.shrinkage,
            today=_today(args.today), average_market_factor=avg_market,
        )
        params, notes = result.params, result.notes
        _print_monthly_detail(result, rows, cfg)
    else:
        params, notes = calibrate_daily(rows, cfg.behavior)
        result = None

    out.write_text(
        yaml.safe_dump(
            {"behavior": params.to_dict()},
            allow_unicode=True, sort_keys=False, default_flow_style=False,
        ),
        encoding="utf-8",
    )
    print(f"\n校正結果を書き出しました: {out}")
    print(f"  {params.calibration_note}")
    for n in notes:
        print(f"  [注意] {n}")

    if result is not None and result.suggested_aov > 0:
        suggested = Path(args.suggest_store)
        suggested.parent.mkdir(parents=True, exist_ok=True)
        suggested.write_text(
            yaml.safe_dump(
                {"store": {"aov": result.suggested_aov}},
                allow_unicode=True, sort_keys=False,
            ),
            encoding="utf-8",
        )
        print(f"\n推奨 aov: {result.suggested_aov:,.0f}円 (現在の設定: {cfg.store.aov:,.0f}円)")
        print(f"  → {suggested} に出力しました。確認のうえ config/config.yaml に反映してください")
        print("  (store設定は自動上書きしません)")
    return 0


def _print_monthly_detail(result, rows, cfg: AppConfig) -> None:
    print("月次実績と推定内訳:")
    print(
        f"  {'月':<9}{'注文':>8}{'GMV':>14}{'単価':>9}"
        f"{'暦調整後':>10}{'トレンド':>10}{'季節残差':>10}"
    )
    for r in rows:
        idx = result.monthly_index.get(r.key)
        trend = result.trend_at.get(r.key)
        resid = (idx / trend) if (idx and trend) else None
        aov = r.gmv / r.orders if r.orders else 0
        partial = " *" if r.is_partial else ""
        print(
            f"  {r.key:<9}{r.orders:>8,.0f}{r.gmv:>14,.0f}{aov:>9,.0f}"
            f"{(idx or 0):>10.2f}{(trend or 0):>10.2f}"
            f"{(resid if resid else 0):>10.3f}{partial}"
        )
    print("  * は部分月（days 指定あり）")
    print(
        "\n  「暦調整後」は曜日構成と給料日サイクルの偏りを除いた日次水準。"
        "\n  「季節残差」はトレンドからの乖離で、これを縮小して季節係数にしている。"
    )


def cmd_check(args: argparse.Namespace) -> int:
    """販促スケジュールの検証と、エントリー締切の一覧表示."""
    cfg = AppConfig.load(args.config, args.behavior)
    schedule = load_schedule(args.schedule)
    today = _today(args.today)
    part_in = cfg.store.participation(True)

    print(f"{schedule.month} 販促スケジュール: 施策{len(schedule.benefits)}件 — 検証OK")
    print(f"計画対象: {schedule.first_day} 〜 {schedule.planning_last_day}\n")

    print(f"{'施策':<34}{'対象':<28}{'開催日'}")
    for b in schedule.benefits:
        days = ", ".join(str(d.day) if d.month == schedule.first_day.month
                         else f"{d.month}/{d.day}" for d in b.days)
        if len(b.days) > 8:
            days = f"{len(b.days)}日間（毎日）"
        active = "" if b.is_active(part_in) else "  ← 現在の参加状態では対象外"
        print(f"  {b.name:<32}{b.eligibility:<28}{days}{active}")

    gated = [b for b in schedule.benefits if b.requires_entry and b.is_active(part_in)]
    print("\nエントリー締切:")
    overdue = 0
    if not gated:
        print("  ボーナスストアPlus参加で開く施策がありません")
    for b in sorted(gated, key=lambda x: x.entry_deadline or x.days[0]):
        dl = b.entry_deadline
        left = (dl - today).days if dl else None
        if left is not None and left < 0:
            overdue += 1
        left_s = "期限切" if (left is not None and left < 0) else (f"{left}日" if left is not None else "-")
        print(f"  {dl.isoformat() if dl else '-':<12}{left_s:>6}  {b.name}")

    if schedule.notes:
        print("\n運用上の注意:")
        for n in schedule.notes:
            days = "、".join(f"{d.month}/{d.day}" for d in n.days) if n.days else "全期間"
            print(f"  [{days}] {n.text}")

    if overdue:
        print(f"\n[警告] 締切を過ぎた施策が{overdue}件あります", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bonus-planner",
        description="Yahoo!ショッピング ボーナスストアのエントリー日程を提案する",
    )
    sub = p.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=DEFAULT_CONFIG, help="ストア設定YAML")
    common.add_argument("--behavior", default=DEFAULT_BEHAVIOR, help="行動モデルYAML")
    common.add_argument("--today", default=None, help="基準日(YYYY-MM-DD). 既定は本日")

    sp = sub.add_parser("plan", parents=[common], help="エントリー日程を提案する")
    sp.add_argument("--schedule", required=True, help="該当月の販促スケジュールYAML")
    sp.add_argument("--out", default="out", help="出力ディレクトリ")
    sp.add_argument("--budget", type=float, default=None, help="月次ポイント原資の上限(円)")
    sp.add_argument("--min-roas", type=float, default=None, help="採用するROASの下限")
    sp.add_argument(
        "--objective", choices=OBJECTIVES, default="gmv",
        help="gmv=利益床つきの売上最大化(既定) / profit=純利益の最大化",
    )
    sp.add_argument(
        "--history", default=None,
        help="月次実績CSV. 指定すると前年同月とベースライン予測を突き合わせる",
    )
    sp.add_argument(
        "--no-sensitivity", action="store_true",
        help="前提の感応度分析を省略する(高速化)",
    )
    sp.add_argument("--quiet", action="store_true", help="標準出力にレポートを出さない")
    sp.set_defaults(func=cmd_plan)

    sc = sub.add_parser("calibrate", parents=[common], help="実績から行動モデルを校正する")
    sc.add_argument("--history", required=True, help="実績CSV(月次 month,... / 日次 date,...)")
    sc.add_argument(
        "--schedule", default=None,
        help="販促スケジュールYAML. 月次校正で販促イベントの上振れを差し引くのに使う",
    )
    sc.add_argument("--out", default="config/behavior_calibrated.yaml", help="出力YAML")
    sc.add_argument(
        "--suggest-store", default="config/store_suggested.yaml",
        help="推奨 aov の出力先",
    )
    sc.add_argument(
        "--shrinkage", type=float, default=0.5,
        help="月次季節係数の縮小係数(0=仮値のまま, 1=残差をそのまま採用)",
    )
    sc.set_defaults(func=cmd_calibrate)

    sk = sub.add_parser("check", parents=[common], help="スケジュール検証と締切一覧")
    sk.add_argument("--schedule", required=True, help="該当月の販促スケジュールYAML")
    sk.set_defaults(func=cmd_check)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
