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


class SearchTextTest(unittest.TestCase):
    def test_includes_ticket_keys_and_pull_request_numbers(self):
        text = inbox_tui.search_text("Fix login", "ws", ["PAY-1"], ["https://github.com/o/r/pull/12"])
        self.assertIn("pay-1", text)
        self.assertIn("#12", text)

    def test_is_lowercase(self):
        self.assertIn("fix login", inbox_tui.search_text("Fix Login", "", [], []))


class MatchesTest(unittest.TestCase):
    TEXT = inbox_tui.search_text("Fix login", "payments", ["PAY-1"], ["https://github.com/o/r/pull/12"])

    def test_empty_query_matches_everything(self):
        self.assertTrue(inbox_tui.matches(self.TEXT, ""))

    def test_matches_a_ticket_ignoring_case(self):
        self.assertTrue(inbox_tui.matches(self.TEXT, "pay-1"))

    def test_matches_a_pull_request_number_with_or_without_hash(self):
        self.assertTrue(inbox_tui.matches(self.TEXT, "#12"))
        self.assertTrue(inbox_tui.matches(self.TEXT, "12"))

    def test_matches_part_of_the_title(self):
        self.assertTrue(inbox_tui.matches(self.TEXT, "login"))

    def test_every_word_must_match(self):
        self.assertFalse(inbox_tui.matches(self.TEXT, "login #99"))

    def test_words_match_in_any_order(self):
        self.assertTrue(inbox_tui.matches(self.TEXT, "#12 login"))


class FilterTest(unittest.TestCase):
    def inbox(self):
        inbox = inbox_tui.Inbox.__new__(inbox_tui.Inbox)
        inbox.now = NOW
        inbox.query = ""
        inbox.all_tabs = {
            inbox_tui.OPEN: [{"search": "alpha pay-1"}, {"search": "beta #7"}],
            inbox_tui.STALE: [{"search": "gamma pay-1"}],
            inbox_tui.SETTLED: [],
            inbox_tui.SNOOZED: [{"search": "delta #7"}],
        }
        inbox.filter()
        return inbox

    def test_without_a_query_every_row_shows(self):
        self.assertEqual([len(rows) for rows in self.inbox().tabs.values()], [2, 1, 0, 1])

    def test_query_filters_every_tab(self):
        inbox = self.inbox()
        inbox.query = "#7"
        inbox.filter()
        self.assertEqual([len(rows) for rows in inbox.tabs.values()], [1, 0, 0, 1])

    def test_clearing_the_query_brings_rows_back(self):
        inbox = self.inbox()
        inbox.query = "#7"
        inbox.filter()
        inbox.query = ""
        inbox.filter()
        self.assertEqual([len(rows) for rows in inbox.tabs.values()], [2, 1, 0, 1])


class FilterKeyTest(unittest.TestCase):
    def test_letters_extend_the_query(self):
        self.assertEqual(inbox_tui.filter_key(ord("p"), "pa"), ("pap", True))

    def test_backspace_trims_the_query(self):
        self.assertEqual(inbox_tui.filter_key(127, "pay"), ("pa", True))

    def test_enter_keeps_the_query_and_stops_typing(self):
        self.assertEqual(inbox_tui.filter_key(10, "pay"), ("pay", False))

    def test_escape_clears_the_query_and_stops_typing(self):
        self.assertEqual(inbox_tui.filter_key(27, "pay"), ("", False))

    def test_other_keys_change_nothing(self):
        self.assertEqual(inbox_tui.filter_key(9, "pay"), ("pay", True))


class FooterTest(unittest.TestCase):
    def test_typing_shows_the_query(self):
        self.assertTrue(inbox_tui.footer("pay", True, "", inbox_tui.OPEN).startswith("/pay"))

    def test_active_filter_is_shown_next_to_the_hints(self):
        self.assertTrue(inbox_tui.footer("pay", False, "", inbox_tui.OPEN).endswith("filter: pay"))

    def test_message_wins_over_hints(self):
        self.assertEqual(inbox_tui.footer("", False, "Settled.", inbox_tui.OPEN), "Settled.")


class StoredSearchTest(unittest.TestCase):
    def test_stored_rows_can_be_found_by_ticket_and_pull_request(self):
        entry = {"session": "s1", "title": "T", "cwd": "/p/x", "shelved_at": NOW, "tickets": ["PAY-1"], "pr_urls": ["https://github.com/o/r/pull/12"]}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": os.path.join(tmp, "herdr.sock")}):
            lib.save_json("stale.json", [entry])
            inbox = inbox_tui.Inbox.__new__(inbox_tui.Inbox)
            inbox.now = NOW
            [row] = inbox.stored("stale", {}, "stale_at")
        self.assertTrue(inbox_tui.matches(row["search"], "pay-1 #12"))

    def test_settle_reason_of_older_entries_is_searchable(self):
        entry = {"session": "s1", "title": "T", "cwd": "/p/x", "shelved_at": NOW, "settle_reason": "#9 merged, PAY-1 done"}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": os.path.join(tmp, "herdr.sock")}):
            lib.save_json("settled.json", [entry])
            inbox = inbox_tui.Inbox.__new__(inbox_tui.Inbox)
            inbox.now = NOW
            [row] = inbox.stored("settled", {}, "shelved_at")
        self.assertTrue(inbox_tui.matches(row["search"], "#9"))


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
