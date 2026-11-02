import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import inbox_tui
import lib

NOW = 1_000_000.0


class AgeTest(unittest.TestCase):
    def test_seconds(self):
        self.assertEqual(inbox_tui.age(45), "45s")

    def test_minutes(self):
        self.assertEqual(inbox_tui.age(12 * 60 + 5), "12m")

    def test_hours(self):
        self.assertEqual(inbox_tui.age(3 * 3600), "3h")

    def test_days(self):
        self.assertEqual(inbox_tui.age(2 * 86400 + 5), "2d")

    def test_past_time_does_not_go_negative(self):
        self.assertEqual(inbox_tui.age(-5), "0s")


class TableTest(unittest.TestCase):
    def test_rows_line_up_under_their_headers(self):
        lines, offsets = inbox_tui.table(["Age", "Title"], [["1h", "short"], ["30m", "a longer title"]], 40)
        self.assertEqual({len(line) for line in lines}, {40})

    def test_header_is_underlined_with_column_rules(self):
        lines, _ = inbox_tui.table(["Age", "Title"], [["1h", "x"]], 20)
        self.assertIn("┼", lines[1])

    def test_long_cell_is_truncated_with_an_ellipsis(self):
        lines, _ = inbox_tui.table(["Age", "Title"], [["1h", "x" * 50]], 20)
        self.assertTrue(lines[2].rstrip().endswith("…"))

    def test_column_offsets_point_at_each_column(self):
        _, offsets = inbox_tui.table(["Age", "Title"], [["1h", "x"]], 20)
        self.assertEqual(offsets, [0, 3 + len(inbox_tui.SEPARATOR)])

    def test_empty_table_has_only_its_header(self):
        lines, _ = inbox_tui.table(["Age", "Title"], [], 20)
        self.assertEqual(len(lines), 2)


class SnoozeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": os.path.join(tmp.name, "herdr.sock")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_stale_session_moves_to_snoozed_with_its_wake_time(self):
        entry = {"session": "s1", "title": "T", "cwd": "/p"}
        lib.save_json("stale.json", [entry])
        inbox_tui.snooze({"entry": entry}, inbox_tui.STALE, 3600, NOW)
        self.assertEqual(lib.load_json("stale.json", []), [])
        self.assertEqual(lib.load_json("snoozed.json", [])[0]["wake_at"], NOW + 3600)

    def test_snoozing_again_replaces_the_old_wake_time(self):
        entry = {"session": "s1", "title": "T", "cwd": "/p", "wake_at": NOW + 10}
        lib.save_json("snoozed.json", [entry])
        inbox_tui.snooze({"entry": entry}, inbox_tui.SNOOZED, 7200, NOW)
        self.assertEqual([e["wake_at"] for e in lib.load_json("snoozed.json", [])], [NOW + 7200])

    def test_open_agent_is_shelved_as_snoozed(self):
        rec = {"agent_status": "idle"}
        with mock.patch.object(inbox_tui, "shelve") as shelve:
            inbox_tui.snooze({"rec": rec}, inbox_tui.OPEN, 3600, NOW)
        shelve.assert_called_once_with(rec, "snoozed", wake_at=NOW + 3600, snoozed_at=NOW)

    def test_durations_are_four_hours_to_seven_days(self):
        self.assertEqual([name for name, _ in lib.SNOOZE_CHOICES], ["4 hours", "1 day", "3 days", "7 days"])


class MarkTest(unittest.TestCase):
    def test_workflow_agent_gets_the_calendar_icon(self):
        self.assertEqual(inbox_tui.mark({"tokens": {"workflow": "\uf073 triage (1h ago)"}}, woken=False), inbox_tui.WORKFLOW_ICON)

    def test_agent_back_from_a_snooze_gets_the_snooze_icon(self):
        self.assertEqual(inbox_tui.mark({"tokens": {}}, woken=True), inbox_tui.SNOOZE_ICON)

    def test_other_agents_get_a_blank(self):
        self.assertEqual(inbox_tui.mark({"tokens": {}}, woken=False), " ")


class SettledByTest(unittest.TestCase):
    def test_automatic_settle_shows_its_reason(self):
        entry = {"settled_by": "auto", "settle_reason": "#9 merged, PAY-1 done"}
        self.assertEqual(inbox_tui.settled_by_cells(entry), ["auto", "#9 merged, PAY-1 done"])

    def test_settle_after_a_snooze_shows_its_reason(self):
        entry = {"settled_by": "snooze", "settle_reason": "#9 closed, PAY-1 done"}
        self.assertEqual(inbox_tui.settled_by_cells(entry), ["snooze", "#9 closed, PAY-1 done"])

    def test_manual_and_older_entries_show_you(self):
        self.assertEqual(inbox_tui.settled_by_cells({"title": "T"}), ["you", ""])

    def test_settled_rows_match_their_headers(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": os.path.join(tmp, "herdr.sock")}):
            lib.save_json("settled.json", [{"session": "s1", "title": "T", "cwd": "/p/x", "shelved_at": NOW - 60, "settled_by": "auto", "settle_reason": "why"}])
            inbox = inbox_tui.Inbox.__new__(inbox_tui.Inbox)
            inbox.now = NOW
            [row] = inbox.stored("settled", {}, "shelved_at")
        self.assertEqual(dict(zip(inbox_tui.HEADERS[inbox_tui.SETTLED], row["cells"]))["By"], "auto")
        self.assertEqual(len(row["cells"]), len(inbox_tui.HEADERS[inbox_tui.SETTLED]))


if __name__ == "__main__":
    unittest.main()


class MenuKeyTest(unittest.TestCase):
    def key(self, char, cursor=0):
        return inbox_tui.menu_key(ord(char) if isinstance(char, str) else char, cursor)

    def test_number_picks_that_duration(self):
        self.assertEqual(self.key("4"), (0, 7 * 86400, True))

    def test_enter_picks_the_highlighted_duration(self):
        self.assertEqual(self.key(10, cursor=0), (0, 4 * 3600, True))

    def test_down_moves_to_the_last_duration_and_stops(self):
        cursor = 0
        for _ in range(8):
            cursor, _, _ = self.key("j", cursor)
        self.assertEqual(cursor, len(lib.SNOOZE_CHOICES) - 1)

    def test_up_stops_at_the_first_duration(self):
        self.assertEqual(self.key("k", 0), (0, None, False))

    def test_escape_closes_without_picking(self):
        self.assertEqual(self.key(27, 2), (2, None, True))

    def test_other_keys_change_nothing(self):
        self.assertEqual(self.key("x", 3), (3, None, False))
