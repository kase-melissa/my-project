"""該当月の販促スケジュール(事前共有)を読み込む."""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import load_yaml
from .models import (
    CAP_PERIODS,
    ELIGIBILITIES,
    ELIGIBILITY_ALL,
    ELIGIBILITY_BSPLUS,
    ELIGIBILITY_BSPLUS_EXCELLENT,
    FUNDING_MALL,
    FUNDINGS,
    Benefit,
    PromoSchedule,
    ScheduleNote,
)

_ALLOWED_KEYS = {
    "id", "name", "days", "period", "rate", "coupon_yen", "funding", "eligibility",
    "min_order_yen", "user_cap_yen", "user_cap_period", "tiers", "traffic_multiplier",
    "entry_unit", "entry_deadline", "entry_deadline_days_before", "note",
}


def _as_date(value: Any, field: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value.strip())
    raise ValueError(f"{field} は日付として解釈できません: {value!r}")


def _expand_days(
    raw: dict[str, Any], name: str, first: date, last: date
) -> tuple[date, ...]:
    """days と period を日付列に展開する.

    days は次の3形式を受ける:
      all            … 対象期間の全日
      [1, 5, 15]     … 対象月の日(整数)
      ["2026-10-05"] … ISO日付(月跨ぎ施策で使う)
    """
    days: list[date] = []

    if "days" in raw:
        value = raw["days"]
        if isinstance(value, str):
            if value.strip() != "all":
                raise ValueError(
                    f"施策'{name}'の days に文字列を使えるのは 'all' のみです: {value!r}"
                )
            cur = first
            while cur <= last:
                days.append(cur)
                cur += timedelta(days=1)
        elif isinstance(value, list):
            for v in value:
                if isinstance(v, bool):
                    raise ValueError(f"施策'{name}'の days に真偽値は使えません")
                if isinstance(v, int):
                    try:
                        days.append(date(first.year, first.month, v))
                    except ValueError as exc:
                        raise ValueError(
                            f"施策'{name}'の days に不正な日があります: {v}"
                        ) from exc
                else:
                    days.append(_as_date(v, f"{name}.days"))
        else:
            raise ValueError(f"施策'{name}'の days はリストか 'all' です")

    if "period" in raw:
        value = raw["period"]
        if not (isinstance(value, list) and len(value) == 2):
            raise ValueError(f"施策'{name}'の period は [開始日, 終了日] で指定してください")
        start = _as_date(value[0], f"{name}.period[0]")
        end = _as_date(value[1], f"{name}.period[1]")
        if end < start:
            raise ValueError(f"施策'{name}'の period が逆順です: {start} > {end}")
        cur = start
        while cur <= end:
            days.append(cur)
            cur += timedelta(days=1)

    if not days:
        raise ValueError(f"施策'{name}'に days も period もありません")
    return tuple(sorted(set(days)))


def _parse_tiers(raw: Any, name: str) -> tuple[tuple[float, float], ...]:
    """買い回り型の段階付与を読む: [{min_total: 5000, rate: 0.04}, ...]"""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"施策'{name}'の tiers はリストで指定してください")
    out: list[tuple[float, float]] = []
    for item in raw:
        if not isinstance(item, dict) or "min_total" not in item or "rate" not in item:
            raise ValueError(
                f"施策'{name}'の tiers の要素は {{min_total: 金額, rate: 率}} です"
            )
        out.append((float(item["min_total"]), float(item["rate"])))
    return tuple(sorted(out))


