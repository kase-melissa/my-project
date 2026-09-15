"""2026年10月カレンダー転記の検証.

販促カレンダーPDFの最下段「最大付与率」行と、転記したYAMLから算出した
総付与率が一致することを確認する。転記ミスに対する回帰テスト。
"""

import dataclasses
import unittest
from datetime import date
from pathlib import Path

from bonus_planner.economics import nominal_total_rate
from bonus_planner.models import Participation
from bonus_planner.schedule import load_schedule

ROOT = Path(__file__).resolve().parent.parent
SCHEDULE = ROOT / "data" / "promo_schedule" / "2026-10.yaml"

# PDF最下段「最大付与率(単位:%) PayPayポイント(通常)+PayPayポイント(期間限定)」
# 10/1〜10/31 と 11/1 の32日分。
PDF_MAX_RATES = {
    1: 10, 2: 12, 3: 7, 4: 12, 5: 11, 6: 7, 7: 12, 8: 11,
    9: 7, 10: 7, 11: 12, 12: 7, 13: 7, 14: 7, 15: 11, 16: 12,
    17: 7, 18: 12, 19: 7, 20: 12, 21: 7, 22: 12, 23: 7, 24: 7,
    25: 11, 26: 7, 27: 12, 28: 12, 29: 7, 30: 14, 31: 14,
}
PDF_NOV_1 = 14

# PDFの「最大付与率」はすべての上乗せを取り切った前提で書かれている
MAX_ASSUMPTION = Participation(
    promo_package=True, excellent_store=True, bonus_store_plus=True
)
# 貴社の実際の条件(優良ストア非該当)
ACTUAL = Participation(
    promo_package=True, excellent_store=False, bonus_store_plus=True
)

# 爆買WEEKの上位段(合計20,000円以上→7%)に届く買い物かごを想定した金額
BIG_BASKET = 20000.0


def _gold_rank(schedule):
    """感謝デーをゴールドランク(5%)に置き換えたスケジュールを返す.

    転記YAMLの既定はシルバー(4%)。PDFの最大付与率はゴールド前提で
    書かれているため、突き合わせにはこちらを使う。
    """
    benefits = tuple(
        dataclasses.replace(b, rate=0.05) if b.id == "kansha_day" else b
        for b in schedule.benefits
    )
    return dataclasses.replace(schedule, benefits=benefits)


