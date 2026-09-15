"""自社の実績から行動モデルのパラメータを推定する.

入力の粒度で校正できる範囲が変わる。できないものは仮値のまま残し、
`calibration_note` に明記する(黙って埋めない)。

月次 (month,orders,gmv[,days]) で校正できるもの:
    base_orders, month(季節係数), aov の推奨値
月次では校正できないもの:
    曜日係数, 給料日サイクル係数, 市場規模とシェアの弾力性, aov_sigma

日次 (date,orders,gmv) が揃えば、さらに曜日係数と給料日サイクル係数を校正できる。
"""

from __future__ import annotations

import calendar as _calendar
import csv
import math
import statistics
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path

from . import calendar_rules as cal
from .config import BehaviorParams

MIN_SAMPLES = 3
# 月次校正の縮小係数。1ヶ月あたり観測1点しかないため、素の残差をそのまま
# 季節係数にすると、その月固有の販促やノイズを季節性として焼き込んでしまう。
DEFAULT_SHRINKAGE = 0.5


# --------------------------------------------------------------------------
# 入力
# --------------------------------------------------------------------------
@dataclass
class DailyRow:
    day: date
    orders: float
    gmv: float


@dataclass
class MonthlyRow:
    year: int
    month: int
    orders: float
    gmv: float
    days: int | None = None  # 部分月の実日数

    @property
    def key(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"

    @property
    def days_in_month(self) -> int:
        return _calendar.monthrange(self.year, self.month)[1]

    @property
    def is_partial(self) -> bool:
        return self.days is not None and self.days < self.days_in_month

    def covered_days(self) -> list[date]:
        n = self.days or self.days_in_month
        return [date(self.year, self.month, i + 1) for i in range(n)]


def load_history(path: str | Path) -> tuple[str, list]:
    """実績CSVを読み、('daily'|'monthly', 行リスト) を返す.

    1列目が YYYY-MM なら月次、YYYY-MM-DD なら日次と判定する。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"実績CSVが見つかりません: {p}")
    with p.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fields = [c.strip() for c in (reader.fieldnames or [])]
        if not fields:
            raise ValueError("実績CSVにヘッダーがありません")
        key = fields[0]
        if key not in ("date", "month"):
            raise ValueError(
                f"実績CSVの1列目は date(日次) か month(月次) です: {key!r}"
            )
        if "orders" not in fields:
            raise ValueError("実績CSVに orders 列がありません")
        raw = list(reader)

    if not raw:
        raise ValueError("実績CSVにデータ行がありません")

    if key == "month":
        return "monthly", _parse_monthly(raw)
    return "daily", _parse_daily(raw)


def _parse_monthly(raw: list[dict]) -> list[MonthlyRow]:
    rows: list[MonthlyRow] = []
    for i, r in enumerate(raw, start=2):
        token = (r.get("month") or "").strip()
        try:
            year, month = (int(x) for x in token.split("-"))
            orders = float(r.get("orders") or 0)
            gmv = float(r.get("gmv") or 0)
            days_raw = (r.get("days") or "").strip()
            days = int(days_raw) if days_raw else None
        except ValueError as exc:
            raise ValueError(f"実績CSVの{i}行目を解釈できません: {exc}") from exc
        if orders <= 0:
            continue
        if days is not None and not 1 <= days <= _calendar.monthrange(year, month)[1]:
            raise ValueError(f"{token} の days が月の日数と矛盾しています: {days}")
        rows.append(MonthlyRow(year, month, orders, gmv, days))
    if not rows:
        raise ValueError("実績CSVに有効な行がありません")
    return sorted(rows, key=lambda r: (r.year, r.month))


def _parse_daily(raw: list[dict]) -> list[DailyRow]:
    rows: list[DailyRow] = []
    for i, r in enumerate(raw, start=2):
        try:
            day = date.fromisoformat((r.get("date") or "").strip())
            orders = float(r.get("orders") or 0)
            gmv = float(r.get("gmv") or 0)
        except ValueError as exc:
            raise ValueError(f"実績CSVの{i}行目を解釈できません: {exc}") from exc
        if orders <= 0:
            continue
        rows.append(DailyRow(day, orders, gmv))
    if not rows:
        raise ValueError("実績CSVに有効な行がありません")
    return sorted(rows, key=lambda r: r.day)


# --------------------------------------------------------------------------
# 共通のユーティリティ
# --------------------------------------------------------------------------
def theil_sen_slope(xs: list[float], ys: list[float]) -> float:
    """ペアごとの傾きの中央値. 外れ値に強い直線トレンド推定."""
    slopes = [
        (ys[j] - ys[i]) / (xs[j] - xs[i])
        for i in range(len(xs))
        for j in range(i + 1, len(xs))
        if xs[j] != xs[i]
    ]
    return statistics.median(slopes) if slopes else 0.0


def _calendar_weight(days: list[date], params: BehaviorParams) -> float:
    """その期間の「暦の重み」. 曜日構成と給料日サイクルの偏りを取り除く."""
    return sum(
        params.dow.get(cal.weekday_key(d), 1.0) * params.dom.get(cal.dom_bucket(d), 1.0)
        for d in days
    )


def _normalize(factors: dict[str, float]) -> tuple[dict[str, float], float]:
    """平均1に正規化し、(正規化後, 吸収したスケール) を返す."""
    mean = statistics.mean(factors.values())
    if mean <= 0:
        return factors, 1.0
    return {k: round(v / mean, 3) for k, v in factors.items()}, mean


# --------------------------------------------------------------------------
# 月次校正
# --------------------------------------------------------------------------
@dataclass
class MonthlyCalibration:
    params: BehaviorParams
    suggested_aov: float
    notes: list[str]
    monthly_index: dict[str, float]     # カレンダー構成を除去した日次水準
    trend_at: dict[str, float]          # トレンド値
    seasonal_raw: dict[str, float]      # 縮小前の残差


def calibrate_monthly(
    rows: list[MonthlyRow],
    prior: BehaviorParams,
    shrinkage: float = DEFAULT_SHRINKAGE,
    today: date | None = None,
    average_market_factor: float = 1.0,
) -> MonthlyCalibration:
    """月次実績から base_orders・月次季節係数・推奨aovを推定する.

    average_market_factor は二重計上を防ぐための補正。

    月次の注文数には、その月の販促イベント(5のつく日など)による上振れが
    すでに含まれている。一方モデルは base_orders に市場規模係数を掛けて
    イベント日の需要を作る。補正しないと、実績に含まれていた上振れを
    もう一度乗せることになり、ベースライン予測が系統的に過大になる。

    そこで「その月の市場規模係数の平均」で割り、base_orders を
    「定常施策だけの日(baseline_rate)の注文数」に引き戻す。
    """
    notes: list[str] = []
    if len(rows) < 6:
        notes.append(
            f"月次データが{len(rows)}ヶ月しかありません。"
            "季節係数はほとんど推定できないため仮値が多く残ります"
        )
    if today is not None:
        last = rows[-1]
        if (last.year, last.month) == (today.year, today.month) and last.days is None:
            notes.append(
                f"最終月({last.key})が当月ですが days 列が空です。"
                "月全体とみなすため水準を過小評価します。実日数を入れてください"
            )

    # 1. カレンダー構成を除去した日次水準
    index: dict[str, float] = {}
    for r in rows:
        weight = _calendar_weight(r.covered_days(), prior)
        if weight <= 0:
            continue
        index[r.key] = r.orders / weight

    if len(index) < 2:
        raise ValueError("月次校正には最低2ヶ月の実績が必要です")

    # 2. Theil–Sen で対数トレンドを推定(外れ値に強い)
    keys = list(index)
    xs = [float(i) for i in range(len(keys))]
    ys = [math.log(index[k]) for k in keys]
    slope = theil_sen_slope(xs, ys)
    intercept = statistics.median([y - slope * x for x, y in zip(xs, ys)])
    trend = {k: math.exp(intercept + slope * x) for k, x in zip(keys, xs)}

    # 3. 残差を季節係数に。1ヶ月=観測1点なので1.0方向へ縮小する。
    seasonal_raw: dict[str, float] = {}
    month_factors = {f"{m:02d}": prior.month.get(f"{m:02d}", 1.0) for m in range(1, 13)}
    observed: dict[str, list[float]] = {}
    for k in keys:
        mm = k.split("-")[1]
        observed.setdefault(mm, []).append(index[k] / trend[k])
    for mm, residuals in observed.items():
        raw = statistics.median(residuals)
        seasonal_raw[mm] = round(raw, 3)
        month_factors[mm] = round(1.0 + shrinkage * (raw - 1.0), 3)

    missing = sorted(set(month_factors) - set(observed))
    if missing:
        notes.append(f"実績のない月は仮値のままです: {', '.join(missing)}")

    month_factors, scale = _normalize(month_factors)

    # 4. base_orders は直近のトレンド水準 × 正規化で吸収したスケール
    #    さらに市場規模係数の平均で割り、定常施策だけの日の水準に引き戻す
    base_orders = trend[keys[-1]] * scale
    if average_market_factor and average_market_factor > 0:
        base_orders /= average_market_factor
        if abs(average_market_factor - 1.0) > 0.01:
            notes.append(
                f"販促イベントによる上振れ(平均{average_market_factor:.3f}倍)を"
                "実績から差し引いて base_orders を求めました"
            )
    else:
        notes.append(
            "販促スケジュールが未指定のため、base_orders に販促イベントの"
            "上振れが含まれたままです。ベースライン予測が過大になります"
            "(--schedule を指定してください)"
        )

    # 5. 推奨 aov は直近3ヶ月の中央値(直近の価格帯を反映)
    recent = [r for r in rows if r.gmv > 0][-3:]
    suggested_aov = (
        statistics.median([r.gmv / r.orders for r in recent]) if recent else 0.0
    )
    if not recent:
        notes.append("gmv 列が空のため aov を推定できませんでした")

    yoy = f"{rows[0].key}〜{rows[-1].key}"
    note = (
        f"月次実績{len(rows)}ヶ月({yoy})で校正。"
        f"base_orders と月次季節係数のみ実測。"
        f"曜日係数・給料日サイクル係数・市場規模とシェアの弾力性・aov_sigma は未校正(仮値)"
    )
    if notes:
        note += " / " + " / ".join(notes)

    params = replace(
        prior,
        base_orders=round(base_orders, 2),
        month=month_factors,
        calibration_note=note,
    )
    return MonthlyCalibration(
        params=params,
        suggested_aov=round(suggested_aov, 1),
        notes=notes,
        monthly_index={k: round(v, 3) for k, v in index.items()},
        trend_at={k: round(v, 3) for k, v in trend.items()},
        seasonal_raw=seasonal_raw,
    )


# --------------------------------------------------------------------------
# 日次校正(将来、日次データが揃ったとき用)
# --------------------------------------------------------------------------
def calibrate_daily(
    rows: list[DailyRow], prior: BehaviorParams
) -> tuple[BehaviorParams, list[str]]:
    """日次実績から曜日係数・給料日サイクル係数・月次係数・base_orders を推定する.

    注意: ここで推定するのは「暦の形」だけ。付与率への反応(市場規模・シェアの
    弾力性)は日ごとの総付与率とエントリー有無の記録が要るため、ここでは触らない。
    """
    notes: list[str] = []
    base = statistics.median([r.orders for r in rows])

    dow = dict(prior.dow)
    for key in dow:
        vals = [r.orders / base for r in rows if cal.weekday_key(r.day) == key]
        if len(vals) >= MIN_SAMPLES:
            dow[key] = round(statistics.median(vals), 3)
    dow, scale = _normalize(dow)
    base *= scale

    dom = dict(prior.dom)
    for key in dom:
        vals = [
            r.orders / (base * dow.get(cal.weekday_key(r.day), 1.0))
            for r in rows
            if cal.dom_bucket(r.day) == key
        ]
        if len(vals) >= MIN_SAMPLES:
            dom[key] = round(statistics.median(vals), 3)
    dom, scale = _normalize(dom)
    base *= scale

    month = {f"{m:02d}": prior.month.get(f"{m:02d}", 1.0) for m in range(1, 13)}
    for key in month:
        vals = [
            r.orders
            / (
                base
                * dow.get(cal.weekday_key(r.day), 1.0)
                * dom.get(cal.dom_bucket(r.day), 1.0)
            )
            for r in rows
            if f"{r.day.month:02d}" == key
        ]
        if len(vals) >= MIN_SAMPLES * 3:
            month[key] = round(statistics.median(vals), 3)
    month, scale = _normalize(month)
    base *= scale

    notes.append(
        "市場規模とシェアの弾力性は未校正。"
        "日ごとの総付与率とエントリー有無の記録が必要です"
    )
    note = (
        f"日次実績{len(rows)}日({rows[0].day.isoformat()}〜{rows[-1].day.isoformat()})で校正。"
        "曜日・給料日サイクル・月次係数を実測。" + " / ".join(notes)
    )
    return replace(
        prior,
        base_orders=round(base, 2),
        dow=dow,
        dom=dom,
        month=month,
        calibration_note=note,
    ), notes


def estimate_aov_sigma(order_values: list[float]) -> float:
    """注文明細の金額から対数標準偏差を推定する."""
    vals = [v for v in order_values if v > 0]
    if len(vals) < 30:
        raise ValueError("注文単価のばらつき推定には30件以上の注文明細が必要です")
    return statistics.stdev([math.log(v) for v in vals])
