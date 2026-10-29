import json
import os
import sys
import tempfile
import time
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


def user_line(content):
    return json.dumps({"type": "user", "message": {"content": content}}) + "\n"


class TicketTest(DaemonTest):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(os.environ["HERDR_SOCKET_PATH"] + ".d", "s1.jsonl")
        os.makedirs(os.path.dirname(self.path))
        patcher = mock.patch.object(daemon, "transcript_path", lambda session_id: self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.rec = {"agent": "claude", "agent_session": {"kind": "id", "value": "s1"}}

    def write(self, *contents):
        with open(self.path, "w") as f:
            f.writelines(user_line(c) for c in contents)

    def test_link_in_a_late_prompt_is_found(self):
        self.write("hi", "more", "still going", "and again", "see https://zopapos.atlassian.net/browse/PAY-1251?atlOrigin=abc")
        self.assertEqual(self.daemon.ticket(self.rec), "PAY-1251")

    def test_link_in_a_prompt_with_a_pasted_image_is_found(self):
        self.write([{"type": "image", "source": {}}, {"type": "text", "text": "context: PAY-77"}])
        self.assertEqual(self.daemon.ticket(self.rec), "PAY-77")

    def test_first_prompt_ticket_is_shown_and_later_mentions_are_not(self):
        self.write("start with PAY-1", "later PAY-2 and PAY-1")
        self.assertEqual((self.daemon.ticket(self.rec), {r["key"]: r["role"] for r in self.daemon.ticket_rows(self.rec)}), ("PAY-1", {"PAY-1": "primary", "PAY-2": "mentioned"}))

    def test_new_mention_is_picked_up_after_the_rescan_interval(self):
        self.write("PAY-1")
        self.daemon.ticket_rows(self.rec)
        self.write("PAY-1", "PAY-2")
        with mock.patch.object(daemon.time, "time", return_value=time.time() + daemon.TICKET_RESCAN_SECS + 1):
            self.assertEqual([r["key"] for r in self.daemon.ticket_rows(self.rec)], ["PAY-1", "PAY-2"])

    def test_pr_title_makes_a_late_link_primary(self):
        self.write("hi", "more", "still going", "see https://x.atlassian.net/browse/PAY-1251?atlOrigin=abc")
        with mock.patch.object(self.daemon, "session_prs", return_value=[{"title": "PAY-1251: Fix it"}]):
            self.assertEqual(self.daemon.ticket_rows(self.rec)[0]["role"], "primary")

    def test_unchanged_transcript_is_not_read_again(self):
        self.write("PAY-1")
        self.daemon.ticket(self.rec)
        with mock.patch.object(daemon.session_tickets, "scan", side_effect=AssertionError("reread")):
            self.assertEqual(self.daemon.ticket(self.rec), "PAY-1")


class ReportTest(DaemonTest):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "s1.jsonl")
        with open(self.path, "w") as f:
            f.writelines(user_line(c) for c in ("start PAY-1", "later see PAY-8 and PAY-9"))
        for name, value in (("transcript_path", lambda session_id: self.path), ("herdr_request", self.capture)):
            patcher = mock.patch.object(daemon, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(self.daemon, "session_prs", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(self.daemon, "hint", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = []

    def capture(self, method, params, timeout=10.0):
        self.calls.append((method, params))
        return {}

    def test_sidebar_shows_the_owned_ticket_and_counts_the_mentions(self):
        rec = agent("a", "idle", agent_session={"kind": "id", "value": "s1"}, terminal_title_stripped="T")
        layout = daemon.layouts([rec], {"a": {"last_used": 1, "status": "idle", "unseen_done": False}}, {}, set(), set())["a"]
        self.daemon.report(rec, {"last_used": 1, "status": "idle", "unseen_done": False}, NOW, layout)
        line = self.calls[0][1]["tokens"]["ticket_line"]
        self.assertEqual((line.endswith("PAY-1 +2"), "PAY-8" in line), (True, False))


class ReplyAgeTest(unittest.TestCase):
    def test_idle_agent_shows_time_since_its_last_answer(self):
        self.assertEqual(daemon.reply_age("idle", {"last_response": NOW - 300}, NOW), "5m ago")

    def test_working_agent_shows_nothing(self):
        self.assertIsNone(daemon.reply_age("working", {"last_response": NOW - 300}, NOW))

    def test_agent_with_no_seen_answer_shows_nothing(self):
        self.assertIsNone(daemon.reply_age("idle", {}, NOW))

    def test_blocked_agent_still_shows_its_last_answer(self):
        self.assertEqual(daemon.reply_age("blocked", {"last_response": NOW - 60}, NOW), "1m ago")


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
