import unittest
from datetime import datetime

import schedule

WEEKDAYS_930 = {"schedule": "30 9 * * 1-5", "catch_up_until": "13:00"}


def at(day, hour, minute):
    # October 2026: the 26th is a Monday, the 31st a Saturday.
    return datetime(2026, 10, day, hour, minute, 15)


class CronTest(unittest.TestCase):
    def test_weekday_range_excludes_saturday(self):
        self.assertEqual([schedule.matches("30 9 * * 1-5", datetime(2026, 10, d, 9, 30)) for d in (26, 30, 31)], [True, True, False])

    def test_sunday_is_zero_or_seven(self):
        sunday = datetime(2026, 11, 1, 9, 30)
        self.assertEqual([schedule.matches("30 9 * * %d" % d, sunday) for d in (0, 7)], [True, True])

    def test_step_over_wildcard(self):
        self.assertEqual([schedule.matches("*/15 * * * *", datetime(2026, 10, 26, 9, m)) for m in (0, 15, 20)], [True, True, False])

    def test_list_and_range_in_one_field(self):
        self.assertTrue(schedule.matches("0 9,12-13 * * *", datetime(2026, 10, 26, 13, 0)))

    def test_day_of_month_or_day_of_week_when_both_restricted(self):
        self.assertTrue(schedule.matches("0 9 1 * 1", datetime(2026, 10, 26, 9, 0)))

    def test_wrong_field_count_is_rejected(self):
        with self.assertRaises(ValueError):
            schedule.parse("30 9 * *")

    def test_out_of_range_value_is_rejected(self):
        with self.assertRaises(ValueError):
            schedule.parse("61 9 * * *")


class DueSlotTest(unittest.TestCase):
    def test_fires_in_the_slot_minute(self):
        self.assertEqual(schedule.due_slot(WEEKDAYS_930, at(26, 9, 30), None), datetime(2026, 10, 26, 9, 30))

    def test_does_not_fire_before_the_slot(self):
        self.assertIsNone(schedule.due_slot(WEEKDAYS_930, at(26, 9, 29), None))

    def test_catches_up_when_woken_late_the_same_day(self):
        self.assertEqual(schedule.due_slot(WEEKDAYS_930, at(26, 11, 45), None), datetime(2026, 10, 26, 9, 30))

    def test_does_not_catch_up_after_the_cutoff(self):
        self.assertIsNone(schedule.due_slot(WEEKDAYS_930, at(26, 13, 1), None))

    def test_does_not_fire_twice_for_the_same_slot(self):
        self.assertIsNone(schedule.due_slot(WEEKDAYS_930, at(26, 11, 45), datetime(2026, 10, 26, 9, 30)))

    def test_does_not_fire_on_the_weekend(self):
        self.assertIsNone(schedule.due_slot(WEEKDAYS_930, at(31, 10, 0), None))

    def test_yesterdays_missed_slot_is_not_caught_up(self):
        self.assertIsNone(schedule.due_slot(WEEKDAYS_930, at(27, 9, 0), datetime(2026, 10, 26, 9, 30)))

    def test_without_catch_up_a_slot_expires_after_two_minutes(self):
        trigger = {"schedule": "30 9 * * 1-5"}
        self.assertEqual([schedule.due_slot(trigger, at(26, 9, m), None) is not None for m in (31, 32, 33)], [True, True, False])


class NextSlotTest(unittest.TestCase):
    def test_later_today(self):
        self.assertEqual(schedule.next_slot("30 9 * * 1-5", datetime(2026, 10, 26, 8, 0)), datetime(2026, 10, 26, 9, 30))

    def test_after_todays_slot_moves_to_tomorrow(self):
        self.assertEqual(schedule.next_slot("30 9 * * 1-5", datetime(2026, 10, 26, 9, 30)), datetime(2026, 10, 27, 9, 30))

    def test_friday_evening_skips_the_weekend(self):
        self.assertEqual(schedule.next_slot("30 9 * * 1-5", datetime(2026, 10, 30, 18, 0)), datetime(2026, 11, 2, 9, 30))

    def test_several_slots_per_day_pick_the_next_one(self):
        self.assertEqual(schedule.next_slot("0 9,13 * * *", datetime(2026, 10, 26, 10, 0)), datetime(2026, 10, 26, 13, 0))

    def test_impossible_date_returns_none(self):
        self.assertIsNone(schedule.next_slot("0 0 31 2 *", datetime(2026, 10, 26)))


if __name__ == "__main__":
    unittest.main()
