"""該当月の販促スケジュール(事前共有)を読み込む."""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import load_yaml
from .models import PromoEvent, PromoSchedule

_ALLOWED_EVENT_KEYS = {
    "id", "name", "dates", "period", "entry_required", "entry_unit",
    "entry_deadline", "entry_deadline_days_before", "traffic_multiplier",
    "entry_uplift", "aov_multiplier", "min_bonus_rate", "allowed_rates",
    "priority", "note",
}


def _as_date(value: Any, field: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value.strip())
    raise ValueError(f"{field} は日付として解釈できません: {value!r}")


def _expand_dates(raw: dict[str, Any], name: str) -> tuple[date, ...]:
    """dates(列挙) と period(開始-終了) の両方を受け付ける."""
    days: list[date] = []
    if "dates" in raw:
        value = raw["dates"]
        if not isinstance(value, list):
            raise ValueError(f"イベント'{name}'の dates はリストで指定してください")
        days.extend(_as_date(v, f"{name}.dates") for v in value)
    if "period" in raw:
        value = raw["period"]
        if not (isinstance(value, list) and len(value) == 2):
            raise ValueError(f"イベント'{name}'の period は [開始日, 終了日] で指定してください")
        start = _as_date(value[0], f"{name}.period[0]")
        end = _as_date(value[1], f"{name}.period[1]")
        if end < start:
            raise ValueError(f"イベント'{name}'の period が逆順です: {start} > {end}")
        cur = start
        while cur <= end:
            days.append(cur)
            cur += timedelta(days=1)
    if not days:
        raise ValueError(f"イベント'{name}'に dates も period もありません")
    return tuple(sorted(set(days)))


def _parse_event(raw: dict[str, Any], default_deadline_days: int) -> PromoEvent:
    name = str(raw.get("name") or raw.get("id") or "")
    if not name:
        raise ValueError("イベントに name(または id)がありません")

    unknown = set(raw) - _ALLOWED_EVENT_KEYS
    if unknown:
        raise ValueError(f"イベント'{name}'に未知のキーがあります: {sorted(unknown)}")

    days = _expand_dates(raw, name)
    entry_unit = str(raw.get("entry_unit", "day"))
    if entry_unit not in ("day", "period"):
        raise ValueError(f"イベント'{name}'の entry_unit は day か period です: {entry_unit}")

    deadline: date | None = None
    if raw.get("entry_deadline") is not None:
        deadline = _as_date(raw["entry_deadline"], f"{name}.entry_deadline")
    else:
        lead = int(raw.get("entry_deadline_days_before", default_deadline_days))
        deadline = days[0] - timedelta(days=lead)

    allowed = tuple(float(r) for r in raw.get("allowed_rates", ()) or ())
    event = PromoEvent(
        id=str(raw.get("id") or name),
        name=name,
        dates=days,
        entry_required=bool(raw.get("entry_required", True)),
        entry_unit=entry_unit,
        entry_deadline=deadline,
        traffic_multiplier=float(raw.get("traffic_multiplier", 1.0)),
        entry_uplift=float(raw.get("entry_uplift", 0.0)),
        aov_multiplier=float(raw.get("aov_multiplier", 1.0)),
        min_bonus_rate=float(raw.get("min_bonus_rate", 0.0)),
        allowed_rates=allowed,
        priority=int(raw.get("priority", 0)),
        note=str(raw.get("note", "")),
    )
    _validate_event(event)
    return event


def _validate_event(e: PromoEvent) -> None:
    if e.traffic_multiplier <= 0:
        raise ValueError(f"イベント'{e.name}'の traffic_multiplier は正の数です")
    if e.entry_uplift < 0:
        raise ValueError(f"イベント'{e.name}'の entry_uplift は0以上です")
    if e.allowed_rates and e.min_bonus_rate > max(e.allowed_rates):
        raise ValueError(
            f"イベント'{e.name}'の min_bonus_rate({e.min_bonus_rate})が "
            f"allowed_rates の最大値を超えています"
        )
    if e.entry_required and e.entry_uplift == 0:
        raise ValueError(
            f"イベント'{e.name}'はエントリー必須ですが entry_uplift が0です。"
            "エントリーの価値が0と評価されてしまうため設定を見直してください"
        )


def load_schedule(path: str | Path) -> PromoSchedule:
    raw = load_yaml(path)
    month = str(raw.get("month", "")).strip()
    if not month:
        raise ValueError("スケジュールに month(YYYY-MM)がありません")
    try:
        year, mon = (int(x) for x in month.split("-"))
        first = date(year, mon, 1)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"month は YYYY-MM 形式で指定してください: {month!r}") from exc
    last = date(year, mon, calendar.monthrange(year, mon)[1])

    default_lead = int(raw.get("entry_deadline_days_before", 3))
    events_raw = raw.get("events") or []
    if not isinstance(events_raw, list):
        raise ValueError("events はリストで指定してください")

    events: list[PromoEvent] = []
    seen_ids: set[str] = set()
    for item in events_raw:
        if not isinstance(item, dict):
            raise ValueError(f"events の要素はマッピングです: {item!r}")
        ev = _parse_event(item, default_lead)
        if ev.id in seen_ids:
            raise ValueError(f"イベントidが重複しています: {ev.id}")
        seen_ids.add(ev.id)
        outside = [d for d in ev.dates if not (first <= d <= last)]
        if outside:
            raise ValueError(
                f"イベント'{ev.name}'に対象月({month})外の日付があります: "
                f"{[d.isoformat() for d in outside]}"
            )
        events.append(ev)

    return PromoSchedule(
        month=month,
        first_day=first,
        last_day=last,
        events=tuple(sorted(events, key=lambda e: (e.dates[0], -e.priority))),
        source_note=str(raw.get("source_note", "")),
    )
