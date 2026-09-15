import tempfile
import unittest
from datetime import date
from pathlib import Path

from bonus_planner.schedule import load_schedule

BASE = """
month: "2026-10"
entry_deadline_days_before: 3
events:
"""


def write(body: str) -> Path:
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
    f.write(BASE + body)
    f.close()
    return Path(f.name)


class TestLoadSchedule(unittest.TestCase):
    def test_period_expands_to_days(self):
        p = write("""
  - id: matsuri
    name: "超PayPay祭"
    period: ["2026-10-20", "2026-10-23"]
    entry_unit: period
    entry_uplift: 0.5
""")
        s = load_schedule(p)
        self.assertEqual(len(s.events[0].dates), 4)
        self.assertEqual(s.events[0].dates[0], date(2026, 10, 20))
        self.assertEqual(s.events[0].dates[-1], date(2026, 10, 23))

    def test_default_deadline_is_days_before_first_date(self):
        p = write("""
  - id: five
    name: "5のつく日"
    dates: ["2026-10-15"]
    entry_uplift: 0.4
""")
        s = load_schedule(p)
        self.assertEqual(s.events[0].entry_deadline, date(2026, 10, 12))

    def test_explicit_deadline_wins(self):
        p = write("""
  - id: five
    name: "5のつく日"
    dates: ["2026-10-15"]
    entry_uplift: 0.4
    entry_deadline: "2026-10-01"
""")
        self.assertEqual(load_schedule(p).events[0].entry_deadline, date(2026, 10, 1))

    def test_month_bounds(self):
        s = load_schedule(write("""
  - id: a
    name: "a"
    dates: ["2026-10-01"]
    entry_uplift: 0.1
"""))
        self.assertEqual(s.first_day, date(2026, 10, 1))
        self.assertEqual(s.last_day, date(2026, 10, 31))
        self.assertEqual(len(s.days()), 31)

    def test_rejects_date_outside_month(self):
        p = write("""
  - id: a
    name: "a"
    dates: ["2026-11-01"]
    entry_uplift: 0.1
""")
        with self.assertRaisesRegex(ValueError, "対象月"):
            load_schedule(p)

    def test_rejects_duplicate_id(self):
        p = write("""
  - id: a
    name: "a"
    dates: ["2026-10-01"]
    entry_uplift: 0.1
  - id: a
    name: "b"
    dates: ["2026-10-02"]
    entry_uplift: 0.1
""")
        with self.assertRaisesRegex(ValueError, "重複"):
            load_schedule(p)

    def test_rejects_entry_required_without_uplift(self):
        # エントリー必須なのに上乗せ0だと、エントリーの価値が0と評価されてしまう
        p = write("""
  - id: a
    name: "プレミアムな日曜日"
    dates: ["2026-10-04"]
    entry_required: true
""")
        with self.assertRaisesRegex(ValueError, "entry_uplift"):
            load_schedule(p)

    def test_rejects_unknown_key(self):
        p = write("""
  - id: a
    name: "a"
    dates: ["2026-10-01"]
    entry_uplift: 0.1
    typo_key: 1
""")
        with self.assertRaisesRegex(ValueError, "未知のキー"):
            load_schedule(p)

    def test_rejects_bad_entry_unit(self):
        p = write("""
  - id: a
    name: "a"
    dates: ["2026-10-01"]
    entry_uplift: 0.1
    entry_unit: weekly
""")
        with self.assertRaisesRegex(ValueError, "entry_unit"):
            load_schedule(p)

    def test_rejects_reversed_period(self):
        p = write("""
  - id: a
    name: "a"
    period: ["2026-10-10", "2026-10-01"]
    entry_uplift: 0.1
""")
        with self.assertRaisesRegex(ValueError, "逆順"):
            load_schedule(p)

    def test_events_on(self):
        s = load_schedule(write("""
  - id: a
    name: "5のつく日"
    dates: ["2026-10-25"]
    entry_uplift: 0.4
  - id: b
    name: "超PayPay祭"
    period: ["2026-10-20", "2026-10-25"]
    entry_unit: period
    entry_uplift: 0.5
"""))
        self.assertEqual(len(s.events_on(date(2026, 10, 25))), 2)
        self.assertEqual(len(s.events_on(date(2026, 10, 26))), 0)


if __name__ == "__main__":
    unittest.main()
