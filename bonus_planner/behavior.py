"""Yahoo!ショッピング顧客の行動モデル(需要予測).

    セッション(d) = base_sessions × 曜日 × 給料日 × 月次季節
                    × 市場規模係数(全ストア共通の付与率)   ← パイの大きさ
                    × 集客係数(付与率とは別の広告効果)
    転換率(d)     = base_cvr
                    × 来訪意欲係数(全ストア共通の付与率)   ← 来訪者の「質」
                    × シェア係数(自社だけの上乗せ)        ← パイの取り分
    注文数(d)     = セッション(d) × 転換率(d)

集客と転換率に分けているのは、実績でこの2つが別々に動いていたため。
2025-09 → 2026-08 でセッションは +16% だが CVR は +23%、単価も +23%。
注文数1本で持つとトレンド推定がこの構造を潰してしまう。
分けておけば、市場規模係数はセッションに、シェア係数は転換率にかかり、
どちらも実績で検証できる量に対応づく。

## 全ストア共通の付与率とシェアを分ける理由

販促カレンダーの施策は2種類ある。

  全ストア対象 (5のつく日・ファーストデイ・定常施策)
      競合も同じ条件になるので、自社のシェアは動かない。
      動くのはモール全体の来訪者数、つまり市場規模。

      ただし来訪者の「数」だけでなく「質」も動く。日別実績では
      5のつく日にセッションが +63%、**転換率も +66%** 上がっていた
      (5のつく日と自社の参加日はこの期間で1日も重ならないため、
      この効果はきれいに識別できている)。
      「5のつく日を待って買う」層が動くぶん、同じ1セッションあたりの
      購買確率が上がる。これを来訪意欲係数として転換率側に持つ。
      競合との相対的な魅力は変わらないので、シェア係数とは別経路。

  参加資格つき (ボーナスストアPlus参加・プロモーションパッケージ加入)
      持っていない競合に対する優位になるので、シェアが動く。
      自社設定の還元率も同じくシェアを動かす。

この2つを1本の反応関数にまとめると、「すでに付与率が高い日は追加の効きが鈍い」
という逓減が効きすぎて、モールが最も集客している日(5のつく日など)を
避ける提案になってしまう。実務感覚と逆で、モデルの作りに起因する歪みになる。

分けておけば、5のつく日は「パイが大きい日」として正しく評価され、
ボーナスストアPlus指定日は「自社だけ有利になれる日」として評価される。

## エントリー判断との関係

ボーナスストアPlusに参加すると、その日だけ開くモール負担の上乗せが
シェア側に乗る。参加しなければその分がまるごと機会損失になる。
"""

from __future__ import annotations

from datetime import date

from . import calendar_rules as cal
from .config import BehaviorParams, StoreConfig
from .economics import perceived_total_rate, rate_by_scope
from .models import DayContext, Participation, PromoSchedule


def build_day_context(schedule: PromoSchedule, day: date) -> DayContext:
    return DayContext(
        day=day,
        benefits=schedule.benefits_on(day),
        is_five_day=cal.is_five_day(day),
        is_zorome=cal.is_zorome(day),
        is_payday_window=cal.is_payday_window(day),
        weekday=day.weekday(),
    )


def combine_with_decay(deltas: list[float], decay: float) -> float:
    """重複する効果を逓減させながら合算する.

    大きい順に並べ、2件目以降を decay^i で割り引いて足し込む。
    同じ購買意欲の高い顧客層を取り合うため、単純な掛け算にはならない。

    >>> round(combine_with_decay([1.0, 0.5], 0.5), 4)
    1.25
    """
    if not deltas:
        return 0.0
    ordered = sorted((d for d in deltas if d != 0.0), reverse=True)
    return sum(d * (decay ** i) for i, d in enumerate(ordered))


