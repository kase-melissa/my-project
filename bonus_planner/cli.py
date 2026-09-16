"""コマンドラインインターフェース."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import date
from pathlib import Path

import yaml

from .calibrate import calibrate_daily, calibrate_monthly, load_history
from .config import AppConfig
from .distribution import EmpiricalDistribution, load_order_values
from .optimizer import OBJECTIVES, optimize
from .planner import build_entry_units
from .report import render_html, render_markdown, write_csv, write_json
from .participation import (
    daily_rates,
    estimate_share_response,
    load_participation,
    monthly_summary,
)
from .sensitivity import (
    average_baseline_factors,
    gated_rate_profile,
    monthly_share_factor,
    run_scenarios,
)
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
        kind, rows = load_history(
            args.history, today=today, partial_days=args.partial_days
        )
        if kind == "monthly":
            history = rows
        else:
            print(
                "[注意] 前年同月との突き合わせは月次実績でのみ行います",
                file=sys.stderr,
            )

    share_estimate = None
    participation_summary = None
    if history:
        participation = _load_participation_or_empty(
            args.participation or cfg.store.participation_file, quiet=True
        )
        if participation:
            participation_summary = []
            series = []
            for r in history:
                summary = monthly_summary(participation, r.year, r.month, r.days)
                cvr = r.cvr or 0.0
                participation_summary.append(
                    dict(key=r.key, cvr=cvr, entry_days=summary.entry_days,
                         days=summary.days_covered, share=summary.share,
                         average_rate=summary.average_rate)
                )
                series.append((r.key, cvr, summary.share, summary.average_rate))
            try:
                share_estimate = estimate_share_response(
                    series,
                    reference_advantage=cfg.behavior.reference_advantage,
                    share_elasticity=cfg.behavior.share_elasticity,
                )
            except ValueError:
                share_estimate = None

    md = render_markdown(
        cfg, schedule, plan, scenarios=scenarios, history=history,
        share_estimate=share_estimate, participation_summary=participation_summary,
    )
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
    kind, rows = load_history(
        args.history, today=_today(args.today), partial_days=args.partial_days
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    if kind == "monthly":
        avg_market, avg_share = 0.0, 1.0  # 0.0 は未指定を示す
        by_month = None
        if args.schedule:
            schedule = load_schedule(args.schedule)
            avg_market, avg_share = average_baseline_factors(cfg, schedule)
            participation = _load_participation_or_empty(
                args.participation or cfg.store.participation_file
            )
            by_month = _share_factor_by_month(cfg, schedule, rows, participation)
            print("実績に含まれる上振れを差し引きます:")
            print(f"  市場規模係数の平均 {avg_market:.3f}倍（セッション側）")
            if participation:
                lo, hi = min(by_month.values()), max(by_month.values())
                entered = sum(1 for r in rows
                              if monthly_summary(participation, r.year, r.month, r.days).entry_days)
                print(f"  シェア係数（転換率側）は月ごとに {lo:.3f}〜{hi:.3f}倍")
                print(f"    ボーナスストアPlus参加実績のある月: {entered}/{len(rows)}")
            else:
                print(f"  シェア係数の平均   {avg_share:.3f}倍（転換率側・参加履歴なし）")
            print()
        result = calibrate_monthly(
            rows, cfg.behavior, shrinkage=args.shrinkage,
            today=_today(args.today), average_market_factor=avg_market,
            average_share_factor=avg_share, share_factor_by_month=by_month,
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
    basis = "セッション" if result.basis == "sessions" else "注文数"
    print(f"月次実績と推定内訳（水準の基準: {basis}）:")
    print(
        f"  {'月':<9}{'注文':>7}{'ｾｯｼｮﾝ':>8}{'CVR':>7}{'単価':>8}{'GMV':>13}"
        f"{'暦調整後':>10}{'トレンド':>10}{'季節残差':>9}"
    )
    for r in rows:
        idx = result.monthly_index.get(r.key)
        trend = result.trend_at.get(r.key)
        resid = (idx / trend) if (idx and trend) else 0.0
        sess = r.sessions or 0
        cvr = r.cvr
        partial = " *" if r.is_partial else ""
        print(
            f"  {r.key:<9}{r.orders:>7,.0f}{sess:>8,.0f}"
            f"{(f'{cvr:.1%}' if cvr else '-'):>7}{r.aov:>8,.0f}{r.gmv:>13,.0f}"
            f"{(idx or 0):>10.2f}{(trend or 0):>10.2f}{resid:>9.3f}{partial}"
        )
    print("  * は部分月（実日数で按分）")
    print(
        "\n  「暦調整後」は曜日構成と給料日サイクルの偏りを除いた日次水準。"
        "\n  「季節残差」はトレンドからの乖離で、これを縮小して季節係数にしている。"
    )
    if result.cvr_trend_at:
        first, last = list(result.cvr_trend_at)[0], list(result.cvr_trend_at)[-1]
        print(
            f"\n  転換率のトレンド: {result.cvr_trend_at[first]:.2%} → "
            f"{result.cvr_trend_at[last]:.2%}（{first} → {last}）"
        )


def _share_factor_by_month(cfg, schedule, rows, participation) -> dict[str, float]:
    """各月の平均シェア係数. 実績に含まれる上振れを差し引くのに使う."""
    profile = gated_rate_profile(cfg, schedule)
    out: dict[str, float] = {}
    for r in rows:
        own = daily_rates(participation, r.year, r.month, r.days)
        out[r.key] = monthly_share_factor(cfg, profile, own)
    return out


def _load_participation_or_empty(path, quiet=False) -> dict:
    if not path:
        return {}
    try:
        return load_participation(path)
    except FileNotFoundError:
        if not quiet:
            print(f"[注意] 参加履歴が見つかりません: {path}", file=sys.stderr)
        return {}


def cmd_participation(args: argparse.Namespace) -> int:
    """参加履歴の集計と、シェア反応が推定できるかの検証."""
    cfg = AppConfig.load(args.config, args.behavior)
    path = args.participation or cfg.store.participation_file
    if not path:
        print("エラー: --participation か config の participation_file を指定してください",
              file=sys.stderr)
        return 2
    participation = load_participation(path)
    kind, rows = load_history(
        args.history, today=_today(args.today), partial_days=args.partial_days
    )
    if kind != "monthly":
        print("エラー: この検証は月次実績が必要です", file=sys.stderr)
        return 2

    print(f"参加履歴: {len(participation)}日分")
    print(f"  {'月':<9}{'CVR':>8}{'参加日':>7}{'日数':>6}{'参加率':>8}{'自社率':>8}")
    series = []
    for r in rows:
        summary = monthly_summary(participation, r.year, r.month, r.days)
        cvr = r.cvr or 0.0
        rate = f"{summary.average_rate:.0%}" if summary.entry_days else "-"
        print(
            f"  {r.key:<9}{cvr:>8.2%}{summary.entry_days:>7}"
            f"{summary.days_covered:>6}{summary.share:>8.0%}{rate:>8}"
        )
        series.append((r.key, cvr, summary.share, summary.average_rate))
    print()

    spend = [
        r.gmv * summary.share * summary.average_rate
        for r in rows
        for summary in [monthly_summary(participation, r.year, r.month, r.days)]
        if summary.entry_days
    ]
    if spend:
        print(f"過去のポイント原資（自社設定分の概算）: 月あたり {statistics.mean(spend):,.0f}円")
        print(f"  現在の月次予算 {cfg.store.monthly_point_budget:,.0f}円 との比 "
              f"{cfg.store.monthly_point_budget / statistics.mean(spend):.1f}倍")
        print()

    print("シェア反応は実測できるか:")
    try:
        est = estimate_share_response(
            series,
            reference_advantage=cfg.behavior.reference_advantage,
            share_elasticity=cfg.behavior.share_elasticity,
        )
    except ValueError as exc:
        print(f"  推定できません: {exc}")
        return 0
    for line in est.summary_lines():
        print(f"  {line}")
    if not est.identifiable:
        print()
        print("  → 月次データでは参加の効果とトレンドを分離できない。")
        print("     日別の注文数・セッション数があれば、参加日と非参加日を")
        print("     直接比較できるため、これが解ける。")
    return 0


def cmd_orders(args: argparse.Namespace) -> int:
    """注文明細の要約を出す. 注文下限がどれだけ効くかを確認する."""
    cfg = AppConfig.load(args.config, args.behavior)
    path = args.file or cfg.store.order_values_file
    if not path:
        print(
            "エラー: 注文明細のパスを --file か config の order_values_file で指定してください",
            file=sys.stderr,
        )
        return 2
    dist = EmpiricalDistribution(load_order_values(path), label=Path(path).name)
    schedule = load_schedule(args.schedule) if args.schedule else None

    print(f"注文明細: {dist.source}")
    print(f"  平均 {dist.mean:,.0f}円 / 中央値 {dist.quantile(0.5):,.0f}円 "
          f"/ 対数標準偏差 {dist.log_sigma():.3f}")
    print(
        "  分位点 "
        + "  ".join(
            f"{int(q*100)}%={dist.quantile(q):,.0f}"
            for q in (0.1, 0.25, 0.5, 0.75, 0.9, 0.99)
        )
    )
    print()

    if schedule:
        print("販促カレンダーの各施策が、この分布でどれだけ効くか:")
        print(f"  {'施策':<30}{'表示':>8}{'注文下限':>9}{'該当率':>8}{'実効率':>9}")
        print("  ※ 段階付与は注文ごとに段を判定。注文下限欄は最下段のしきい値")
        for b in schedule.benefits:
            if b.coupon_yen > 0:
                eff = dist.expected_coupon_rate(b.coupon_yen, b.min_order_yen)
                shown = f"{b.coupon_yen:,.0f}円"
                floor = b.min_order_yen
            elif b.tiers:
                eff = dist.expected_tiered_rate(b.tiers, b.user_cap_yen)
                shown = "/".join(f"{r:.0%}" for _t, r in sorted(b.tiers))
                floor = sorted(b.tiers)[0][0]
            else:
                eff = dist.expected_rate(b.rate, b.min_order_yen, b.user_cap_yen)
                shown = f"{b.rate:.0%}"
                floor = b.min_order_yen
            share = dist.qualifying_share(floor)
            print(
                f"  {b.name:<28}{shown:>8}{floor:>9,.0f}"
                f"{share:>8.0%}{eff:>9.2%}"
            )
        print()

    thresholds = _threshold_list(schedule)
    print("注文下限の「あと一歩」— まとめ買い誘導やセット設計で越えられるか:")
    for t in thresholds:
        nm = dist.near_miss(t)
        if not nm["count"]:
            continue
        print(
            f"  下限 {t:,.0f}円（該当 {nm['qualifying_share']:.0%}）: "
            f"{nm['floor']:,.0f}〜{t:,.0f}円 に {nm['count']:,}件"
        )
        for c in nm["clusters"][:3]:
            print(
                f"      {c['price']:>9,.0f}円 x {c['count']:>4}件"
                f"（あと {c['gap']:,.0f}円）"
            )
    print()

    if args.profile_out:
        out = Path(args.profile_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(dist.profile(tuple(thresholds)), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"要約統計を書き出しました: {out}")
        print("  明細そのものが無くても、この要約で再現・レビューできます")
    return 0


def _threshold_list(schedule) -> list[float]:
    """カレンダーに出てくる注文下限を重複なく拾う."""
    if schedule is None:
        return [3000.0, 5000.0, 20000.0, 25000.0]
    found = {b.min_order_yen for b in schedule.benefits if b.min_order_yen > 0}
    for b in schedule.benefits:
        for threshold, _rate in b.tiers:
            found.add(threshold)
    return sorted(found)


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
        "--participation", default=None,
        help="ボーナスストアPlus参加履歴CSV. 既定は config の participation_file",
    )
    sp.add_argument(
        "--partial-days", type=int, default=None,
        help="実績CSVの最終月の実日数. 省略時は --today から推定(前日まで)",
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
        "--participation", default=None,
        help="ボーナスストアPlus参加履歴CSV. 既定は config の participation_file",
    )
    sc.add_argument(
        "--partial-days", type=int, default=None,
        help="最終月の実日数. 省略時は --today から推定(前日まで)",
    )
    sc.add_argument(
        "--shrinkage", type=float, default=0.5,
        help="月次季節係数の縮小係数(0=仮値のまま, 1=残差をそのまま採用)",
    )
    sc.set_defaults(func=cmd_calibrate)

    spt = sub.add_parser(
        "participation", parents=[common],
        help="ボーナスストアPlus参加履歴の集計とシェア反応の推定可能性",
    )
    spt.add_argument("--history", required=True, help="月次実績CSV")
    spt.add_argument("--participation", default=None, help="参加履歴CSV")
    spt.add_argument("--partial-days", type=int, default=None, help="最終月の実日数")
    spt.set_defaults(func=cmd_participation)

    so = sub.add_parser("orders", parents=[common], help="注文明細の要約と注文下限の効き方")
    so.add_argument("--file", default=None, help="注文明細CSV(金額列). 既定は config の order_values_file")
    so.add_argument("--schedule", default=None, help="販促スケジュールYAML(施策別の実効率を出す)")
    so.add_argument("--profile-out", default=None, help="要約統計のJSON出力先")
    so.set_defaults(func=cmd_orders)

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
