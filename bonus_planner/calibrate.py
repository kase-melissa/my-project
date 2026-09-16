"""自社の実績から行動モデルのパラメータを推定する.

入力の粒度で校正できる範囲が変わる。できないものは仮値のまま残し、
`calibration_note` に明記する(黙って埋めない)。

受け付ける入力:
    1. ストアクリエイターProの実績エクスポート(1列目が「日付」、CP932)
    2. 簡易月次 month,orders,gmv[,days]
    3. 日次 date,orders,gmv

月次で校正できるもの:
    base_sessions, base_cvr, month(季節係数), aov の推奨値
    ※ セッション列があるときだけ集客と転換率を分離できる
月次では校正できないもの:
    曜日係数, 給料日サイクル係数, 市場規模とシェアの弾力性, aov_sigma

日次 (date,orders,gmv) が揃えば、さらに曜日係数と給料日サイクル係数を校正できる。
"""

from __future__ import annotations

import calendar as _calendar
import csv
import io
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
    days: int | None = None      # 部分月の実日数
    sessions: float | None = None
    buyers: float | None = None

    @property
    def aov(self) -> float:
        return self.gmv / self.orders if self.orders else 0.0

    @property
    def cvr(self) -> float | None:
        if not self.sessions:
            return None
        return self.orders / self.sessions

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


# Yahoo!ショッピング ストアクリエイターPro の実績エクスポートの列名。
# 同じ「前年比」列が何度も現れるため、名前ではなく位置で拾う必要がある。
YAHOO_COLUMNS = {
    "month": "日付",
    "gmv": "売上合計値",
    "orders": "注文数 - 注文数合計",
    "buyers": "注文数 - 注文者数合計",
    "sessions": "セッション合計",
}


def _decode(path: Path) -> str:
    """ストアクリエイターProのエクスポートは CP932。UTF-8 も受ける."""
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"文字コードを判別できませんでした: {path}")


