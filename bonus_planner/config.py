"""設定ファイルの読み込みとパラメータ定義."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


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
    """自社(ストア)側の経済条件."""

    store_name: str = "ストア"
    gross_margin_rate: float = 0.35      # 粗利率(モール手数料・送料控除後)
    aov: float = 18000.0                 # 平均注文単価(円)
    aov_sigma: float = 0.55              # 注文単価の対数標準偏差
    point_cap_per_order: float = 5000.0  # 1注文あたり付与ポイント上限
    point_fee_rate: float = 0.0          # ポイント原資にかかる手数料率
    monthly_point_budget: float = 400000.0
    min_roas: float = 4.0                # これを下回る日はエントリーしない
    min_net_value: float = 0.0           # 1日あたり純増効果の下限(円)
    new_customer_ratio: float = 0.45     # 増分注文のうち新規客の割合
    ltv_uplift_per_new_customer: float = 3500.0  # 消耗品リピート等の将来粗利
    mandatory_event_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StoreConfig":
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"store設定に未知のキーがあります: {sorted(unknown)}")
        return cls(**d)


@dataclass
class BehaviorParams:
    """Yahoo!ショッピング顧客の行動パラメータ.

    既定値は業界的な相場観に基づく「初期仮値」。
    自社の受注実績を `calibrate` サブコマンドに通して必ず上書きすること。
    """

    base_orders: float = 40.0
    dow: dict[str, float] = field(default_factory=lambda: {
        "mon": 0.95, "tue": 0.92, "wed": 0.95, "thu": 0.95,
        "fri": 1.00, "sat": 1.08, "sun": 1.18,
    })
    dom: dict[str, float] = field(default_factory=lambda: {
        "d01_05": 1.05, "d06_10": 0.93, "d11_15": 0.96,
        "d16_20": 0.92, "d21_24": 0.95, "d25_end": 1.16,
    })
    month: dict[str, float] = field(default_factory=dict)  # "01".."12"
    five_day_traffic: float = 1.55   # 5のつく日の需要倍率(エントリー非依存分)
    five_day_uplift: float = 0.42    # 5のつく日にエントリーした場合の上乗せ
    zorome_traffic: float = 1.10
    zorome_uplift: float = 0.12
    normal_day_uplift: float = 0.10  # 平常日にエントリーした場合の上乗せ
    reference_rate: float = 0.04     # entry_uplift が定義される基準還元率
    rate_elasticity: float = 0.55    # 還元率に対する反応の逓減指数(<1)
    overlap_decay: float = 0.55      # イベント重複時の2件目以降の逓減
    aov_event_lift_cap: float = 1.35
    candidate_rates: list[float] = field(default_factory=lambda: [0.01, 0.02, 0.03, 0.04, 0.05])
    calibration_note: str = "未キャリブレーション(初期仮値)"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BehaviorParams":
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"behavior設定に未知のキーがあります: {sorted(unknown)}")
        return cls(**d)

    def to_dict(self) -> dict[str, Any]:
        return {f: getattr(self, f) for f in self.__dataclass_fields__}


@dataclass
class AppConfig:
    store: StoreConfig
    behavior: BehaviorParams

    @classmethod
    def load(cls, config_path: str | Path, behavior_path: str | Path | None = None) -> "AppConfig":
        raw = load_yaml(config_path)
        store = StoreConfig.from_dict(raw.get("store", {}))
        if behavior_path is not None:
            behavior = BehaviorParams.from_dict(load_yaml(behavior_path).get("behavior", {}))
        else:
            behavior = BehaviorParams.from_dict(raw.get("behavior", {}))
        return cls(store=store, behavior=behavior)
