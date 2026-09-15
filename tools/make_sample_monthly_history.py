#!/usr/bin/env python3
"""動作確認用の月次実績サンプルを生成する(合成データ).

実データではない。月次校正が既知の正解値をどれだけ復元できるかを
確認するためのもので、生成時の正解値を標準エラーに表示する。

    python3 tools/make_sample_monthly_history.py > data/sales_history_monthly_sample.csv
"""

from __future__ import annotations

import csv
import math
import random
import sys

sys.path.insert(0, ".")
from bonus_planner import calendar_rules as cal  # noqa: E402
from bonus_planner.calibrate import MonthlyRow  # noqa: E402
from bonus_planner.config import BehaviorParams  # noqa: E402

TRUE_DAILY_INDEX = 41.0      # 暦要因を除いた1日あたり注文数
TRUE_MONTHLY_GROWTH = 1.012  # 月次の成長トレンド
TRUE_SEASON = {
    1: 0.95, 2: 0.90, 3: 1.10, 4: 1.06, 5: 0.96, 6: 0.90,
    7: 1.14, 8: 1.10, 9: 1.00, 10: 1.06, 11: 0.96, 12: 1.20,
}
TRUE_AOV = 19800.0
NOISE = 0.06
PARTIAL_LAST_DAYS = 14  # 2026-09 は月途中まで


def main() -> None:
    rng = random.Random(20260915)
    prior = BehaviorParams()
    w = csv.writer(sys.stdout)
    w.writerow(["month", "orders", "gmv", "days"])

    y, m = 2025, 9
    for i in range(13):
        days = PARTIAL_LAST_DAYS if i == 12 else None
        row = MonthlyRow(y, m, 1, 1, days)
        weight = sum(
            prior.dow[cal.weekday_key(d)] * prior.dom[cal.dom_bucket(d)]
            for d in row.covered_days()
        )
        level = TRUE_DAILY_INDEX * (TRUE_MONTHLY_GROWTH ** i) * TRUE_SEASON[m]
        orders = level * weight * math.exp(rng.gauss(0, NOISE))
        aov = TRUE_AOV * math.exp(rng.gauss(0, 0.03))
        w.writerow([f"{y}-{m:02d}", round(orders), round(orders * aov), days or ""])
        m += 1
        if m > 12:
            m, y = 1, y + 1

    print(
        f"# 正解値: daily_index={TRUE_DAILY_INDEX} growth={TRUE_MONTHLY_GROWTH} "
        f"aov={TRUE_AOV} season={TRUE_SEASON}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
