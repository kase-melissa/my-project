#!/usr/bin/env python3
"""動作確認用の受注実績サンプルを生成する(合成データ).

実データではない。`calibrate` が既知の正解値をどれだけ復元できるかを
確認するためのもので、生成時の正解値を末尾に表示する。

    python3 tools/make_sample_history.py > data/sales_history_sample.csv
"""

from __future__ import annotations

import csv
import math
import random
import sys
from datetime import date, timedelta

sys.path.insert(0, ".")
from bonus_planner import calendar_rules as cal  # noqa: E402

# ---- 正解値(これを calibrate が復元できるか確認する) ----------------
TRUE_BASE = 45.0
TRUE_DOW = {"mon": 0.93, "tue": 0.90, "wed": 0.94, "thu": 0.96,
            "fri": 1.02, "sat": 1.10, "sun": 1.22}
TRUE_DOM = {"d01_05": 1.06, "d06_10": 0.92, "d11_15": 0.95,
            "d16_20": 0.90, "d21_24": 0.95, "d25_end": 1.18}
TRUE_MONTH = {3: 1.08, 4: 1.05, 7: 1.12, 8: 1.10, 9: 1.05, 10: 1.06, 12: 1.15}
TRUE_FIVE_TRAFFIC = 1.52
TRUE_FIVE_UPLIFT = 0.40
TRUE_ZOROME_TRAFFIC = 1.09
TRUE_NORMAL_UPLIFT = 0.11
TRUE_EVENTS = {
    "プレミアムな日曜日": (1.34, 0.36),
    "Yahoo!ショッピング感謝デー": (1.38, 0.34),
    "超PayPay祭": (1.58, 0.52),
    "爆買いWEEK": (1.28, 0.28),
}
REFERENCE_RATE = 0.04
ELASTICITY = 0.55
DECAY = 0.55
AOV = 19800.0
NOISE_SIGMA = 0.13


def combine(deltas: list[float]) -> float:
    return sum(d * (DECAY ** i) for i, d in enumerate(sorted(deltas, reverse=True)))


def events_for(day: date) -> list[str]:
    """モール販促イベントの発生パターン(サンプル用の単純な規則)."""
    out = []
    if day.weekday() == 6 and day.day <= 7:
        out.append("プレミアムな日曜日")
    if day.day == 8:
        out.append("Yahoo!ショッピング感謝デー")
    if day.month in (3, 6, 9, 12) and 20 <= day.day <= 25:
        out.append("超PayPay祭")
    if day.month % 2 == 0 and 12 <= day.day <= 16:
        out.append("爆買いWEEK")
    return out


def main() -> None:
    rng = random.Random(20260915)
    start, end = date(2025, 9, 1), date(2026, 9, 14)
    w = csv.writer(sys.stdout)
    w.writerow(["date", "orders", "gmv", "entered", "bonus_rate", "events"])

    day = start
    while day <= end:
        evs = events_for(day)
        traffic_deltas = [TRUE_EVENTS[e][0] - 1.0 for e in evs]
        uplift_deltas = [TRUE_EVENTS[e][1] for e in evs]
        if not evs:
            if cal.is_five_day(day):
                traffic_deltas.append(TRUE_FIVE_TRAFFIC - 1.0)
                uplift_deltas.append(TRUE_FIVE_UPLIFT)
            if cal.is_zorome(day):
                traffic_deltas.append(TRUE_ZOROME_TRAFFIC - 1.0)

        expected = (
            TRUE_BASE
            * TRUE_DOW[cal.weekday_key(day)]
            * TRUE_DOM[cal.dom_bucket(day)]
            * TRUE_MONTH.get(day.month, 1.0)
            * (1.0 + combine(traffic_deltas))
        )

        # 実際の運用に近い形でエントリー有無を散らす(イベント日ほど入りやすい)
        p_entry = 0.75 if evs else (0.6 if cal.is_five_day(day) else 0.18)
        entered = rng.random() < p_entry
        rate = rng.choice([0.02, 0.03, 0.04, 0.05]) if entered else 0.0

        if entered:
            max_uplift = max(combine(uplift_deltas), TRUE_NORMAL_UPLIFT) if uplift_deltas else TRUE_NORMAL_UPLIFT
            expected *= 1.0 + max_uplift * (rate / REFERENCE_RATE) ** ELASTICITY

        orders = max(1, round(expected * math.exp(rng.gauss(0, NOISE_SIGMA))))
        gmv = round(orders * AOV * math.exp(rng.gauss(0, 0.05)))
        w.writerow([day.isoformat(), orders, gmv, int(entered),
                    f"{rate:.2f}" if entered else "", "|".join(evs)])
        day += timedelta(days=1)

    print(
        f"# 正解値: base={TRUE_BASE} five_traffic={TRUE_FIVE_TRAFFIC} "
        f"five_uplift={TRUE_FIVE_UPLIFT} normal_uplift={TRUE_NORMAL_UPLIFT}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
