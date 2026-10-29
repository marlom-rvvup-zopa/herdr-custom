import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import quick_tui
from lib import CannotShelve

AGENT = {"agent": "claude", "pane_id": "w1:p1", "agent_status": "idle", "terminal_title_stripped": "Fix it"}


class FakeScreen:
    def __init__(self, keys):
        self.keys = list(keys)

    def getch(self):
        return ord(self.keys.pop(0)) if isinstance(self.keys[0], str) else self.keys.pop(0)


class QuickTuiTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.messages = []
        patches = [
            mock.patch.object(quick_tui, "focused_agent", return_value=AGENT),
            mock.patch.object(quick_tui, "settle", side_effect=lambda agent: self.calls.append(("settle", agent["pane_id"]))),
            mock.patch.object(quick_tui, "snooze", side_effect=lambda agent, secs: self.calls.append(("snooze", secs))),
            mock.patch.object(quick_tui, "draw", side_effect=lambda screen, title, lines, cursor, message: self.messages.append(message)),
            mock.patch.object(quick_tui.curses, "curs_set"),
            mock.patch.object(quick_tui.curses, "set_escdelay"),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def press(self, *keys):
        quick_tui.run(FakeScreen(keys))

    def test_c_settles_the_focused_agent(self):
        self.press("c")
        self.assertEqual(self.calls, [("settle", "w1:p1")])

    def test_s_then_a_number_snoozes_for_that_long(self):
        self.press("s", "2")
        self.assertEqual(self.calls, [("snooze", 86400)])

    def test_s_then_enter_snoozes_for_the_highlighted_duration(self):
        self.press("s", 10)
        self.assertEqual(self.calls, [("snooze", 4 * 3600)])

    def test_escape_in_the_snooze_menu_goes_back_without_acting(self):
        self.press("s", 27, "q")
        self.assertEqual(self.calls, [])

    def test_q_cancels(self):
        self.press("q")
        self.assertEqual(self.calls, [])

    def test_a_working_agent_shows_why_and_the_popup_stays_open(self):
        with mock.patch.object(quick_tui, "settle", side_effect=CannotShelve("Agent is working. Wait until it is idle.")):
            self.press("c", "q")
        self.assertIn("Agent is working. Wait until it is idle.", self.messages)

    def test_no_agent_in_the_focused_pane_is_reported(self):
        with mock.patch.object(quick_tui, "focused_agent", side_effect=LookupError("no agent in the focused pane")):
            self.press("x")
        self.assertEqual(self.calls, [])
        self.assertIn("no agent in the focused pane", self.messages)


class FocusedAgentTest(unittest.TestCase):
    def test_finds_the_agent_by_pane(self):
        with mock.patch.dict(os.environ, {"QUICK_PANE_ID": "w1:p1"}), mock.patch.object(quick_tui, "herdr_request", return_value={"agents": [AGENT]}):
            self.assertEqual(quick_tui.focused_agent()["pane_id"], "w1:p1")

    def test_missing_agent_raises(self):
        with mock.patch.dict(os.environ, {"QUICK_PANE_ID": "w1:p9"}), mock.patch.object(quick_tui, "herdr_request", return_value={"agents": [AGENT]}):
            with self.assertRaises(LookupError):
                quick_tui.focused_agent()


if __name__ == "__main__":
    unittest.main()
