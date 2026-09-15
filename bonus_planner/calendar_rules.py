"""Yahoo!ショッピング顧客の行動に効く暦の規則.

モール公式の販促スケジュールに載らない「毎月必ず効く日付要因」をここで扱う。
販促スケジュール側で明示された場合はそちらが優先される。
"""

from __future__ import annotations

from datetime import date, timedelta

WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]


def is_five_day(day: date) -> bool:
    """5のつく日(5/15/25). PayPay還元が積み上がりモール全体の需要が跳ねる."""
    return day.day in (5, 15, 25)


def is_zorome(day: date) -> bool:
    """ゾロ目の日(11/22). クーポン施策が当たりやすい."""
    return day.day in (11, 22)


def is_payday_window(day: date, payday: int = 25, window: int = 6) -> bool:
    """給料日直後ウィンドウ.

    犬猫用家電は1万円超の高単価商材が中心で、給与支給後に決済が寄る。
    25日〜月末に加え、前月の給料日ウィンドウが月をまたいで食い込む分も含む。
    """
    if day.day >= payday:
        return True
    # 前月の給料日から window 日間が、前月の日数を超えて翌月に食い込む日数
    spill = payday + window - 1 - _days_in_previous_month(day)
    return 0 < day.day <= spill


def _days_in_previous_month(day: date) -> int:
    first_of_month = date(day.year, day.month, 1)
    last_of_prev = first_of_month - timedelta(days=1)
    return last_of_prev.day


def dom_bucket(day: date) -> str:
    """給料日サイクルのビン. キャリブレーション時の粒度と揃える."""
    d = day.day
    if d <= 5:
        return "d01_05"
    if d <= 10:
        return "d06_10"
    if d <= 15:
        return "d11_15"
    if d <= 20:
        return "d16_20"
    if d <= 24:
        return "d21_24"
    return "d25_end"


def weekday_key(day: date) -> str:
    return ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][day.weekday()]


def format_day(day: date) -> str:
    return f"{day.month}/{day.day}({WEEKDAY_JA[day.weekday()]})"
