#!/usr/bin/env python3
"""日別実績から行動パラメータを推定する.

日別エクスポート(セッション列つき)と参加履歴があれば、
これまで仮値だった係数が実測できる。

    python3 tools/analyze_daily.py \
        --daily data/sales_history_daily.csv \
        --participation data/bsplus_participation.csv

推定するもの:

    曜日係数・給料日サイクル係数
    market_elasticity  … 全ストア共通の付与率 → セッション
    intent_elasticity  … 全ストア共通の付与率 → 転換率
    share_gain_at_reference … 自社だけの上乗せ → 転換率

## 交絡の扱い

素朴に「参加日 vs 非参加日」を比べてはいけない。参加日は
モール販促日を狙って選ばれているため、参加していなくても
売れた日である。実測でも参加日はセッションが4割多い。

そこで曜日・線形トレンド・5のつく日・ゾロ目を同時に入れて回帰する。
5のつく日(5/15/25)と参加日はこの期間で1日も重ならないため、
5のつく日の効果はきれいに識別できる。

## 縮小推定

日別は3ヶ月しかなく、曜日ごと13日・月内ビンごと15〜18日しかない。
点推定をそのまま採ると、たまたま参加日や5のつく日が偏った曜日で
大きく外れる(実測の素の土曜は4日しかない)。
そこで対数空間で仮値と幾何平均を取る(既存の月次校正と同じ方針)。
"""

from __future__ import annotations

import argparse
import csv
import io
import math
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bonus_planner.calendar_rules import dom_bucket, weekday_key  # noqa: E402
from bonus_planner.config import BehaviorParams  # noqa: E402
from bonus_planner.participation import _ols, load_participation  # noqa: E402

DOW = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
BUCKETS = ("d01_05", "d06_10", "d11_15", "d16_20", "d21_24", "d25_end")

# 縮小の強さ。0で仮値のまま、1で点推定そのまま、0.5で幾何平均。
SHRINKAGE = 0.5
# t値がこれ未満の係数は信用しない(仮値を据え置く)
MIN_T = 2.0


@dataclass
class Day:
    day: date
    orders: float
    sessions: float
    gmv: float

    @property
    def cvr(self) -> float:
        return self.orders / self.sessions


def load_daily(path: str | Path) -> list[Day]:
    """Yahoo!ショッピングの日別エクスポートを読む(CP932)."""
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "cp932"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError(f"文字コードを判別できませんでした: {path}")

    rows = list(csv.reader(io.StringIO(text)))
    header = [c.strip() for c in rows[0]]
    idx = {
        "date": header.index("日付"),
        "gmv": header.index("売上合計値"),
        "orders": header.index("注文数 - 注文数合計"),
        "sessions": header.index("セッション合計"),
    }
    out: list[Day] = []
    for r in rows[1:]:
        if not r or not r[idx["date"]].strip():
            continue
        y, m, d = (int(x) for x in r[idx["date"]].split("/"))
        sessions = float(r[idx["sessions"]] or 0)
        orders = float(r[idx["orders"]] or 0)
        if sessions <= 0 or orders <= 0:
            continue
        out.append(Day(date(y, m, d), orders, sessions, float(r[idx["gmv"]] or 0)))
    out.sort(key=lambda x: x.day)
    return out


# --------------------------------------------------------------------------
# 回帰
# --------------------------------------------------------------------------
def design(days: list[Day], participation: dict[date, float]) -> tuple[list[list[float]], list[str]]:
    """説明変数行列と列名を作る.

    基準は「月曜・月内1〜5日・5のつく日でもゾロ目でもない・非参加」の日。
    """
    t0 = days[0].day.toordinal()
    names = ["切片", "トレンド"]
    names += [f"曜日:{w}" for w in DOW[1:]]
    names += [f"月内:{b}" for b in BUCKETS[1:]]
    names += ["5のつく日", "ゾロ目", "参加率"]
    X: list[list[float]] = []
    for r in days:
        d = r.day
        row = [1.0, (d.toordinal() - t0) / 30.0]
        row += [1.0 if weekday_key(d) == w else 0.0 for w in DOW[1:]]
        row += [1.0 if dom_bucket(d) == b else 0.0 for b in BUCKETS[1:]]
        row += [
            1.0 if d.day in (5, 15, 25) else 0.0,
            1.0 if d.day in (11, 22) else 0.0,
            participation.get(d, 0.0),
        ]
        X.append(row)
    return X, names


def fit(days, participation, value):
    X, names = design(days, participation)
    y = [math.log(value(r)) for r in days]
    beta, se = _ols(X, y)
    return {n: (b, s, (b / s if s > 0 else 0.0)) for n, b, s in zip(names, beta, se)}


def _factors(coefs: dict, prefix: str, keys: tuple[str, ...]) -> dict[str, float]:
    """ダミー係数を平均1の係数に直す(基準カテゴリは1.0)."""
    raw = {keys[0]: 1.0}
    for k in keys[1:]:
        raw[k] = math.exp(coefs[f"{prefix}:{k}"][0])
    mean = sum(raw.values()) / len(raw)
    return {k: v / mean for k, v in raw.items()}


