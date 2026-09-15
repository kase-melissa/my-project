"""コマンドラインインターフェース."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import yaml

from .calibrate import calibrate, event_factors, load_history
from .config import AppConfig
from .optimizer import optimize
from .planner import build_entry_units
from .report import render_html, render_markdown, write_csv, write_json
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

    schedule = load_schedule(args.schedule)
    today = _today(args.today)

    units = build_entry_units(cfg, schedule, today)
    plan = optimize(cfg, units, schedule.month, today)

    md = render_markdown(cfg, schedule, plan)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"bonus_store_plan_{schedule.month}"

    (out_dir / f"{stem}.md").write_text(md, encoding="utf-8")
    write_csv(out_dir / f"{stem}.csv", schedule, plan)
    write_json(out_dir / f"{stem}.json", schedule, plan)
    (out_dir / f"{stem}.html").write_text(
        render_html(cfg, schedule, plan), encoding="utf-8"
    )

    if not args.quiet:
        print(md)
    print(f"\n出力先: {out_dir}/{stem}.{{md,csv,json,html}}", file=sys.stderr)
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    cfg = AppConfig.load(args.config, args.behavior)
    rows = load_history(args.history)
    params, notes = calibrate(rows, cfg.behavior)

    payload = {"behavior": params.to_dict()}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    print(f"校正結果を書き出しました: {out}")
    print(f"  {params.calibration_note}")
    for n in notes:
        print(f"  [注意] {n}")

    factors = event_factors(rows, params)
    if factors:
        print("\nイベント別の推定値(販促スケジュールYAMLへの記入の参考に):")
        print(f"  {'イベント':<22}{'件数':>5}{'ｴﾝﾄﾘ':>6}{'需要倍率':>12}{'上乗せ':>12}")
        for name, f in factors.items():
            traffic = f["traffic_multiplier"]
            uplift = f["entry_uplift"]
            t_s = f"{traffic:.2f}" if traffic is not None else "推定不可"
            u_s = f"{uplift:.2f}" if uplift is not None else "推定不可"
            print(
                f"  {name:<22}{f['samples']:>5.0f}{f['entered_samples']:>6.0f}"
                f"{t_s:>12}{u_s:>12}"
            )
        print("  ※ 「推定不可」はサンプル不足(効果がないという意味ではない)")
        print("  ※ 需要倍率は未エントリー日から、上乗せはエントリー日から推定するため、")
        print("     どちらかに偏っているイベントは推定できない。意図的に外す月を作ると精度が上がる")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """販促スケジュールの検証と、エントリー締切の一覧表示."""
    schedule = load_schedule(args.schedule)
    today = _today(args.today)
    print(f"{schedule.month} 販促スケジュール: イベント{len(schedule.events)}件 — 検証OK\n")
    print(f"{'締切':<12}{'残り':>5}  {'エントリー':<6} {'イベント'}")
    rows = sorted(
        schedule.events,
        key=lambda e: (e.entry_deadline or e.dates[0], e.dates[0]),
    )
    overdue = 0
    for e in rows:
        dl = e.entry_deadline
        left = (dl - today).days if dl else None
        if left is not None and left < 0:
            overdue += 1
        mark = "必須" if e.entry_required else "任意"
        left_s = "期限切" if (left is not None and left < 0) else (f"{left}日" if left is not None else "-")
        days = f"{e.dates[0].isoformat()}" + (f"〜{e.dates[-1].isoformat()}" if len(e.dates) > 1 else "")
        print(f"{dl.isoformat() if dl else '-':<12}{left_s:>5}  {mark:<6} {e.name} ({days})")
    if overdue:
        print(f"\n[警告] 締切を過ぎたイベントが{overdue}件あります", file=sys.stderr)
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
    sp.add_argument("--quiet", action="store_true", help="標準出力にレポートを出さない")
    sp.set_defaults(func=cmd_plan)

    sc = sub.add_parser("calibrate", parents=[common], help="実績から行動モデルを校正する")
    sc.add_argument("--history", required=True, help="日次受注実績CSV")
    sc.add_argument("--out", default="config/behavior_calibrated.yaml", help="出力YAML")
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