class TestTranscription(unittest.TestCase):
    def setUp(self):
        self.schedule = load_schedule(SCHEDULE)

    def test_covers_october_plus_november_first(self):
        days = self.schedule.days()
        self.assertEqual(len(days), 32)
        self.assertEqual(days[0], date(2026, 10, 1))
        self.assertEqual(days[-1], date(2026, 11, 1))

    def test_matches_pdf_max_rate_row(self):
        """PDF最下段の最大付与率と全32日で一致すること."""
        schedule = _gold_rank(self.schedule)
        mismatches = []
        for day in schedule.days():
            expected = PDF_NOV_1 if day.month == 11 else PDF_MAX_RATES[day.day]
            actual = nominal_total_rate(
                schedule.benefits_on(day), MAX_ASSUMPTION, BIG_BASKET
            )
            actual_pct = round(actual * 100, 6)
            if abs(actual_pct - expected) > 1e-6:
                mismatches.append(f"{day}: PDF={expected}% 算出={actual_pct}%")
        self.assertEqual(mismatches, [], "\n".join(mismatches))

    def test_baseline_is_seven_percent(self):
        """定常施策だけの日は7%になること."""
        plain = date(2026, 10, 3)  # 土曜・イベントなし
        rate = nominal_total_rate(
            self.schedule.benefits_on(plain), MAX_ASSUMPTION, BIG_BASKET
        )
        self.assertAlmostEqual(rate, 0.07, places=6)

    def test_actual_rate_without_excellent_store(self):
        """優良ストア非該当ならボーナスストアPlus指定日は9%(PDFの12%ではない)."""
        for d in (2, 7, 16, 20, 27, 28):
            day = date(2026, 10, d)
            rate = nominal_total_rate(self.schedule.benefits_on(day), ACTUAL, BIG_BASKET)
            self.assertAlmostEqual(rate, 0.09, places=6, msg=f"10/{d}")

    def test_bsplus_days_collapse_to_baseline_without_entry(self):
        """エントリーしなければ指定日でも7%どまり = これが機会損失の正体."""
        no_entry = Participation(promo_package=True, bonus_store_plus=False)
        for d in (2, 7, 16, 20, 27, 28):
            day = date(2026, 10, d)
            rate = nominal_total_rate(self.schedule.benefits_on(day), no_entry, BIG_BASKET)
            self.assertAlmostEqual(rate, 0.07, places=6, msg=f"10/{d}")

    def test_five_day_needs_no_entry(self):
        """5のつく日は全ストア対象。エントリーしなくても11%."""
        no_entry = Participation(promo_package=True, bonus_store_plus=False)
        for d in (5, 15, 25):
            day = date(2026, 10, d)
            rate = nominal_total_rate(self.schedule.benefits_on(day), no_entry, BIG_BASKET)
            self.assertAlmostEqual(rate, 0.11, places=6, msg=f"10/{d}")

    def test_promo_package_gates_premium_sunday(self):
        day = date(2026, 10, 4)
        joined = nominal_total_rate(
            self.schedule.benefits_on(day),
            Participation(promo_package=True), BIG_BASKET,
        )
        not_joined = nominal_total_rate(
            self.schedule.benefits_on(day),
            Participation(promo_package=False), BIG_BASKET,
        )
        self.assertAlmostEqual(joined, 0.12, places=6)
        self.assertAlmostEqual(not_joined, 0.07, places=6)

    def test_premium_sunday_excludes_oct_25(self):
        """10/25は日曜だがプレミアムな日曜日の対象外."""
        benefit = next(b for b in self.schedule.benefits if b.id == "premium_sunday")
        self.assertEqual(
            [d.day for d in benefit.days], [4, 11, 18]
        )

    def test_bakugai_week_is_period_entry_across_month_end(self):
        benefit = next(b for b in self.schedule.benefits if b.id == "bakugai_week")
        self.assertEqual(benefit.entry_unit, "period")
        self.assertEqual(
            [d.isoformat() for d in benefit.days],
            ["2026-10-30", "2026-10-31", "2026-11-01"],
        )

    def test_bakugai_week_tiers(self):
        benefit = next(b for b in self.schedule.benefits if b.id == "bakugai_week")
        self.assertAlmostEqual(benefit.rate_for_basket(4999), 0.0)
        self.assertAlmostEqual(benefit.rate_for_basket(5000), 0.04)
        self.assertAlmostEqual(benefit.rate_for_basket(19999), 0.04)
        self.assertAlmostEqual(benefit.rate_for_basket(20000), 0.07)

    def test_store_point_is_the_only_store_funded_row(self):
        """カレンダー掲載分で自社負担なのはストアポイントのみ."""
        store_funded = [b.id for b in self.schedule.benefits if b.funding == "store"]
        self.assertEqual(store_funded, ["store_point"])

    def test_entry_deadlines_only_on_bonus_store_plus(self):
        """締切を持つのはボーナスストアPlus参加が必要な施策だけ."""
        for b in self.schedule.benefits:
            if b.requires_entry:
                self.assertIsNotNone(b.entry_deadline, b.name)
            else:
                self.assertIsNone(b.entry_deadline, b.name)

    def test_notes_cover_kuji_and_no_preannounce(self):
        texts = " ".join(n.text for n in self.schedule.notes)
        self.assertIn("くじ", texts)
        self.assertIn("訴求NG", texts)


if __name__ == "__main__":
    unittest.main()