def shrink(measured: dict[str, float], prior: dict[str, float], lam: float) -> dict[str, float]:
    """対数空間で仮値に寄せてから平均1に直す."""
    blended = {
        k: math.exp(lam * math.log(v) + (1 - lam) * math.log(prior.get(k, 1.0)))
        for k, v in measured.items()
    }
    mean = sum(blended.values()) / len(blended)
    return {k: round(v / mean, 3) for k, v in blended.items()}


def main() -> int:
    ap = argparse.ArgumentParser(description="日別実績から行動パラメータを推定する")
    ap.add_argument("--daily", required=True, help="日別エクスポートCSV")
    ap.add_argument("--participation", required=True, help="参加履歴CSV")
    ap.add_argument("--baseline-rate", type=float, default=0.07,
                    help="定常施策だけの日の全ストア共通付与率")
    ap.add_argument("--five-day-rate", type=float, default=0.11,
                    help="5のつく日の全ストア共通付与率")
    ap.add_argument("--shrinkage", type=float, default=SHRINKAGE,
                    help="縮小の強さ(0=仮値のまま, 1=点推定そのまま)")
    args = ap.parse_args()

    days = load_daily(args.daily)
    part = load_participation(args.participation)
    prior = BehaviorParams()

    n_part = sum(1 for r in days if part.get(r.day, 0) > 0)
    print(f"期間 {days[0].day} 〜 {days[-1].day}（{len(days)}日）")
    print(f"参加 {n_part}日 / 非参加 {len(days) - n_part}日")

    ses = fit(days, part, lambda r: r.sessions)
    cvr = fit(days, part, lambda r: r.cvr)
    ordr = fit(days, part, lambda r: r.orders)

    print(f"\nトレンド  セッション {math.exp(ses['トレンド'][0]) - 1:+.1%}/月"
          f"   転換率 {math.exp(cvr['トレンド'][0]) - 1:+.1%}/月")

    # -- 曜日・月内 ------------------------------------------------------
    # モデルは曜日係数をセッション側に1つしか持たないため、
    # 「注文数への効果」をそこに入れる(注文数 = セッション x 転換率)。
    print("\n## 曜日係数（注文数ベース）")
    dow_m = _factors(ordr, "曜日", DOW)
    dow_s = shrink(dow_m, prior.dow, args.shrinkage)
    print("  実測 " + " ".join(f"{w}={dow_m[w]:.3f}" for w in DOW))
    print("  仮値 " + " ".join(f"{w}={prior.dow[w]:.3f}" for w in DOW))
    print("  採用 " + " ".join(f"{w}={dow_s[w]:.3f}" for w in DOW))

    print("\n## 給料日サイクル係数（注文数ベース・5のつく日を制御）")
    dom_m = _factors(ordr, "月内", BUCKETS)
    dom_s = shrink(dom_m, prior.dom, args.shrinkage)
    print("  実測 " + " ".join(f"{b}={dom_m[b]:.3f}" for b in BUCKETS))
    print("  仮値 " + " ".join(f"{b}={prior.dom[b]:.3f}" for b in BUCKETS))
    print("  採用 " + " ".join(f"{b}={dom_s[b]:.3f}" for b in BUCKETS))

    # -- 5のつく日 -------------------------------------------------------
    ln_ratio = math.log(args.five_day_rate / args.baseline_rate)
    print(f"\n## 5のつく日（全ストア共通 {args.baseline_rate:.0%} → {args.five_day_rate:.0%}）")
    for label, coefs, name in (
        ("セッション → market_elasticity", ses, "market_elasticity"),
        ("転換率   → intent_elasticity", cvr, "intent_elasticity"),
    ):
        b, s, t = coefs["5のつく日"]
        est, est_se = b / ln_ratio, s / ln_ratio
        ok = "識別できている" if abs(t) >= MIN_T else f"t={t:.2f} で0と区別できない"
        print(f"  {label:32s} 効果 {math.exp(b) - 1:+6.1%} (t={t:5.2f})"
              f"  {name} = {est:.2f} ± {est_se:.2f}   {ok}")
    b, s, t = ordr["5のつく日"]
    print(f"  {'注文数（= 上の2つの積）':32s} 効果 {math.exp(b) - 1:+6.1%} (t={t:5.2f})")

    # -- 参加効果 --------------------------------------------------------
    rates = [v for d, v in part.items() if any(r.day == d for r in days) and v > 0]
    avg_rate = sum(rates) / len(rates) if rates else 0.0
    b, s, t = cvr["参加率"]
    lift = math.exp(b * avg_rate) - 1
    print(f"\n## 自社の上乗せ（参加日の平均設定率 {avg_rate:.2%}）")
    print(f"  転換率への効果 {lift:+.1%} (t={t:.2f})")
    for extra, note in ((0.0, "自社分のみ"), (0.02, "モール負担+2%も開いていた場合")):
        adv = avg_rate + extra
        resp = (adv / prior.reference_advantage) ** prior.share_elasticity
        print(f"    優位 {adv:.2%}（{note}）→ share_gain_at_reference = {lift / resp:.3f}")
    bs, ss, ts = ses["参加率"]
    print(f"  ※ セッションへの見かけの効果 {math.exp(bs * avg_rate) - 1:+.1%} (t={ts:.2f})")
    print("     エントリー自体が来訪を増やすことはない。観測できていないモール販促"
          "（プレミアムな日曜日・感謝デー等）を狙って参加した分が乗っている。"
          "\n     したがって上の転換率への効果も上振れ側に見ておくこと。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