def _parse_benefit(
    raw: dict[str, Any], default_lead: int, first: date, last: date
) -> Benefit:
    name = str(raw.get("name") or raw.get("id") or "")
    if not name:
        raise ValueError("施策に name(または id)がありません")

    unknown = set(raw) - _ALLOWED_KEYS
    if unknown:
        raise ValueError(f"施策'{name}'に未知のキーがあります: {sorted(unknown)}")

    days = _expand_days(raw, name, first, last)

    entry_unit = str(raw.get("entry_unit", "day"))
    if entry_unit not in ("day", "period"):
        raise ValueError(f"施策'{name}'の entry_unit は day か period です: {entry_unit}")

    funding = str(raw.get("funding", FUNDING_MALL))
    if funding not in FUNDINGS:
        raise ValueError(f"施策'{name}'の funding は {FUNDINGS} のいずれかです: {funding}")

    eligibility = str(raw.get("eligibility", ELIGIBILITY_ALL))
    if eligibility not in ELIGIBILITIES:
        raise ValueError(
            f"施策'{name}'の eligibility は {ELIGIBILITIES} のいずれかです: {eligibility}"
        )

    cap_period = str(raw.get("user_cap_period", "day"))
    if cap_period not in CAP_PERIODS:
        raise ValueError(
            f"施策'{name}'の user_cap_period は {CAP_PERIODS} のいずれかです: {cap_period}"
        )

    # 締切が意味を持つのはボーナスストアPlusの参加申込のみ。
    # プロモーションパッケージは月単位の加入なので日ごとの締切を持たない。
    needs_entry = eligibility in (ELIGIBILITY_BSPLUS, ELIGIBILITY_BSPLUS_EXCELLENT)
    deadline: date | None = None
    if raw.get("entry_deadline") is not None:
        deadline = _as_date(raw["entry_deadline"], f"{name}.entry_deadline")
    elif raw.get("entry_deadline_days_before") is not None or needs_entry:
        lead = int(raw.get("entry_deadline_days_before", default_lead))
        deadline = days[0] - timedelta(days=lead)

    cap = raw.get("user_cap_yen")
    benefit = Benefit(
        id=str(raw.get("id") or name),
        name=name,
        days=days,
        rate=float(raw.get("rate", 0.0)),
        coupon_yen=float(raw.get("coupon_yen", 0.0)),
        funding=funding,
        eligibility=eligibility,
        min_order_yen=float(raw.get("min_order_yen", 0.0)),
        user_cap_yen=None if cap is None else float(cap),
        user_cap_period=cap_period,
        tiers=_parse_tiers(raw.get("tiers"), name),
        traffic_multiplier=float(raw.get("traffic_multiplier", 1.0)),
        entry_unit=entry_unit,
        entry_deadline=deadline,
        note=str(raw.get("note", "")),
    )
    _validate(benefit)
    return benefit


def _validate(b: Benefit) -> None:
    if b.traffic_multiplier <= 0:
        raise ValueError(f"施策'{b.name}'の traffic_multiplier は正の数です")
    if b.rate < 0:
        raise ValueError(f"施策'{b.name}'の rate は0以上です")
    if b.rate and b.tiers:
        raise ValueError(
            f"施策'{b.name}'は rate と tiers の両方を持っています。どちらか一方にしてください"
        )
    if not b.rate and not b.tiers and not b.coupon_yen:
        raise ValueError(
            f"施策'{b.name}'に rate も tiers も coupon_yen もありません。"
            "付与内容のない施策は需要に影響しないため、記載する場合は値を入れてください"
        )
    if b.user_cap_yen is not None and b.user_cap_yen <= 0:
        raise ValueError(f"施策'{b.name}'の user_cap_yen は正の数です")


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

    extends_to: date | None = None
    if raw.get("extends_to") is not None:
        extends_to = _as_date(raw["extends_to"], "extends_to")
        if extends_to < last:
            raise ValueError(
                f"extends_to は対象月の末日({last})以降である必要があります: {extends_to}"
            )

    planning_last = extends_to or last
    default_lead = int(raw.get("entry_deadline_days_before", 3))

    items = raw.get("benefits")
    if items is None:
        raise ValueError(
            "スケジュールに benefits がありません"
            "(v1の events 形式は廃止されました。docs/model.md を参照してください)"
        )
    if not isinstance(items, list):
        raise ValueError("benefits はリストで指定してください")

    benefits: list[Benefit] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"benefits の要素はマッピングです: {item!r}")
        b = _parse_benefit(item, default_lead, first, planning_last)
        if b.id in seen:
            raise ValueError(f"施策idが重複しています: {b.id}")
        seen.add(b.id)
        outside = [d for d in b.days if not (first <= d <= planning_last)]
        if outside:
            raise ValueError(
                f"施策'{b.name}'に計画対象期間({first}〜{planning_last})外の日付があります: "
                f"{[d.isoformat() for d in outside]}。"
                "月を跨ぐ場合は extends_to を設定してください"
            )
        benefits.append(b)

    return PromoSchedule(
        month=month,
        first_day=first,
        last_day=last,
        benefits=tuple(sorted(benefits, key=lambda b: (b.days[0], b.id))),
        extends_to=extends_to,
        source_note=str(raw.get("source_note", "")),
        notes=_parse_notes(raw.get("notes"), first, planning_last),
    )


def _parse_notes(raw: Any, first: date, last: date) -> tuple[ScheduleNote, ...]:
    """付与率で表せない施策・運用上の注意を読む(くじ、訴求NG など)."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("notes はリストで指定してください")
    out: list[ScheduleNote] = []
    for item in raw:
        if not isinstance(item, dict) or "text" not in item:
            raise ValueError("notes の要素は {days: [...], text: \"...\"} です")
        days: tuple[date, ...] = ()
        if item.get("days"):
            days = _expand_days({"days": item["days"]}, "notes", first, last)
        out.append(ScheduleNote(text=str(item["text"]), days=days))
    return tuple(out)
