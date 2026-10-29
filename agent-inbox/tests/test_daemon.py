import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import daemon
import lib

NOW = 1_000_000.0


def agent(tid, status="idle", **fields):
    return {"terminal_id": tid, "pane_id": "w1:" + tid, "agent_status": status, "agent": "claude", **fields}


class DaemonTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": os.path.join(tmp.name, "herdr.sock")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.daemon = daemon.Daemon()


class LayoutsTest(unittest.TestCase):
    def order(self, woken):
        agents = [agent("a"), agent("b")]
        state = {"a": {"last_used": 200}, "b": {"last_used": 100}}
        out = daemon.layouts(agents, state, {}, set(), woken)
        return sorted(out, key=lambda tid: out[tid]["sort_key"], reverse=True)

    def test_most_recent_agent_comes_first(self):
        self.assertEqual(self.order(set()), ["a", "b"])

    def test_agent_back_from_a_snooze_comes_first(self):
        self.assertEqual(self.order({"b"}), ["b", "a"])

    def test_woken_flag_is_reported(self):
        out = daemon.layouts([agent("a")], {"a": {"last_used": 1}}, {}, set(), {"a"})
        self.assertTrue(out["a"]["woken"])

    def test_sub_agent_of_a_woken_parent_stays_under_it(self):
        agents = [agent("p"), agent("c"), agent("x")]
        state = {"p": {"last_used": 1}, "c": {"last_used": 2}, "x": {"last_used": 3}}
        out = daemon.layouts(agents, state, {"c": "p"}, set(), {"p"})
        order = sorted(out, key=lambda tid: out[tid]["sort_key"], reverse=True)
        self.assertEqual(order, ["p", "c", "x"])


class WokenTest(DaemonTest):
    def test_prompting_a_woken_agent_clears_it(self):
        lib.save_json("woken.json", {"a": NOW})
        self.assertEqual(self.daemon.update_woken([agent("a", "working")], NOW), set())

    def test_idle_woken_agent_stays_woken(self):
        lib.save_json("woken.json", {"a": NOW})
        self.assertEqual(self.daemon.update_woken([agent("a")], NOW), {"a"})

    def test_resumed_pane_not_yet_listed_stays_woken(self):
        lib.save_json("woken.json", {"a": NOW - 30})
        self.assertEqual(self.daemon.update_woken([], NOW), {"a"})

    def test_pane_that_never_appeared_is_forgotten(self):
        lib.save_json("woken.json", {"a": NOW - 3600})
        self.assertEqual(self.daemon.update_woken([], NOW), set())


class WakeDueTest(DaemonTest):
    ENTRY = {"session": "s1", "title": "T", "wake_at": NOW - 1}

    def test_due_session_is_resumed_and_marked_woken(self):
        lib.save_json("snoozed.json", [self.ENTRY])
        with mock.patch.object(daemon, "resume", return_value="term1") as resume:
            self.daemon.wake_due(NOW)
        resume.assert_called_once_with(self.ENTRY, focus=False)
        self.assertEqual(lib.load_json("woken.json", {}), {"term1": NOW})
        self.assertEqual(lib.load_json("snoozed.json", []), [])

    def test_session_not_due_is_left_alone(self):
        later = {**self.ENTRY, "wake_at": NOW + 60}
        lib.save_json("snoozed.json", [later])
        with mock.patch.object(daemon, "resume") as resume:
            self.daemon.wake_due(NOW)
        resume.assert_not_called()

    def test_failed_wake_is_retried_later(self):
        lib.save_json("snoozed.json", [self.ENTRY])
        with mock.patch.object(daemon, "resume", side_effect=RuntimeError("boom")):
            self.daemon.wake_due(NOW)
        self.assertEqual(lib.load_json("snoozed.json", [])[0]["wake_at"], NOW + 300)


class ArchiveStaleTest(DaemonTest):
    def setUp(self):
        super().setUp()
        self.daemon.state = {"a": {"last_used": NOW - 2 * 86400}, "b": {"last_used": NOW}}

    def archive(self, agents, links=None):
        with mock.patch.object(daemon, "shelve") as shelve:
            kept = self.daemon.archive_stale(agents, links or {}, NOW)
        return kept, shelve

    def test_stale_agent_is_shelved_and_dropped(self):
        kept, shelve = self.archive([agent("a"), agent("b")])
        self.assertEqual([r["terminal_id"] for r in kept], ["b"])
        shelve.assert_called_once()

    def test_stale_agent_in_a_family_stays(self):
        kept, shelve = self.archive([agent("a")], {"a": "b"})
        self.assertEqual(len(kept), 1)
        shelve.assert_not_called()

    def test_agent_that_cannot_be_shelved_stays(self):
        with mock.patch.object(daemon, "shelve", side_effect=lib.CannotShelve("no")):
            kept = self.daemon.archive_stale([agent("a")], {}, NOW)
        self.assertEqual(len(kept), 1)


if __name__ == "__main__":
    unittest.main()
