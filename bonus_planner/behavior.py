"""Yahoo!ショッピング顧客の行動モデル(需要予測).

乗算モデル:
    注文数(d) = base_orders x 曜日係数 x 給料日サイクル係数 x 月係数
                x イベント需要倍率(重複逓減)
                x (1 + エントリー上乗せ率)

「イベント需要倍率」と「エントリー上乗せ率」を分けているのが設計上の要点:

  traffic_multiplier ... エントリーしてもしなくても得られるモール全体の需要増
  entry_uplift       ... エントリーして初めて得られる分
                         (特集面掲載 + 「ボーナスストア対象」絞り込み + 還元率表示によるCVR改善)

したがって「プレミアムな日曜日」「感謝デー」「超PayPay祭」「爆買いWEEK」のような
エントリー必須イベントでは、エントリーしないと entry_uplift 分がまるごと機会損失になり、
その機会損失の大きさがそのまま提案の優先順位になる。
"""

from __future__ import annotations

from datetime import date

from . import calendar_rules as cal
from .config import BehaviorParams
from .models import DayContext, PromoEvent, PromoSchedule


def build_day_context(schedule: PromoSchedule, day: date) -> DayContext:
    return DayContext(
        day=day,
        events=schedule.events_on(day),
        is_five_day=cal.is_five_day(day),
        is_zorome=cal.is_zorome(day),
        is_payday_window=cal.is_payday_window(day),
        weekday=day.weekday(),
    )


def combine_with_decay(deltas: list[float], decay: float) -> float:
    """重複する効果を逓減させながら合算する.

    2.2倍 x 1.8倍 x 1.5倍 のような単純な掛け算は現実には起こらない
    (同じ購買意欲の高い顧客層を取り合うため)。
    大きい順に並べ、2件目以降を decay^i で割り引いて足し込む。

    >>> round(combine_with_decay([1.0, 0.5], 0.5), 4)
    1.25
    """
    if not deltas:
        return 0.0
    ordered = sorted((d for d in deltas if d != 0.0), reverse=True)
    return sum(d * (decay ** i) for i, d in enumerate(ordered))


class DemandModel:
    """日次の注文数・注文単価を推定する."""

    def __init__(self, params: BehaviorParams) -> None:
        self.p = params

    # -- 暦要因 -------------------------------------------------------
    def calendar_factor(self, ctx: DayContext) -> float:
        p = self.p
        factor = p.dow.get(cal.weekday_key(ctx.day), 1.0)
        factor *= p.dom.get(cal.dom_bucket(ctx.day), 1.0)
        factor *= p.month.get(f"{ctx.day.month:02d}", 1.0)
        return factor

    def implicit_events(self, ctx: DayContext) -> list[tuple[str, float, float]]:
        """スケジュールに明記されていない暦イベント(5のつく日/ゾロ目).

        販促スケジュール側で同じ日に明示イベントがある場合は、
        二重計上を避けるため暗黙イベントは採用しない
        (公式スケジュールを常に正とする)。
        """
        if ctx.events:
            return []
        out: list[tuple[str, float, float]] = []
        if ctx.is_five_day:
            out.append(("5のつく日", self.p.five_day_traffic, self.p.five_day_uplift))
        if ctx.is_zorome:
            out.append(("ゾロ目の日", self.p.zorome_traffic, self.p.zorome_uplift))
        return out

    # -- 需要 ---------------------------------------------------------
    def traffic_multiplier(self, ctx: DayContext) -> float:
        deltas = [e.traffic_multiplier - 1.0 for e in ctx.events]
        deltas += [t - 1.0 for _, t, _ in self.implicit_events(ctx)]
        return 1.0 + combine_with_decay(deltas, self.p.overlap_decay)

    def base_orders(self, ctx: DayContext) -> float:
        """エントリーしなかった場合の注文数."""
        return self.p.base_orders * self.calendar_factor(ctx) * self.traffic_multiplier(ctx)

    def aov_multiplier(self, ctx: DayContext) -> float:
        """イベント日は高単価商材が動きやすい(比較検討していた層が決済する)."""
        deltas = [e.aov_multiplier - 1.0 for e in ctx.events]
        mult = 1.0 + combine_with_decay(deltas, self.p.overlap_decay)
        return min(mult, self.p.aov_event_lift_cap)

    # -- エントリー効果 -----------------------------------------------
    def max_entry_uplift(self, ctx: DayContext) -> float:
        """参照還元率でエントリーした場合の注文増加率."""
        deltas = [e.entry_uplift for e in ctx.events]
        deltas += [u for _, _, u in self.implicit_events(ctx)]
        if not deltas:
            return self.p.normal_day_uplift
        combined = combine_with_decay(deltas, self.p.overlap_decay)
        # 平常日でも得られる底上げ分は下回らない
        return max(combined, self.p.normal_day_uplift)

    def rate_response(self, perceived_rate: float) -> float:
        """還元率に対する反応(逓減).

        参照還元率で1.0。指数が1未満なので、還元率を2倍にしても効果は2倍にならない。
        ポイント上限で頭打ちになった体感還元率を入力に使う点が重要。
        """
        if perceived_rate <= 0:
            return 0.0
        return (perceived_rate / self.p.reference_rate) ** self.p.rate_elasticity

    def entry_uplift(self, ctx: DayContext, perceived_rate: float) -> float:
        return self.max_entry_uplift(ctx) * self.rate_response(perceived_rate)

    # -- 制約 ---------------------------------------------------------
    def required_min_rate(self, ctx: DayContext) -> float:
        """その日にエントリーするために最低限必要な還元率."""
        return max((e.min_bonus_rate for e in ctx.events), default=0.0)

    def allowed_rates(self, ctx: DayContext) -> list[float]:
        """その日に選べる還元率の候補."""
        candidates = set(self.p.candidate_rates)
        for e in ctx.events:
            if e.allowed_rates:
                candidates &= set(e.allowed_rates)
        floor = self.required_min_rate(ctx)
        usable = sorted(r for r in candidates if r >= floor)
        if not usable and floor > 0:
            # イベントの最低還元率が候補外なら、その最低値のみ選択可能とする
            usable = [floor]
        return usable

    def entry_required_events(self, ctx: DayContext) -> list[PromoEvent]:
        return [e for e in ctx.events if e.entry_required]
