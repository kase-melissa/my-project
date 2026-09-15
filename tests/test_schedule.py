import tempfile
import unittest
from datetime import date
from pathlib import Path

from bonus_planner.schedule import load_schedule

HEAD = """
month: "2026-10"
entry_deadline_days_before: 3
benefits:
"""


def write(body: str, head: str = HEAD) -> Path:
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
    f.write(head + body)
    f.close()
    return Path(f.name)


class TestLoadSchedule(unittest.TestCase):
    def test_days_all_expands_to_every_day(self):
        s = load_schedule(write("""
  - id: base
    name: "定常"
    days: all
    rate: 0.07
"""))
        self.assertEqual(len(s.benefits[0].days), 31)

    def test_integer_days_are_day_of_month(self):
        s = load_schedule(write("""
  - id: five
    name: "5のつく日"
    days: [5, 15, 25]
    rate: 0.04
"""))
        self.assertEqual([d.day for d in s.benefits[0].days], [5, 15, 25])

    def test_period_expands_and_crosses_month_with_extends_to(self):
        head = 'month: "2026-10"\nextends_to: "2026-11-01"\nbenefits:\n'
        s = load_schedule(write("""
  - id: bakugai
    name: "爆買WEEK"
    period: ["2026-10-30", "2026-11-01"]
    entry_unit: period
    rate: 0.04
""", head))
        self.assertEqual(len(s.benefits[0].days), 3)
        self.assertEqual(s.planning_last_day, date(2026, 11, 1))
        self.assertEqual(len(s.days()), 32)

    def test_rejects_month_crossing_without_extends_to(self):
        with self.assertRaisesRegex(ValueError, "extends_to"):
            load_schedule(write("""
  - id: bakugai
    name: "爆買WEEK"
    period: ["2026-10-30", "2026-11-01"]
    rate: 0.04
"""))

    def test_extends_to_must_be_after_month_end(self):
        head = 'month: "2026-10"\nextends_to: "2026-10-15"\nbenefits:\n'
        with self.assertRaisesRegex(ValueError, "末日"):
            load_schedule(write("""
  - id: a
    name: "a"
    days: [1]
    rate: 0.01
""", head))

    def test_deadline_only_for_bonus_store_plus(self):
        s = load_schedule(write("""
  - id: plus
    name: "BSPlus+2%"
    days: [2]
    rate: 0.02
    eligibility: bonus_store_plus
  - id: pkg
    name: "プレミアムな日曜日"
    days: [4]
    rate: 0.05
    eligibility: promo_package
  - id: free
    name: "5のつく日"
    days: [5]
    rate: 0.04
"""))
        by_id = {b.id: b for b in s.benefits}
        self.assertEqual(by_id["plus"].entry_deadline, date(2026, 9, 29))  # 10/2 の3日前
        self.assertIsNone(by_id["pkg"].entry_deadline)
        self.assertIsNone(by_id["free"].entry_deadline)

    def test_explicit_deadline_wins(self):
        s = load_schedule(write("""
  - id: plus
    name: "BSPlus"
    days: [16]
    rate: 0.02
    eligibility: bonus_store_plus
    entry_deadline: "2026-10-01"
"""))
        self.assertEqual(s.benefits[0].entry_deadline, date(2026, 10, 1))

    def test_tiers_parsed_and_sorted(self):
        s = load_schedule(write("""
  - id: t
    name: "買い回り"
    days: [30]
    tiers:
      - {min_total: 20000, rate: 0.07}
      - {min_total: 5000, rate: 0.04}
"""))
        self.assertEqual(s.benefits[0].tiers, ((5000.0, 0.04), (20000.0, 0.07)))

    def test_rejects_rate_and_tiers_together(self):
        with self.assertRaisesRegex(ValueError, "rate と tiers"):
            load_schedule(write("""
  - id: t
    name: "矛盾"
    days: [1]
    rate: 0.04
    tiers:
      - {min_total: 5000, rate: 0.04}
"""))

    def test_rejects_benefit_without_any_value(self):
        with self.assertRaisesRegex(ValueError, "rate も tiers も"):
            load_schedule(write("""
  - id: empty
    name: "空"
    days: [1]
"""))

    def test_rejects_unknown_key(self):
        with self.assertRaisesRegex(ValueError, "未知のキー"):
            load_schedule(write("""
  - id: a
    name: "a"
    days: [1]
    rate: 0.01
    typo_key: 1
"""))

    def test_rejects_duplicate_id(self):
        with self.assertRaisesRegex(ValueError, "重複"):
            load_schedule(write("""
  - id: a
    name: "a"
    days: [1]
    rate: 0.01
  - id: a
    name: "b"
    days: [2]
    rate: 0.01
"""))

    def test_rejects_bad_eligibility(self):
        with self.assertRaisesRegex(ValueError, "eligibility"):
            load_schedule(write("""
  - id: a
    name: "a"
    days: [1]
    rate: 0.01
    eligibility: vip_only
"""))

    def test_rejects_bad_funding(self):
        with self.assertRaisesRegex(ValueError, "funding"):
            load_schedule(write("""
  - id: a
    name: "a"
    days: [1]
    rate: 0.01
    funding: customer
"""))

    def test_rejects_reversed_period(self):
        with self.assertRaisesRegex(ValueError, "逆順"):
            load_schedule(write("""
  - id: a
    name: "a"
    period: ["2026-10-10", "2026-10-01"]
    rate: 0.01
"""))

    def test_rejects_invalid_day_of_month(self):
        with self.assertRaisesRegex(ValueError, "不正な日"):
            load_schedule(write("""
  - id: a
    name: "a"
    days: [32]
    rate: 0.01
"""))

    def test_rejects_v1_events_format(self):
        head = 'month: "2026-10"\nevents:\n'
        with self.assertRaisesRegex(ValueError, "benefits がありません"):
            load_schedule(write("""
  - id: a
    name: "a"
    dates: ["2026-10-01"]
""", head))

    def test_notes_parsed(self):
        s = load_schedule(write("""
  - id: a
    name: "a"
    days: [1]
    rate: 0.01
notes:
  - days: [26, 27]
    text: "くじ"
  - text: "全期間の注意"
"""))
        self.assertEqual(len(s.notes), 2)
        self.assertEqual([d.day for d in s.notes[0].days], [26, 27])
        self.assertEqual(s.notes[1].days, ())

    def test_benefits_on(self):
        s = load_schedule(write("""
  - id: a
    name: "a"
    days: all
    rate: 0.07
  - id: b
    name: "b"
    days: [25]
    rate: 0.04
"""))
        self.assertEqual(len(s.benefits_on(date(2026, 10, 25))), 2)
        self.assertEqual(len(s.benefits_on(date(2026, 10, 26))), 1)


if __name__ == "__main__":
    unittest.main()
