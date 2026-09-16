"""設定ファイルの読み込みとパラメータ定義."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .models import Participation


def load_yaml(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"設定ファイルが見つかりません: {p}")
    with p.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"YAMLのトップレベルはマッピングである必要があります: {p}")
    return data


@dataclass
class StoreConfig:
    """自社(ストア)側の経済条件と参加状態."""

    store_name: str = "ストア"
    gross_margin_rate: float = 0.32

    # 注文単価の水準。GMV の計算に使う(月次実績から校正する)。
    aov: float = 19800.0
    # 注文単価のばらつき。order_values_file が無いときのフォールバック用。
    aov_sigma: float = 0.55
    # 注文明細(金額列)のパス。あれば実測分布を使う。
    # 注文下限・付与上限の効き方は分布の形で決まるため、明細があるほうが正確。
    order_values_file: str | None = None
    # ボーナスストアPlusの参加履歴(date,store_rate)のパス。
    # 実績の転換率に含まれる「自社参加による上振れ」を差し引くのに使う。
    participation_file: str | None = None
    point_cap_per_order: float = 5000.0
    point_fee_rate: float = 0.0
    monthly_point_budget: float = 500000.0
    min_roas: float = 5.0
    min_net_value: float = 0.0
    new_customer_ratio: float = 0.50
    ltv_uplift_per_new_customer: float = 4200.0

    # 参加状態 — カレンダーの施策がどれだけ開くかを決める
    promo_package: bool = False       # プロモーションパッケージ加入
    excellent_store: bool = False     # 優良ストア該当

    # ボーナスストアPlusで自社が設定できる還元率の候補(0=参加するが上乗せなし)
    store_bonus_rates: list[float] = field(
        default_factory=lambda: [0.0, 0.01, 0.02, 0.03, 0.04, 0.05]
    )

    # 採算に関わらず必ずエントリーする施策ID
    mandatory_benefit_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StoreConfig":
        known = set(cls.__dataclass_fields__)
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"store設定に未知のキーがあります: {sorted(unknown)}")
        cfg = cls(**d)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not 0 < self.gross_margin_rate < 1:
            raise ValueError("gross_margin_rate は0と1の間である必要があります")
        if self.aov <= 0:
            raise ValueError("aov は正の数である必要があります")
        if self.aov_sigma < 0:
            raise ValueError("aov_sigma は0以上である必要があります")
        if not self.store_bonus_rates:
            raise ValueError("store_bonus_rates が空です")
        if any(r < 0 for r in self.store_bonus_rates):
            raise ValueError("store_bonus_rates に負の値があります")

    @property
    def distribution(self):
        """注文単価分布. 明細があれば実測、無ければ対数正規で近似する.

        1度作ったら使い回す。プランナーが日 x 還元率 x 施策の回数だけ
        期待付与率を問い合わせるため、毎回読み直すと遅い。
        """
        cached = getattr(self, "_distribution", None)
        if cached is None:
            from .distribution import build_distribution

            cached = build_distribution(self.order_values_file, self.aov, self.aov_sigma)
            object.__setattr__(self, "_distribution", cached)
        return cached

    def reset_distribution(self) -> None:
        """aov などを書き換えたあとに分布を作り直す."""
        if hasattr(self, "_distribution"):
            object.__delattr__(self, "_distribution")

    def participation(self, bonus_store_plus: bool) -> Participation:
        return Participation(
            promo_package=self.promo_package,
            excellent_store=self.excellent_store,
            bonus_store_plus=bonus_store_plus,
        )


@dataclass
class BehaviorParams:
    """Yahoo!ショッピング顧客の行動パラメータ.

    需要は「顧客が受け取る総付与率」で動く。baseline_rate(定常施策の合計)を
    基準1.0として、付与率が上がったぶんだけ注文が増える形にしている。
    """

    # 需要は「集客 × 転換率」に分解して持つ。
    # 実績では、成長の大半がセッション増ではなく転換率と単価の改善だった
    # (2025-09 → 2026-08 でセッション +16%、CVR +23%)。
    # 1本にまとめるとこの構造が潰れ、前方予測を誤る。
    base_sessions: float = 220.0   # 定常施策だけの日の1日あたりセッション数
    base_cvr: float = 0.10         # 基準転換率(注文数 / セッション)

    dow: dict[str, float] = field(default_factory=lambda: {
        "mon": 0.95, "tue": 0.92, "wed": 0.95, "thu": 0.95,
        "fri": 1.00, "sat": 1.08, "sun": 1.18,
    })
    dom: dict[str, float] = field(default_factory=lambda: {
        "d01_05": 1.05, "d06_10": 0.93, "d11_15": 0.96,
        "d16_20": 0.92, "d21_24": 0.95, "d25_end": 1.16,
    })
    month: dict[str, float] = field(default_factory=dict)

    # 付与率への反応の基準点。定常施策だけの日の「全ストア共通」付与率を入れる。
    baseline_rate: float = 0.07

    # 市場規模の弾力性(全ストア共通の付与率に対する比率反応):
    #   全ストア共通の付与率が上がると、モール全体の来訪者が増える。
    #   競合も同条件なので自社のシェアは変わらず、パイだけが大きくなる。
    #   既定 0.9 は「5のつく日(7%→11%)で来訪が約1.5倍」に合わせた値。
    #   自社の実績でこの倍率が分かれば、そこから逆算して設定し直すこと。
    market_elasticity: float = 0.90

    # シェアの反応: 競合に対する付与率の「絶対差」で決まる。
    #   顧客から見た上乗せ2ポイント分の価値は、その日の基準率が7%でも11%でも
    #   同じ金額(2万円の注文なら400円)。したがって比率ではなく絶対差で見る。
    #   reference_advantage の優位があるとき注文が share_gain_at_reference だけ増え、
    #   それ以上の優位は share_elasticity で逓減する。
    reference_advantage: float = 0.02       # 基準となる優位(2ポイント)
    share_gain_at_reference: float = 0.20   # そのときの注文増加率
    share_elasticity: float = 0.70          # 優位を増やしたときの逓減
    # traffic_multiplier が複数重なったときの逓減
    overlap_decay: float = 0.55

    calibration_note: str = "未キャリブレーション(初期仮値)"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BehaviorParams":
        known = set(cls.__dataclass_fields__)
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"behavior設定に未知のキーがあります: {sorted(unknown)}")
        params = cls(**d)
        params.validate()
        return params

    @property
    def base_orders(self) -> float:
        """定常施策だけの日の1日あたり注文数. 集客と転換率の積."""
        return self.base_sessions * self.base_cvr

    def validate(self) -> None:
        if self.base_sessions <= 0:
            raise ValueError("base_sessions は正の数である必要があります")
        if not 0 < self.base_cvr <= 1:
            raise ValueError("base_cvr は0より大きく1以下である必要があります")
        if self.baseline_rate <= 0:
            raise ValueError("baseline_rate は正の数である必要があります")
        if not 0 < self.market_elasticity <= 1:
            raise ValueError("market_elasticity は0より大きく1以下である必要があります")
        if not 0 < self.share_elasticity <= 1:
            raise ValueError("share_elasticity は0より大きく1以下である必要があります")
        if self.reference_advantage <= 0:
            raise ValueError("reference_advantage は正の数である必要があります")
        if self.share_gain_at_reference <= 0:
            raise ValueError("share_gain_at_reference は正の数である必要があります")

    def to_dict(self) -> dict[str, Any]:
        return {f: getattr(self, f) for f in self.__dataclass_fields__}


@dataclass
class AppConfig:
    store: StoreConfig
    behavior: BehaviorParams

    @classmethod
    def load(
        cls, config_path: str | Path, behavior_path: str | Path | None = None
    ) -> "AppConfig":
        raw = load_yaml(config_path)
        store = StoreConfig.from_dict(raw.get("store", {}))
        if behavior_path is not None:
            behavior = BehaviorParams.from_dict(load_yaml(behavior_path).get("behavior", {}))
        else:
            behavior = BehaviorParams.from_dict(raw.get("behavior", {}))
        return cls(store=store, behavior=behavior)
