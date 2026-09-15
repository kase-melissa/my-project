"""ドメインモデル定義."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Iterable


# --------------------------------------------------------------------------
# 販促スケジュール
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class PromoEvent:
    """モール側の販促イベント1件.

    traffic_multiplier と entry_uplift の役割分担がこのシステムの肝。

    traffic_multiplier
        エントリー有無に関わらず得られるモール全体の需要増。
        (イベント告知によるモール来訪増 → 自社商品にも流入する分)
    entry_uplift
        エントリーして初めて得られる上乗せ。
        ボーナスストア特集面への掲載、「ボーナスストア対象」絞り込み検索での
        露出、還元率表示によるCVR改善の合計。
        参照還元率(reference_rate)でこの値、還元率が変われば逓減反応で調整。

    「プレミアムな日曜日」「感謝デー」「超PayPay祭」「爆買いWEEK」のように
    エントリーが参加条件になっているイベントは entry_required=True とし、
    エントリーしない場合は entry_uplift 分がまるごと機会損失になる。
    """

    id: str
    name: str
    dates: tuple[date, ...]
    entry_required: bool = True
    entry_unit: str = "day"  # "day" = 日単位 / "period" = 期間一括エントリー
    entry_deadline: date | None = None
    traffic_multiplier: float = 1.0
    entry_uplift: float = 0.0
    aov_multiplier: float = 1.0
    min_bonus_rate: float = 0.0
    allowed_rates: tuple[float, ...] = ()
    priority: int = 0  # 表示順・同点時の優先度(大きいほど重要)
    note: str = ""

    def covers(self, day: date) -> bool:
        return day in self.dates


@dataclass(frozen=True)
class PromoSchedule:
    """該当月の販促スケジュール全体."""

    month: str  # "YYYY-MM"
    first_day: date
    last_day: date
    events: tuple[PromoEvent, ...]
    source_note: str = ""

    def days(self) -> list[date]:
        out, d = [], self.first_day
        while d <= self.last_day:
            out.append(d)
            d = date.fromordinal(d.toordinal() + 1)
        return out

    def events_on(self, day: date) -> list[PromoEvent]:
        return [e for e in self.events if e.covers(day)]


# --------------------------------------------------------------------------
# 1日の評価結果
# --------------------------------------------------------------------------
@dataclass
class DayContext:
    """需要モデルに渡す1日分の文脈."""

    day: date
    events: list[PromoEvent] = field(default_factory=list)
    is_five_day: bool = False       # 5のつく日
    is_zorome: bool = False         # ゾロ目の日
    is_payday_window: bool = False  # 給料日直後
    weekday: int = 0                # 0=月 .. 6=日

    @property
    def event_names(self) -> list[str]:
        return [e.name for e in self.events]


@dataclass
class DayEstimate:
    """ある日・ある還元率での見積り.

    「エントリーしない場合」を基準線(base)とし、その差分で評価する。
    ポイント原資は自然発生分の注文にも等しく乗るため、コストは
    エントリー時の全GMVにかかる点に注意(=カニバリを織り込む)。
    """

    day: date
    bonus_rate: float
    base_orders: float
    base_gmv: float
    entry_orders: float
    entry_gmv: float
    effective_rate: float  # ポイント上限適用後の実効還元率
    point_cost: float
    incremental_gmv: float
    incremental_orders: float
    gross_profit_delta: float  # 粗利増 - ポイント原資(LTV除く)
    ltv_value: float
    net_value: float  # gross_profit_delta + ltv_value
    roas: float
    uplift_ratio: float

    @property
    def is_profitable(self) -> bool:
        return self.net_value > 0


@dataclass
class EntryOption:
    """エントリー単位に対する1つの選択肢(=還元率)."""

    bonus_rate: float
    cost: float
    value: float
    incremental_gmv: float
    estimates: list[DayEstimate]

    @property
    def roas(self) -> float:
        return self.incremental_gmv / self.cost if self.cost > 0 else 0.0


@dataclass
class EntryUnit:
    """エントリー申込の最小単位.

    日単位イベント/平常日は1日=1ユニット。
    超PayPay祭のような期間一括エントリーは期間全体で1ユニット(all-or-nothing)。
    """

    key: str
    label: str
    days: list[date]
    options: list[EntryOption]
    entry_deadline: date | None = None
    entry_required_events: list[str] = field(default_factory=list)
    mandatory: bool = False  # 経営判断で必ずエントリーする(予算から先取り)
    blocked_reason: str | None = None  # 締切超過などで選択不可

    @property
    def selectable(self) -> bool:
        return self.blocked_reason is None and bool(self.options)


@dataclass
class PlanResult:
    """最終的な提案."""

    month: str
    generated_for: date
    selected: list[tuple[EntryUnit, EntryOption]]
    rejected: list[tuple[EntryUnit, EntryOption, str]]
    blocked: list[EntryUnit]
    budget: float
    total_cost: float
    total_incremental_gmv: float
    total_value: float
    baseline_gmv: float

    @property
    def total_roas(self) -> float:
        return self.total_incremental_gmv / self.total_cost if self.total_cost > 0 else 0.0

    @property
    def budget_used_ratio(self) -> float:
        return self.total_cost / self.budget if self.budget > 0 else 0.0

    def selected_days(self) -> set[date]:
        out: set[date] = set()
        for unit, _ in self.selected:
            out.update(unit.days)
        return out

    def rate_for(self, day: date) -> float | None:
        for unit, opt in self.selected:
            if day in unit.days:
                return opt.bonus_rate
        return None


def dedupe_events(events: Iterable[PromoEvent]) -> list[PromoEvent]:
    seen, out = set(), []
    for e in events:
        if e.id not in seen:
            seen.add(e.id)
            out.append(e)
    return out
