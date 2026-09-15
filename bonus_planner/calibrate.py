"""自社の受注実績から行動モデルのパラメータを推定する.

既定値はあくまで初期仮値なので、必ず自社実績で上書きして使う。
外れ値(欠品・炎上・広告スパイク)に強くするため、平均ではなく中央値で推定する。

入力CSV(1行=1日):
    date,orders,gmv,entered,bonus_rate,events
    2026-06-01,38,684000,0,,
    2026-06-05,92,1840000,1,0.04,5のつく日
      entered ... その日ボーナスストアにエントリーしていたか(1/0)
      bonus_rate ... エントリー時の還元率(0.04 など)
      events  ... モール販促イベント名(複数は | 区切り、なければ空欄)
"""

from __future__ import annotations

import csv
import math
import statistics
from dataclasses import replace
from datetime import date
from pathlib import Path

from . import calendar_rules as cal
from .config import BehaviorParams

MIN_SAMPLES = 3


class HistoryRow:
    __slots__ = ("day", "orders", "gmv", "entered", "bonus_rate", "events")

    def __init__(self, day: date, orders: float, gmv: float, entered: bool,
                 bonus_rate: float, events: list[str]) -> None:
        self.day = day
        self.orders = orders
        self.gmv = gmv
        self.entered = entered
        self.bonus_rate = bonus_rate
        self.events = events


def load_history(path: str | Path) -> list[HistoryRow]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"実績CSVが見つかりません: {p}")
    rows: list[HistoryRow] = []
    with p.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        required = {"date", "orders"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"実績CSVに必要な列がありません: {sorted(missing)}")
        for i, r in enumerate(reader, start=2):
            try:
                orders = float(r["orders"] or 0)
                if orders <= 0:
                    continue
                rows.append(
                    HistoryRow(
                        day=date.fromisoformat(r["date"].strip()),
                        orders=orders,
                        gmv=float(r.get("gmv") or 0),
                        entered=str(r.get("entered", "")).strip() in ("1", "true", "True", "YES", "yes"),
                        bonus_rate=float(r.get("bonus_rate") or 0),
                        events=[e for e in (r.get("events") or "").split("|") if e.strip()],
                    )
                )
            except ValueError as exc:
                raise ValueError(f"実績CSVの{i}行目を解釈できません: {exc}") from exc
    if not rows:
        raise ValueError("実績CSVに有効な行がありません")
    return rows


def _median_ratio(values: list[float]) -> float | None:
    vals = [v for v in values if v > 0 and math.isfinite(v)]
    if len(vals) < MIN_SAMPLES:
        return None
    return statistics.median(vals)