class DemandModel:
    """日次の注文数を推定する."""

    def __init__(self, params: BehaviorParams, store: StoreConfig) -> None:
        self.p = params
        self.store = store

    # -- 暦要因 -------------------------------------------------------
    def calendar_factor(self, ctx: DayContext) -> float:
        p = self.p
        return (
            p.dow.get(cal.weekday_key(ctx.day), 1.0)
            * p.dom.get(cal.dom_bucket(ctx.day), 1.0)
            * p.month.get(f"{ctx.day.month:02d}", 1.0)
        )

    def traffic_multiplier(self, ctx: DayContext, part: Participation) -> float:
        """付与率とは別に働くモール集客増(広告出稿など).

        付与率の効果は rate_response が担うため、ここを1.0以外にすると
        二重計上になる。確認できた施策にだけ設定する。
        """
        deltas = [
            b.traffic_multiplier - 1.0
            for b in ctx.benefits
            if b.is_active(part) and b.traffic_multiplier != 1.0
        ]
        return 1.0 + combine_with_decay(deltas, self.p.overlap_decay)

    # -- 付与率への反応 -----------------------------------------------
    def total_rate(self, ctx: DayContext, part: Participation) -> float:
        """その日に顧客が受け取る体感総付与率(注文下限・付与上限込み)."""
        return perceived_total_rate(ctx.benefits, part, self.store)

    def scoped_rates(self, ctx: DayContext, part: Participation) -> tuple[float, float]:
        """(全ストア共通の付与率, 自社だけの上乗せ率) を返す."""
        return rate_by_scope(ctx.benefits, part, self.store)

    def market_factor(self, mall_wide_rate: float) -> float:
        """モール全体の需要規模. 全ストア共通の付与率で決まる.

        baseline_rate(定常施策のみの日)で1.0。
        競合も同条件なので、ここが上がってもシェアは変わらない。
        """
        if mall_wide_rate <= 0:
            return 0.0
        return (mall_wide_rate / self.p.baseline_rate) ** self.p.market_elasticity

    def intent_factor(self, mall_wide_rate: float) -> float:
        """来訪者の購買意欲. 全ストア共通の付与率で決まる.

        market_factor と同じ形だが、掛かる先が転換率である点が違う。
        baseline_rate(定常施策のみの日)で1.0。

        シェア係数と混同しないこと。こちらは競合も同じだけ得をする
        (相対的な優位は生まれない)が、モール全体の転換率が上がる。
        """
        if mall_wide_rate <= 0:
            return 0.0
        return (mall_wide_rate / self.p.baseline_rate) ** self.p.intent_elasticity

    def share_factor(self, own_advantage: float) -> float:
        """自社シェア. 競合に対する付与率の「絶対差」で決まる.

        基準は「参加資格つき施策を何も持たないストア」。優位ゼロで1.0。

        比率ではなく絶対差で見るのが要点。顧客にとって上乗せ2ポイント分の
        価値は、その日の基準率が7%でも11%でも同じ金額(2万円の注文なら400円)。
        比率で見ると、基準率が高い日ほど同じ上乗せが小さく評価され、
        モールが最も集客している日を避ける提案になってしまう。
        """
        if own_advantage <= 0:
            return 1.0
        ratio = own_advantage / self.p.reference_advantage
        return 1.0 + self.p.share_gain_at_reference * (ratio ** self.p.share_elasticity)

    # -- 需要 ---------------------------------------------------------
    def sessions(self, ctx: DayContext, part: Participation) -> float:
        """その日のセッション数. 全ストア共通の付与率(=モール全体の集客)で決まる."""
        mall_wide, _gated = self.scoped_rates(ctx, part)
        return (
            self.p.base_sessions
            * self.calendar_factor(ctx)
            * self.traffic_multiplier(ctx, part)
            * self.market_factor(mall_wide)
        )

    def cvr(self, ctx: DayContext, part: Participation, own_rate: float = 0.0) -> float:
        """その日の転換率. 来訪者の質(全ストア共通)と自社の優位で決まる."""
        mall_wide, gated = self.scoped_rates(ctx, part)
        return min(
            self.p.base_cvr
            * self.intent_factor(mall_wide)
            * self.share_factor(gated + own_rate),
            1.0,
        )

    def orders(self, ctx: DayContext, part: Participation, own_rate: float = 0.0) -> float:
        return self.sessions(ctx, part) * self.cvr(ctx, part, own_rate)

    # -- エントリー判断に関わる情報 -----------------------------------
    def entry_gated_benefits(self, ctx: DayContext) -> list:
        """ボーナスストアPlus参加で初めて開く施策."""
        return [b for b in ctx.benefits if b.requires_entry]

    def unlocked_rate(self, ctx: DayContext, store: StoreConfig) -> float:
        """参加で開くモール負担分の付与率(自社設定率を除く).

        これがゼロの日は、参加しても自社のポイント原資を配るだけになる。
        """
        part_out = store.participation(bonus_store_plus=False)
        part_in = store.participation(bonus_store_plus=True)
        return self.total_rate(ctx, part_in) - self.total_rate(ctx, part_out)
