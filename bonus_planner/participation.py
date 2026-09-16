"""ボーナスストアPlusの参加履歴.

「いつ・何%で参加したか」の記録は2つの役割を持つ。

1. **二重計上の補正**
   実績の転換率には、参加した日の上振れがすでに含まれている。
   モデルはそれをシェア係数で作るため、補正しないと二度乗せることになる。

2. **シェア反応の推定**
   参加日と非参加日を比べれば、付与率への反応を実測できる——はずだが、
   月次データでは参加開始が成長トレンドの立ち上がりと重なっていると
   分離できない。`estimate_share_response` はそれを判定して返す。
"""

from __future__ import annotations

import calendar as _calendar
import csv
import io
import math
import statistics
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# 参加率と時期の相関がこれを超えると、トレンドと分離できないとみなす
CONFOUNDING_THRESHOLD = 0.75
# t値がこれ未満なら、係数が0と区別できないとみなす
MIN_T_VALUE = 2.0


def load_participation(path: str | Path) -> dict[date, float]:
    """参加履歴CSVを読む.

    形式: `date,store_rate`
    日付は `YYYY-MM-DD` と `YYYY/M/D` の両方を受ける。
    `#` で始まる行はコメントとして読み飛ばす。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"参加履歴が見つかりません: {p}")

    raw = p.read_bytes()
    text = None
    for enc in ("utf-8-sig", "cp932"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError(f"文字コードを判別できませんでした: {p}")

    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        raise ValueError(f"参加履歴が空です: {p}")

    rows = list(csv.reader(io.StringIO("\n".join(lines))))
    header = [c.strip().lower() for c in rows[0]]
    if "date" not in header:
        raise ValueError(f"参加履歴に date 列がありません: {rows[0]}")
    d_idx = header.index("date")
    r_idx = header.index("store_rate") if "store_rate" in header else None

    out: dict[date, float] = {}
    for i, r in enumerate(rows[1:], start=2):
        if not r or d_idx >= len(r) or not r[d_idx].strip():
            continue
        token = r[d_idx].strip().replace("/", "-")
        try:
            y, m, d = (int(x) for x in token.split("-"))
            day = date(y, m, d)
        except ValueError as exc:
            raise ValueError(f"参加履歴の{i}行目の日付を解釈できません: {exc}") from exc
        rate = 0.0
        if r_idx is not None and r_idx < len(r) and r[r_idx].strip():
            try:
                rate = float(r[r_idx])
            except ValueError as exc:
                raise ValueError(f"参加履歴の{i}行目の還元率を解釈できません: {exc}") from exc
        if rate < 0:
            raise ValueError(f"参加履歴の{i}行目の還元率が負です: {rate}")
        out[day] = rate
    if not out:
        raise ValueError(f"参加履歴に有効な行がありません: {p}")
    return out


@dataclass
class MonthlyParticipation:
    year: int
    month: int
    days_covered: int
    entry_days: int
    average_rate: float  # 参加日の平均自社率(非参加日は含めない)

    @property
    def key(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"

    @property
    def share(self) -> float:
        """その月の日数のうち参加した割合."""
        return self.entry_days / self.days_covered if self.days_covered else 0.0


def monthly_summary(
    participation: dict[date, float], year: int, month: int, days: int | None = None
) -> MonthlyParticipation:
    """その月の参加状況をまとめる.

    days は部分月の実日数。省略時は月全体。
    記録の無い日は非参加として扱う。
    """
    n = days or _calendar.monthrange(year, month)[1]
    rates = [
        rate
        for day, rate in participation.items()
        if day.year == year and day.month == month and day.day <= n and rate > 0
    ]
    return MonthlyParticipation(
        year=year,
        month=month,
        days_covered=n,
        entry_days=len(rates),
        average_rate=statistics.mean(rates) if rates else 0.0,
    )


def daily_rates(
    participation: dict[date, float], year: int, month: int, days: int | None = None
) -> list[float]:
    """その月の各日の自社設定率. 非参加日は0."""
    n = days or _calendar.monthrange(year, month)[1]
    return [participation.get(date(year, month, i + 1), 0.0) for i in range(n)]


# --------------------------------------------------------------------------
# シェア反応の推定
# --------------------------------------------------------------------------
@dataclass
class ShareResponseEstimate:
    """月次データから推定したシェア反応."""

    months: int
    trend_per_month: float          # 参加率を入れたときの月次トレンド
    trend_without_control: float    # 参加率を入れないときの月次トレンド
    coefficient: float              # log(CVR) に対する参加率の係数
    std_error: float
    t_value: float
    correlation_with_time: float    # 参加率と時期の相関
    implied_share_gain: float | None  # share_gain_at_reference 換算
    identifiable: bool
    reason: str

    def summary_lines(self) -> list[str]:
        lines = [
            f"観測 {self.months}ヶ月",
            f"トレンド（参加率を入れない） {self.trend_without_control:+.2%}/月",
            f"トレンド（参加率を入れる）   {self.trend_per_month:+.2%}/月",
            f"参加率の係数 {self.coefficient:+.3f}（SE {self.std_error:.3f}, t={self.t_value:.2f}）",
            f"参加率と時期の相関 {self.correlation_with_time:.3f}",
        ]
        if self.implied_share_gain is not None:
            lines.append(
                f"share_gain_at_reference 換算 {self.implied_share_gain:.3f}"
            )
        lines.append(("推定可能" if self.identifiable else "推定不能") + f": {self.reason}")
        return lines


def _ols(X: list[list[float]], y: list[float]) -> tuple[list[float], list[float]]:
    """最小二乗法. 係数と標準誤差を返す(stdlibのみ)."""
    k, n = len(X[0]), len(y)
    if n <= k:
        raise ValueError("観測数が説明変数の数以下です")
    xtx = [[sum(X[i][a] * X[i][b] for i in range(n)) for b in range(k)] for a in range(k)]
    xty = [sum(X[i][a] * y[i] for i in range(n)) for a in range(k)]

    # ガウス・ジョルダンで逆行列と解を同時に求める
    aug = [row[:] + [1.0 if a == b else 0.0 for b in range(k)] + [xty[a]]
           for a, row in enumerate(xtx)]
    for c in range(k):
        piv = max(range(c, k), key=lambda r: abs(aug[r][c]))
        if abs(aug[piv][c]) < 1e-12:
            raise ValueError("説明変数が一次従属です")
        aug[c], aug[piv] = aug[piv], aug[c]
        d = aug[c][c]
        aug[c] = [v / d for v in aug[c]]
        for r in range(k):
            if r != c:
                f = aug[r][c]
                aug[r] = [aug[r][j] - f * aug[c][j] for j in range(2 * k + 1)]
    beta = [aug[a][2 * k] for a in range(k)]
    inv = [[aug[a][k + b] for b in range(k)] for a in range(k)]

    resid = [y[i] - sum(X[i][a] * beta[a] for a in range(k)) for i in range(n)]
    s2 = sum(e * e for e in resid) / (n - k)
    se = [math.sqrt(max(s2 * inv[a][a], 0.0)) for a in range(k)]
    return beta, se


def estimate_share_response(
    series: list[tuple[str, float, float, float]],
    reference_advantage: float = 0.02,
    share_elasticity: float = 0.70,
) -> ShareResponseEstimate:
    """月次の (キー, 転換率, 参加率, 参加日の平均自社率) からシェア反応を推定する.

    log(CVR) = a + b*t + c*参加率 を当てる。

    参加開始が成長トレンドの立ち上がりと重なっていると、b と c を分離できない。
    その場合は identifiable=False を返す。数値は出すが、鵜呑みにしてはいけない。
    """
    usable = [s for s in series if s[1] > 0]
    if len(usable) < 5:
        raise ValueError("シェア反応の推定には最低5ヶ月の実績が必要です")

    ts = [float(i) for i in range(len(usable))]
    ys = [math.log(s[1]) for s in usable]
    ps = [s[2] for s in usable]

    (a0, b0), _ = _ols([[1.0, t] for t in ts], ys)
    if len(set(ps)) == 1:
        return ShareResponseEstimate(
            months=len(usable), trend_per_month=math.exp(b0) - 1,
            trend_without_control=math.exp(b0) - 1, coefficient=0.0,
            std_error=0.0, t_value=0.0, correlation_with_time=0.0,
            implied_share_gain=None, identifiable=False,
            reason="参加率が全月で同じため、効果を切り出せない",
        )

    (a1, b1, c1), (_, _, se_c) = _ols([[1.0, t, p] for t, p in zip(ts, ps)], ys)
    corr = statistics.correlation(ts, ps) if len(set(ts)) > 1 else 0.0
    t_value = c1 / se_c if se_c > 0 else 0.0

    # 参加率100%・平均自社率で参加したときの注文増加率を、モデルの
    # share_gain_at_reference に換算する
    implied = None
    rates = [s[3] for s in usable if s[3] > 0]
    if rates:
        avg_rate = statistics.mean(rates)
        response = (avg_rate / reference_advantage) ** share_elasticity
        if response > 0:
            implied = max((math.exp(c1) - 1.0) / response, 0.0)

    if abs(corr) > CONFOUNDING_THRESHOLD:
        identifiable, reason = False, (
            f"参加率と時期の相関が {corr:.2f} と高く、成長トレンドと分離できない"
        )
    elif abs(t_value) < MIN_T_VALUE:
        identifiable, reason = False, (
            f"参加率の係数が t={t_value:.2f} で、0と区別できない"
        )
    else:
        identifiable, reason = True, "トレンドと分離できている"

    return ShareResponseEstimate(
        months=len(usable),
        trend_per_month=math.exp(b1) - 1,
        trend_without_control=math.exp(b0) - 1,
        coefficient=c1,
        std_error=se_c,
        t_value=t_value,
        correlation_with_time=corr,
        implied_share_gain=implied,
        identifiable=identifiable,
        reason=reason,
    )