def calibrate(
    rows: list[HistoryRow],
    prior: BehaviorParams,
) -> tuple[BehaviorParams, list[str]]:
    """実績から乗算モデルの係数を推定し、(新パラメータ, 注記) を返す."""
    notes: list[str] = []
    rows = sorted(rows, key=lambda r: r.day)

    # 平常日 = イベントなし・未エントリー・暦イベント(5のつく日/ゾロ目)でもない日。
    # 暦イベント日を混ぜると曜日・給料日サイクルの係数が上振れするため必ず除外する。
    plain = [
        r
        for r in rows
        if not r.events
        and not r.entered
        and not cal.is_five_day(r.day)
        and not cal.is_zorome(r.day)
    ]
    if len(plain) < MIN_SAMPLES * 4:
        notes.append(
            f"平常日サンプルが{len(plain)}日と少ないため、暦係数の一部は初期仮値のままです"
        )
    base = statistics.median([r.orders for r in plain]) if plain else prior.base_orders

    # -- 曜日係数
    dow = dict(prior.dow)
    for key in dow:
        vals = [r.orders / base for r in plain if cal.weekday_key(r.day) == key]
        m = _median_ratio(vals)
        if m is not None:
            dow[key] = round(m, 3)

    # 曜日係数を平均1に正規化(base_orders と役割が重複しないように)
    mean_dow = statistics.mean(dow.values())
    if mean_dow > 0:
        dow = {k: round(v / mean_dow, 3) for k, v in dow.items()}
        base *= mean_dow

    # -- 給料日サイクル係数(曜日を除去してから推定)
    dom = dict(prior.dom)
    for key in dom:
        vals = [
            r.orders / (base * dow.get(cal.weekday_key(r.day), 1.0))
            for r in plain
            if cal.dom_bucket(r.day) == key
        ]
        m = _median_ratio(vals)
        if m is not None:
            dom[key] = round(m, 3)
    mean_dom = statistics.mean(dom.values())
    if mean_dom > 0:
        dom = {k: round(v / mean_dom, 3) for k, v in dom.items()}
        base *= mean_dom

    # -- 月次の季節係数(換毛期・猛暑期・年末商戦)
    # base_orders と二重計上にならないよう、ここでも平均1に正規化して base に吸収させる
    month = {f"{m:02d}": prior.month.get(f"{m:02d}", 1.0) for m in range(1, 13)}
    for key in month:
        vals = [
            r.orders
            / (
                base
                * dow.get(cal.weekday_key(r.day), 1.0)
                * dom.get(cal.dom_bucket(r.day), 1.0)
            )
            for r in plain
            if f"{r.day.month:02d}" == key
        ]
        m_val = _median_ratio(vals)
        if m_val is not None:
            month[key] = round(m_val, 3)
    mean_month = statistics.mean(month.values())
    if mean_month > 0:
        month = {k: round(v / mean_month, 3) for k, v in month.items()}
        base *= mean_month

    # 暦係数は平均1に正規化済みなので、base が水準をすべて吸収している
    def calendar_pred(day: date) -> float:
        return (
            base
            * dow.get(cal.weekday_key(day), 1.0)
            * dom.get(cal.dom_bucket(day), 1.0)
            * month.get(f"{day.month:02d}", 1.0)
        )

    # -- 5のつく日 / ゾロ目(暦イベント)
    five_traffic = _median_ratio(
        [r.orders / calendar_pred(r.day) for r in rows
         if cal.is_five_day(r.day) and not r.entered and not r.events]
    )
    five_uplift = None
    if five_traffic:
        entered_five = [
            r.orders / (calendar_pred(r.day) * five_traffic)
            for r in rows
            if cal.is_five_day(r.day) and r.entered and r.bonus_rate > 0
        ]
        m = _median_ratio(entered_five)
        if m is not None:
            rates = [r.bonus_rate for r in rows if cal.is_five_day(r.day) and r.entered and r.bonus_rate > 0]
            response = (statistics.median(rates) / prior.reference_rate) ** prior.rate_elasticity
            if response > 0:
                five_uplift = round(max(m - 1.0, 0.0) / response, 3)

    # -- 平常日のエントリー上乗せ
    normal_uplift = None
    def _is_plain_entered(r: HistoryRow) -> bool:
        return (
            not r.events
            and r.entered
            and r.bonus_rate > 0
            and not cal.is_five_day(r.day)
            and not cal.is_zorome(r.day)
        )

    entered_plain = [r.orders / calendar_pred(r.day) for r in rows if _is_plain_entered(r)]
    m = _median_ratio(entered_plain)
    if m is not None:
        rates = [r.bonus_rate for r in rows if _is_plain_entered(r)]
        response = (statistics.median(rates) / prior.reference_rate) ** prior.rate_elasticity
        if response > 0:
            normal_uplift = round(max(m - 1.0, 0.0) / response, 3)

    params = replace(
        prior,
        base_orders=round(base, 2),
        dow=dow,
        dom=dom,
        month=month,
        five_day_traffic=round(five_traffic, 3) if five_traffic else prior.five_day_traffic,
        five_day_uplift=five_uplift if five_uplift is not None else prior.five_day_uplift,
        normal_day_uplift=normal_uplift if normal_uplift is not None else prior.normal_day_uplift,
    )

    if five_traffic is None:
        notes.append("5のつく日の需要倍率はサンプル不足のため初期仮値のままです")
    if five_uplift is None:
        notes.append("5のつく日のエントリー上乗せはサンプル不足のため初期仮値のままです")
    if normal_uplift is None:
        notes.append("平常日のエントリー上乗せはサンプル不足のため初期仮値のままです")

    note = (
        f"{rows[0].day.isoformat()}〜{rows[-1].day.isoformat()} の実績{len(rows)}日で校正"
        f"(平常日{len(plain)}日)"
    )
    if notes:
        note += " / " + " / ".join(notes)
    params = replace(params, calibration_note=note)
    return params, notes


def estimate_aov_sigma(order_values: list[float]) -> float:
    """注文明細の金額から対数標準偏差を推定する."""
    vals = [v for v in order_values if v > 0]
    if len(vals) < 30:
        raise ValueError("注文単価のばらつき推定には30件以上の注文明細が必要です")
    logs = [math.log(v) for v in vals]
    return statistics.stdev(logs)


def event_factors(
    rows: list[HistoryRow], params: BehaviorParams
) -> dict[str, dict[str, float | None]]:
    """イベント名ごとの需要倍率・エントリー上乗せを推定する(スケジュール記入の参考値)."""
    def pred(day: date) -> float:
        return (
            params.base_orders
            * params.dow.get(cal.weekday_key(day), 1.0)
            * params.dom.get(cal.dom_bucket(day), 1.0)
            * params.month.get(f"{day.month:02d}", 1.0)
        )

    names = sorted({e for r in rows for e in r.events})
    out: dict[str, dict[str, float | None]] = {}
    for name in names:
        subset = [r for r in rows if name in r.events]
        traffic = _median_ratio([r.orders / pred(r.day) for r in subset if not r.entered])
        entry = None
        if traffic:
            entered = [r for r in subset if r.entered and r.bonus_rate > 0]
            m = _median_ratio([r.orders / (pred(r.day) * traffic) for r in entered])
            if m is not None:
                med_rate = statistics.median([r.bonus_rate for r in entered])
                response = (med_rate / params.reference_rate) ** params.rate_elasticity
                if response > 0:
                    entry = round(max(m - 1.0, 0.0) / response, 3)
        # サンプル不足は None で返す。0.0 を返すと「効果なし」と誤読されるため。
        out[name] = {
            "samples": len(subset),
            "entered_samples": sum(1 for r in subset if r.entered),
            "traffic_multiplier": round(traffic, 3) if traffic else None,
            "entry_uplift": entry,
        }
    return out
