"""ドメインモデル定義.

設計の中核は「顧客が受け取る付与率」と「自社が負担する原資」の分離。

    顧客体感の総付与率(d) = その日有効な全施策の率の合計   ← 需要のドライバー
    自社ポイント原資(d)   = funding=store の施策のみ × GMV  ← コスト

モール負担の施策は需要を押し上げるがコストにはならない。両者を混ぜると、
モール負担分まで自社原資として計上して原資が数倍に膨らむ。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable

# 参加資格。施策ごとに「どの条件を満たすストアが対象か」を表す。
ELIGIBILITY_ALL = "all"                     # 全ストア(エントリー不要)
ELIGIBILITY_PROMO_PACKAGE = "promo_package"  # プロモーションパッケージ加入ストア
ELIGIBILITY_BSPLUS = "bonus_store_plus"      # ボーナスストアPlus参加ストア
ELIGIBILITY_BSPLUS_EXCELLENT = "bonus_store_plus_excellent"  # 参加かつ優良ストア
ELIGIBILITIES = (
    ELIGIBILITY_ALL,
    ELIGIBILITY_PROMO_PACKAGE,
    ELIGIBILITY_BSPLUS,
    ELIGIBILITY_BSPLUS_EXCELLENT,
)

FUNDING_MALL = "mall"    # モール負担 — 需要は押し上げるが自社コストにならない
FUNDING_STORE = "store"  # 自社負担 — ポイント原資として計上する
FUNDINGS = (FUNDING_MALL, FUNDING_STORE)

CAP_PERIODS = ("day", "month", "period")


@dataclass(frozen=True)
class Participation:
    """ストアの参加状態.

    promo_package と excellent_store は月単位で決まる前提条件。
    bonus_store_plus だけが日ごとに選べる意思決定変数。
    """

    promo_package: bool = False
    excellent_store: bool = False
    bonus_store_plus: bool = False

    def with_bsplus(self, joined: bool) -> "Participation":
        return Participation(self.promo_package, self.excellent_store, joined)


@dataclass(frozen=True)
class Benefit:
    """販促カレンダー1行 ＝ ある期間に有効な1つの特典."""

    id: str
    name: str
    days: tuple[date, ...]
    rate: float = 0.0                 # 付与率。買い回り型は tiers を使う
    coupon_yen: float = 0.0           # 定額値引き(モールクーポン等)
    funding: str = FUNDING_MALL
    eligibility: str = ELIGIBILITY_ALL
    min_order_yen: float = 0.0        # 注文下限金額
    user_cap_yen: float | None = None  # ユーザー側の付与上限(円相当)
    user_cap_period: str = "day"
    tiers: tuple[tuple[float, float], ...] = ()  # (合計注文金額の下限, 付与率)
    traffic_multiplier: float = 1.0   # 付与率とは別のモール集客増(広告出稿など)
    entry_unit: str = "day"           # day | period(期間一括エントリー)
    entry_deadline: date | None = None
    note: str = ""

    def covers(self, day: date) -> bool:
        return day in self.days

    @property
    def requires_entry(self) -> bool:
        """エントリー(ボーナスストアPlus参加)が必要か."""
        return self.eligibility in (ELIGIBILITY_BSPLUS, ELIGIBILITY_BSPLUS_EXCELLENT)

    @property
    def requires_promo_package(self) -> bool:
        return self.eligibility == ELIGIBILITY_PROMO_PACKAGE

    def is_active(self, part: Participation) -> bool:
        """この参加状態でこの特典が有効か."""
        if self.eligibility == ELIGIBILITY_ALL:
            return True
        if self.eligibility == ELIGIBILITY_PROMO_PACKAGE:
            return part.promo_package
        if self.eligibility == ELIGIBILITY_BSPLUS:
            return part.bonus_store_plus
        if self.eligibility == ELIGIBILITY_BSPLUS_EXCELLENT:
            return part.bonus_store_plus and part.excellent_store
        raise ValueError(f"未知の eligibility: {self.eligibility}")

    def rate_for_basket(self, basket_yen: float) -> float:
        """注文金額に応じた付与率. tiers があれば該当する最上位の段を返す."""
        if self.tiers:
            applicable = [r for threshold, r in self.tiers if basket_yen >= threshold]
            return max(applicable) if applicable else 0.0
        return self.rate


@dataclass(frozen=True)
class ScheduleNote:
    """付与率で表せない施策や運用上の注意.

    「ボーナスストアPlusのお買い物で引けるくじ」のように当選確率が公開されず
    期待値を置けないもの、「事前のユーザー訴求NG」のような制約を持つ。
    需要モデルには入れず、レポートに注意事項として出す。
    """

    text: str
    days: tuple[date, ...] = ()


@dataclass(frozen=True)
class PromoSchedule:
    """該当月の販促スケジュール全体."""

    month: str  # "YYYY-MM"
    first_day: date
    last_day: date
    benefits: tuple[Benefit, ...]
    extends_to: date | None = None  # 月を跨ぐ施策の最終日(爆買WEEKの11/1など)
    source_note: str = ""
    notes: tuple[ScheduleNote, ...] = ()

    @property
    def planning_last_day(self) -> date:
        """計画対象の最終日. 月跨ぎ施策があればそこまで伸ばす."""
        if self.extends_to and self.extends_to > self.last_day:
            return self.extends_to
        return self.last_day

    def days(self) -> list[date]:
        out, d = [], self.first_day
        last = self.planning_last_day
        while d <= last:
            out.append(d)
            d += timedelta(days=1)
        return out

    def benefits_on(self, day: date) -> list[Benefit]:
        return [b for b in self.benefits if b.covers(day)]

    def active_on(self, day: date, part: Participation) -> list[Benefit]:
        return [b for b in self.benefits_on(day) if b.is_active(part)]


# --------------------------------------------------------------------------
# 1日の評価結果
# --------------------------------------------------------------------------
@dataclass
class DayContext:
    """需要モデルに渡す1日分の文脈."""

    day: date
    benefits: list[Benefit] = field(default_factory=list)
    is_five_day: bool = False
    is_zorome: bool = False
    is_payday_window: bool = False
    weekday: int = 0

    @property
    def benefit_names(self) -> list[str]:
        seen, out = set(), []
        for b in self.benefits:
            if b.name not in seen:
                seen.add(b.name)
                out.append(b.name)
        return out


@dataclass
class DayEstimate:
    """ある日・ある自社設定還元率での見積り.

    基準線(base)は「ボーナスストアPlusに参加しない場合」。
    参加しても常時施策とプロモパッケージ施策は効いているため、
    基準線はゼロではない点に注意。
    """

    day: date
    store_rate: float          # 自社が設定した還元率(自社負担)
    base_total_rate: float     # 不参加時に顧客が受け取る総付与率
    entry_total_rate: float    # 参加時に顧客が受け取る総付与率
    unlocked_mall_rate: float  # 参加で開いたモール負担分
    base_orders: float
    base_gmv: float
    entry_orders: float
    entry_gmv: float
    effective_store_rate: float  # 1注文あたり上限適用後の自社実効還元率
    base_point_cost: float       # 不参加でも発生する自社原資(ストアポイント等)
    entry_point_cost: float      # 参加時の自社原資の総額
    point_cost: float            # 上記の差分 = エントリー判断で増える原資
    incremental_gmv: float
    incremental_orders: float
    gross_profit_delta: float
    ltv_value: float
    net_value: float
    roas: float
    uplift_ratio: float

    @property
    def is_profitable(self) -> bool:
        return self.net_value > 0


@dataclass
class EntryOption:
    """エントリー単位に対する1つの選択肢(=自社設定還元率)."""

    store_rate: float
    cost: float
    net_value: float
    incremental_gmv: float
    estimates: list[DayEstimate]

    @property
    def roas(self) -> float:
        return self.incremental_gmv / self.cost if self.cost > 0 else float("inf")

    def objective_value(self, objective: str) -> float:
        """最適化の目的関数値."""
        if objective == "gmv":
            return self.incremental_gmv
        if objective == "profit":
            return self.net_value
        raise ValueError(f"未知の objective: {objective}")


@dataclass
class EntryUnit:
    """エントリー申込の最小単位.

    日単位は1日=1ユニット。期間一括エントリー(爆買WEEKなど)は期間全体で
    1ユニットとして採否を決める(all-or-nothing)。
    """

    key: str
    label: str
    days: list[date]
    options: list[EntryOption]
    entry_deadline: date | None = None
    entry_required_benefits: list[str] = field(default_factory=list)
    mandatory: bool = False
    blocked_reason: str | None = None

    @property
    def selectable(self) -> bool:
        return self.blocked_reason is None and bool(self.options)


@dataclass
class PlanResult:
    """最終的な提案."""

    month: str
    generated_for: date
    objective: str
    participation: Participation
    selected: list[tuple[EntryUnit, EntryOption]]
    rejected: list[tuple[EntryUnit, EntryOption, str]]
    blocked: list[EntryUnit]
    budget: float
    total_cost: float
    total_incremental_gmv: float
    total_net_value: float
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
                return opt.store_rate
        return None

    def estimate_for(self, day: date) -> DayEstimate | None:
        for unit, opt in self.selected:
            for est in opt.estimates:
                if est.day == day:
                    return est
        return None


def dedupe(benefits: Iterable[Benefit]) -> list[Benefit]:
    seen, out = set(), []
    for b in benefits:
        if b.id not in seen:
            seen.add(b.id)
            out.append(b)
    return out