def load_history(
    path: str | Path, today: date | None = None, partial_days: int | None = None
) -> tuple[str, list]:
    """実績CSVを読み、('daily'|'monthly', 行リスト) を返す.

    受け付ける形式:
      1. ストアクリエイターProの実績エクスポート（1列目が「日付」、CP932）
      2. 簡易月次 month,orders,gmv[,days]
      3. 日次 date,orders,gmv

    形式1は日数列を持たないため、最終月が当月なら `today` から実日数を推定する
    (データは前日までとみなす)。`partial_days` で明示的に上書きできる。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"実績CSVが見つかりません: {p}")

    rows = list(csv.reader(io.StringIO(_decode(p))))
    if not rows:
        raise ValueError("実績CSVにヘッダーがありません")
    header = [c.strip() for c in rows[0]]
    body = [r for r in rows[1:] if r and any(c.strip() for c in r)]
    if not body:
        raise ValueError("実績CSVにデータ行がありません")

    key = header[0]
    if key == YAHOO_COLUMNS["month"]:
        parsed = _parse_yahoo_export(header, body)
    elif key in ("month", "date"):
        if "orders" not in header:
            raise ValueError("実績CSVに orders 列がありません")
        rows_as_dicts = [dict(zip(header, r)) for r in body]
        if key == "date":
            return "daily", _parse_daily(rows_as_dicts)
        parsed = _parse_monthly(rows_as_dicts)
    else:
        raise ValueError(
            f"実績CSVの1列目は「日付」(エクスポート) / month / date のいずれかです: {key!r}"
        )

    _apply_partial_days(parsed, today, partial_days)
    return "monthly", parsed


def _apply_partial_days(
    rows: list[MonthlyRow], today: date | None, partial_days: int | None
) -> None:
    """最終月が当月なら実日数を補う."""
    if not rows:
        return
    last = rows[-1]
    if partial_days is not None:
        last.days = partial_days
        return
    if last.days is not None or today is None:
        return
    if (last.year, last.month) == (today.year, today.month):
        # エクスポートは前日までのデータとみなす
        last.days = max(today.day - 1, 1)


def _column_index(header: list[str], name: str) -> int:
    try:
        return header.index(name)
    except ValueError as exc:
        raise ValueError(
            f"実績エクスポートに必要な列がありません: {name}"
        ) from exc


def _parse_yahoo_export(header: list[str], body: list[list[str]]) -> list[MonthlyRow]:
    """ストアクリエイターProの実績エクスポートを読む.

    「前年比」列が繰り返し現れるため、DictReader ではなく位置で拾う。
    """
    idx = {k: _column_index(header, v) for k, v in YAHOO_COLUMNS.items()}
    rows: list[MonthlyRow] = []
    for i, r in enumerate(body, start=2):
        token = r[idx["month"]].strip()
        try:
            year, month = (int(x) for x in token.split("-"))
            orders = float(r[idx["orders"]] or 0)
            gmv = float(r[idx["gmv"]] or 0)
            sessions = float(r[idx["sessions"]] or 0) or None
            buyers = float(r[idx["buyers"]] or 0) or None
        except (ValueError, IndexError) as exc:
            raise ValueError(f"実績エクスポートの{i}行目を解釈できません: {exc}") from exc
        if orders <= 0:
            continue
        rows.append(MonthlyRow(year, month, orders, gmv, None, sessions, buyers))
    if not rows:
        raise ValueError("実績エクスポートに有効な行がありません")
    return sorted(rows, key=lambda r: (r.year, r.month))


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
    basis: str                      # "sessions" | "orders"
    monthly_index: dict[str, float]  # カレンダー構成を除去した日次水準
    trend_at: dict[str, float]       # トレンド値
    seasonal_raw: dict[str, float]   # 縮小前の残差
    cvr_at: dict[str, float]         # 実績の転換率
    cvr_trend_at: dict[str, float]   # 転換率のトレンド


def _theil_sen_trend(keys: list[str], values: dict[str, float]) -> dict[str, float]:
    """対数トレンドを Theil–Sen で推定し、各月の水準を返す."""
    xs = [float(i) for i in range(len(keys))]
    ys = [math.log(values[k]) for k in keys]
    slope = theil_sen_slope(xs, ys)
    intercept = statistics.median([y - slope * x for x, y in zip(xs, ys)])
    return {k: math.exp(intercept + slope * x) for k, x in zip(keys, xs)}


def calibrate_monthly(
    rows: list[MonthlyRow],
    prior: BehaviorParams,
    shrinkage: float = DEFAULT_SHRINKAGE,
    today: date | None = None,
    average_market_factor: float = 1.0,
    average_share_factor: float = 1.0,
) -> MonthlyCalibration:
    """月次実績から base_sessions・base_cvr・月次季節係数・推奨aovを推定する.

    セッション列があれば「集客」と「転換率」を別々に推定する。
    この2つは実績で別々に動いており(セッション横ばい、CVR上昇)、
    1本にまとめるとトレンド推定を誤る。

    average_market_factor は二重計上を防ぐための補正。
    月次のセッション数には、その月の販促イベントによる上振れがすでに
    含まれている。一方モデルは base_sessions に市場規模係数を掛けて
    イベント日の集客を作る。補正しないと上振れを二度乗せることになる。
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
                f"最終月({last.key})が当月ですが日数が不明です。"
                "月全体とみなすため水準を過小評価します"
            )

    has_sessions = all(r.sessions for r in rows)
    basis = "sessions" if has_sessions else "orders"
    if not has_sessions:
        notes.append(
            "セッション列がないため、集客と転換率を分離できませんでした。"
            "base_cvr は仮値のままで、水準はすべて base_sessions に吸収されています"
        )

    # 1. カレンダー構成(曜日・給料日サイクル)を除いた日次水準
    index: dict[str, float] = {}
    for r in rows:
        weight = _calendar_weight(r.covered_days(), prior)
        if weight <= 0:
            continue
        index[r.key] = (r.sessions if has_sessions else r.orders) / weight

    if len(index) < 2:
        raise ValueError("月次校正には最低2ヶ月の実績が必要です")

    keys = list(index)
    trend = _theil_sen_trend(keys, index)

    # 2. 残差を季節係数に。1ヶ月=観測1点なので1.0方向へ縮小する。
    seasonal_raw: dict[str, float] = {}
    month_factors = {f"{m:02d}": prior.month.get(f"{m:02d}", 1.0) for m in range(1, 13)}
    observed: dict[str, list[float]] = {}
    for k in keys:
        observed.setdefault(k.split("-")[1], []).append(index[k] / trend[k])
    for mm, residuals in observed.items():
        raw = statistics.median(residuals)
        seasonal_raw[mm] = round(raw, 3)
        month_factors[mm] = round(1.0 + shrinkage * (raw - 1.0), 3)

    missing = sorted(set(month_factors) - set(observed))
    if missing:
        notes.append(f"実績のない月は仮値のままです: {', '.join(missing)}")

    month_factors, scale = _normalize(month_factors)

    # 3. 水準。販促イベントの上振れを差し引いて定常施策だけの日に引き戻す。
    level = trend[keys[-1]] * scale
    if average_market_factor and average_market_factor > 0:
        level /= average_market_factor
        if abs(average_market_factor - 1.0) > 0.01:
            notes.append(
                f"販促イベントによる上振れ(平均{average_market_factor:.3f}倍)を"
                "実績から差し引きました"
            )
    else:
        notes.append(
            "販促スケジュールが未指定のため、販促イベントの上振れが"
            "含まれたままです。ベースライン予測が過大になります"
            "(--schedule を指定してください)"
        )

    # 4. 転換率は別系列としてトレンドだけ取る
    cvr_at: dict[str, float] = {}
    cvr_trend_at: dict[str, float] = {}
    if has_sessions:
        for r in rows:
            cvr_at[r.key] = round(r.cvr, 5)
        cvr_trend_at = _theil_sen_trend(list(cvr_at), cvr_at)
        base_cvr = cvr_trend_at[keys[-1]]
        # 実績の転換率には、プロモーションパッケージ加入で開く施策による
        # 上振れがすでに含まれている。モデルはこれをシェア係数で作るため、
        # 補正しないと同じ効果を二度乗せることになる(集客側と同じ構造)。
        if average_share_factor and average_share_factor > 0:
            base_cvr /= average_share_factor
            if abs(average_share_factor - 1.0) > 0.01:
                notes.append(
                    f"参加資格つき施策による転換率の上振れ"
                    f"(平均{average_share_factor:.3f}倍)を実績から差し引きました"
                )
        base_sessions = level
    else:
        base_cvr = prior.base_cvr
        base_sessions = level / base_cvr if base_cvr > 0 else level

    # 5. 推奨 aov は直近3ヶ月の中央値(直近の価格帯を反映)
    recent = [r for r in rows if r.gmv > 0][-3:]
    suggested_aov = statistics.median([r.aov for r in recent]) if recent else 0.0
    if not recent:
        notes.append("売上列が空のため aov を推定できませんでした")

    span = f"{rows[0].key}〜{rows[-1].key}"
    basis_ja = "セッションと転換率を分離して" if has_sessions else "注文数ベースで"
    note = (
        f"月次実績{len(rows)}ヶ月({span})を{basis_ja}校正。"
        "base_sessions・base_cvr・月次季節係数のみ実測。"
        "曜日係数・給料日サイクル係数・市場規模とシェアの弾力性・aov_sigma は未校正(仮値)"
    )
    if notes:
        note += " / " + " / ".join(notes)

    params = replace(
        prior,
        base_sessions=round(base_sessions, 2),
        base_cvr=round(base_cvr, 5),
        month=month_factors,
        calibration_note=note,
    )
    return MonthlyCalibration(
        params=params,
        suggested_aov=round(suggested_aov, 1),
        notes=notes,
        basis=basis,
        monthly_index={k: round(v, 3) for k, v in index.items()},
        trend_at={k: round(v, 3) for k, v in trend.items()},
        seasonal_raw=seasonal_raw,
        cvr_at=cvr_at,
        cvr_trend_at={k: round(v, 5) for k, v in cvr_trend_at.items()},
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
        base_sessions=round(base / prior.base_cvr, 2) if prior.base_cvr > 0 else base,
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
