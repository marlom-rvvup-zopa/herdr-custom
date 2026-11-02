import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import daemon
import github_prs
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
        for name in ("hint", "has_shell"):
            patcher = mock.patch.object(self.daemon, name, return_value=None)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.calls = []
        self.check("PAY-1", "PAY-8", "PAY-9")

    def check(self, *keys):
        """Jira has confirmed these tickets exist."""
        for key in keys:
            self.daemon.ticket_status[key] = {"done": False, "at": time.time(), "ok_at": time.time()}

    def capture(self, method, params, timeout=10.0):
        self.calls.append((method, params))
        return {}

    def test_sidebar_shows_the_owned_ticket_and_counts_the_mentions(self):
        rec = agent("a", "idle", agent_session={"kind": "id", "value": "s1"}, terminal_title_stripped="T")
        layout = daemon.layouts([rec], {"a": {"last_used": 1, "status": "idle", "unseen_done": False}}, {}, set(), set())["a"]
        self.daemon.report(rec, {"last_used": 1, "status": "idle", "unseen_done": False}, NOW, layout)
        line = self.calls[0][1]["tokens"]["ticket_line"]
        self.assertEqual((line.endswith("PAY-1 +2"), "PAY-8" in line), (True, False))

    def sidebar_ticket_line(self, rows, done):
        rec = agent("a", "idle", agent_session={"kind": "id", "value": "s1"}, terminal_title_stripped="T")
        for key in done:
            self.daemon.ticket_status[key]["done"] = True
        layout = daemon.layouts([rec], {"a": {"last_used": 1, "status": "idle", "unseen_done": False}}, {}, set(), set())["a"]
        with mock.patch.object(self.daemon, "ticket_rows", return_value=rows):
            self.daemon.report(rec, {"last_used": 1, "status": "idle", "unseen_done": False}, NOW, layout)
        return self.calls[0][1]["tokens"]["ticket_line"]

    def test_done_tickets_are_counted_instead_of_listed(self):
        rows = [{"key": key, "role": "primary", "sources": []} for key in ("PAY-1", "PAY-8", "PAY-9")]
        line = self.sidebar_ticket_line(rows, ["PAY-8", "PAY-9"])
        self.assertEqual((line.endswith("PAY-1 +2"), "PAY-8" in line), (True, False))

    def test_done_tickets_add_to_the_mention_count(self):
        rows = [{"key": "PAY-1", "role": "primary", "sources": []}, {"key": "PAY-8", "role": "primary", "sources": []}, {"key": "PAY-9", "role": "mentioned", "sources": []}]
        self.assertTrue(self.sidebar_ticket_line(rows, ["PAY-8"]).endswith("PAY-1 +2"))

    def test_only_done_tickets_show_just_the_count(self):
        rows = [{"key": key, "role": "primary", "sources": []} for key in ("PAY-1", "PAY-8")]
        self.assertTrue(self.sidebar_ticket_line(rows, ["PAY-1", "PAY-8"]).endswith(" +2"))

    def test_waiting_parent_is_reported_through_the_working_token(self):
        parent, child = agent("p", "idle", terminal_title_stripped="T"), agent("c", "working")
        state = {"p": {"last_used": 1, "status": "idle", "unseen_done": False}, "c": {"last_used": 2, "status": "working", "unseen_done": False}}
        layout = daemon.layouts([parent, child], state, {"c": "p"}, set(), set())["p"]
        self.daemon.report(parent, state["p"], NOW, layout)
        tokens = self.calls[0][1]["tokens"]
        self.assertEqual((tokens["st_working"], tokens["st_idle"]), (daemon.STATE_LABELS["waiting"], None))


class TicketCheckTest(TicketTest):
    def setUp(self):
        super().setUp()
        self.write("start with PAY-1 and QS-2")

    def keys(self):
        return [r["key"] for r in self.daemon.ticket_rows(self.rec)]

    def test_every_prefix_is_accepted_until_the_project_list_is_known(self):
        self.assertEqual(self.keys(), ["PAY-1", "QS-2"])

    def test_only_prefixes_of_projects_we_can_see_are_accepted(self):
        self.daemon.projects = {"keys": ["PAY"], "at": time.time()}
        self.assertEqual(self.keys(), ["PAY-1"])

    def test_tracked_prefixes_are_accepted_as_well(self):
        self.daemon.projects = {"keys": ["PAY"], "at": time.time()}
        with mock.patch.object(daemon, "load_json", return_value=["QS"]):
            self.assertEqual(self.keys(), ["PAY-1", "QS-2"])

    def test_a_ticket_jira_does_not_know_is_dropped(self):
        self.daemon.ticket_status["QS-2"] = {"done": False, "missing": True, "at": time.time(), "ok_at": time.time()}
        self.assertEqual(self.keys(), ["PAY-1"])

    def test_a_ticket_is_shown_only_after_jira_confirmed_it(self):
        self.assertEqual(self.daemon.checked_tickets(["PAY-1"]), [])
        self.assertEqual(self.daemon.ticket_queue.get_nowait(), "PAY-1")

    def test_done_tickets_come_last(self):
        now = time.time()
        self.daemon.ticket_status["PAY-1"] = {"done": True, "at": now, "ok_at": now}
        self.daemon.ticket_status["PAY-2"] = {"done": False, "at": now, "ok_at": now}
        self.assertEqual(self.daemon.checked_tickets(["PAY-1", "PAY-2"]), ["PAY-2", "PAY-1"])

    def test_a_confirmed_ticket_is_shown(self):
        self.daemon.ticket_status["PAY-1"] = {"done": False, "at": time.time(), "ok_at": time.time()}
        self.assertEqual(self.daemon.checked_tickets(["PAY-1"]), ["PAY-1"])

    def test_a_ticket_jira_does_not_know_is_never_shown(self):
        self.daemon.ticket_status["PAY-1"] = {"done": False, "missing": True, "at": time.time(), "ok_at": time.time()}
        self.assertEqual(self.daemon.checked_tickets(["PAY-1"]), [])

    def test_a_ticket_jira_does_not_know_is_remembered_by_the_lookup_loop(self):
        self.daemon.ticket_queue.put("QS-2")
        with mock.patch.object(daemon.jira_status, "is_done", side_effect=daemon.jira_status.TicketNotFound("nope")), mock.patch.object(self.daemon.ticket_queue, "get", side_effect=["QS-2", SystemExit]):
            with self.assertRaises(SystemExit):
                self.daemon.ticket_loop()
        self.assertTrue(self.daemon.ticket_status["QS-2"]["missing"])


class ShellMarkTest(DaemonTest):
    def setUp(self):
        super().setUp()
        self.calls = []
        patcher = mock.patch.object(daemon, "herdr_request", self.capture)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.rec = agent("a", "idle", agent_session={"kind": "id", "value": "s1"})
        self.layout = daemon.layouts([self.rec], {"a": {"last_used": 1, "unseen_done": False}}, {}, set(), set())["a"]

    def capture(self, method, params, timeout=10.0):
        self.calls.append((method, params))
        if method == "pane.process_info":
            return {"process_info": {"foreground_process_group_id": 74565}}
        return {}

    def mark(self, shell_parents):
        self.daemon.shell_parents = shell_parents
        with mock.patch.object(daemon.native_subs, "running", return_value=[]), mock.patch.object(self.daemon, "session_prs", return_value=[]):
            self.daemon.report_extra(self.rec, self.layout)
        return next(p["tokens"] for m, p in self.calls if m == "pane.report_metadata")["shell_mark"]

    def test_agent_whose_claude_has_a_shell_shows_the_terminal_icon(self):
        self.assertEqual(self.mark({74565}), daemon.SHELL_ICON)

    def test_agent_without_a_shell_shows_nothing(self):
        self.assertIsNone(self.mark({1}))

    def test_other_agents_than_claude_are_not_looked_up(self):
        self.rec["agent"] = "codex"
        self.mark({74565})
        self.assertNotIn("pane.process_info", [m for m, _ in self.calls])


def open_pr(number, state="OPEN"):
    return {"number": number, "state": state, "statusCheckRollup": [], "url": "u%d" % number}


class LinkRowsTest(unittest.TestCase):
    def test_everything_that_fits_stays_on_the_first_row(self):
        first, *rest = daemon.link_rows(["PAY-1", "PAY-2"], [open_pr(1), open_pr(2)], 40)
        self.assertEqual((first["ticket"], len(first["pr"]), rest), (["PAY-1", "PAY-2"], 2, [{"ticket": [], "pr": []}] * 3))

    def test_tickets_that_do_not_fit_wrap_onto_the_second_row(self):
        first, second, *_ = daemon.link_rows(["PAY-1", "PAY-2", "PAY-3"], [], 16)
        self.assertEqual((first["ticket"], second["ticket"]), (["PAY-1", "PAY-2"], ["PAY-3"]))

    def test_prs_that_do_not_fit_wrap_after_the_tickets(self):
        first, second, *_ = daemon.link_rows(["PAY-1", "PAY-2"], [open_pr(1), open_pr(2)], 18)
        self.assertEqual((first["ticket"], first["pr"], second["pr"]), (["PAY-1", "PAY-2"], [], [open_pr(1), open_pr(2)]))

    def test_prs_are_ordered_like_their_tokens(self):
        merged, live = open_pr(1, "MERGED"), open_pr(2)
        first, *_ = daemon.link_rows([], [merged, live], 40)
        self.assertEqual(first["pr"], [live, merged])

    def test_combining_strike_marks_take_no_room(self):
        first, *_ = daemon.link_rows([github_prs.strike("PAY-1"), github_prs.strike("PAY-2")], [], 14)
        self.assertEqual(len(first["ticket"]), 2)

    def test_prs_keep_wrapping_onto_further_rows(self):
        prs = [open_pr(n) for n in range(10, 16)]
        rows = daemon.link_rows([], prs, 14)
        self.assertEqual([len(row["pr"]) for row in rows], [3, 3, 0, 0])

    def test_what_overflows_the_last_row_stays_on_it(self):
        rows = daemon.link_rows(["PAY-1", "PAY-2", "PAY-3", "PAY-4", "PAY-5"], [], 10)
        self.assertEqual(rows[-1]["ticket"], ["PAY-4", "PAY-5"])


class PrLineTest(ReportTest):
    def tokens(self, ticket_count, pr_count, width, numbers=(1, 2, 3)):
        rows = [{"key": "PAY-%d" % n, "role": "primary", "sources": []} for n in range(ticket_count)]
        self.check(*[row["key"] for row in rows])
        rec = agent("a", "idle", agent_session={"kind": "id", "value": "s1"}, terminal_title_stripped="T")
        state = {"a": {"last_used": 1, "status": "idle", "unseen_done": False}}
        layout = daemon.layouts([rec], state, {}, set(), set())["a"]
        prs = [open_pr(n) for n in numbers][:pr_count]
        with mock.patch.object(daemon, "LINK_ROW_WIDTH", width), mock.patch.object(self.daemon, "ticket_rows", return_value=rows), mock.patch.object(self.daemon, "session_prs", return_value=prs):
            self.daemon.report(rec, state["a"], NOW, layout)
            self.daemon.report_extra(rec, layout)
        main = next(p["tokens"] for m, p in self.calls if p["source"] == daemon.SOURCE)
        extra = next(p["tokens"] for m, p in self.calls if p["source"] == daemon.NATIVE_SOURCE)
        self.more = next(p["tokens"] for m, p in self.calls if p["source"] == daemon.LINKS_SOURCE)
        return main, extra

    def test_links_that_fit_share_the_first_row(self):
        main, extra = self.tokens(2, 3, 60)
        self.assertEqual((main["pr_open"].endswith("#1 #2 #3"), extra["prl_open"], extra["ticket_line2"]), (True, None, None))

    def test_prs_that_do_not_fit_next_to_the_tickets_get_the_second_row(self):
        main, extra = self.tokens(3, 3, 24)
        self.assertEqual((main["pr_open"], extra["prl_open"].endswith("#1 #2 #3")), (None, True))

    def test_tickets_that_do_not_fit_continue_on_the_second_row(self):
        main, extra = self.tokens(4, 0, 16)
        self.assertEqual((main["ticket_line"].endswith("PAY-0 PAY-1"), extra["ticket_line2"].endswith("PAY-2 PAY-3")), (True, True))

    def test_prs_that_overflow_the_second_row_continue_in_the_third_and_fourth(self):
        self.tokens(0, 9, 14, numbers=range(10, 19))
        self.assertEqual((self.more["pr3_open"].endswith("#16 #17 #18"), self.more["pr4_open"]), (True, None))

    def test_further_rows_are_reported_in_a_source_of_their_own(self):
        self.tokens(0, 1, 40)
        self.assertEqual(sorted(self.more), sorted(["ticket_line3", "ticket_line4", *github_prs.PR_ROW_TOKENS[2].values(), *github_prs.PR_ROW_TOKENS[3].values()]))


class RenameClearedSessionTest(DaemonTest):
    def setUp(self):
        super().setUp()
        self.calls = []
        for name, value in (("herdr_request", self.capture), ("save_json", lambda name, data: None)):
            patcher = mock.patch.object(daemon, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.daemon.state = {"a": {"last_used": 1}}
        self.cleared = agent("a", tab_id="t1", terminal_title_stripped="Claude Code", agent_session={"kind": "id", "value": "new"})

    def capture(self, method, params, timeout=10.0):
        self.calls.append((method, params))
        return {"panes": [{"pane_id": "w1:a", "label": "Old title", "cwd": "/x"}]}

    def test_tab_named_by_the_plugin_goes_back_to_its_position(self):
        self.daemon.tab_labels = {"t1": "2 · Old title"}
        self.daemon.rename_tabs([self.cleared], [{"tab_id": "t1", "label": "2 · Old title"}], {"t1": 2})
        self.assertEqual(self.calls, [("tab.rename", {"tab_id": "t1", "label": "2"})])

    def test_tab_the_user_named_is_kept(self):
        self.daemon.tab_labels = {"t1": "2 · Old title"}
        self.daemon.rename_tabs([self.cleared], [{"tab_id": "t1", "label": "my tab"}], {"t1": 2})
        self.assertEqual(self.calls, [])

    def test_pane_labelled_with_an_earlier_title_loses_it(self):
        self.daemon.pane_titles = {"w1:a": ["Old title"]}
        self.daemon.pane_labels = {"w1:a": "Old title"}
        self.daemon.rename_panes([self.cleared])
        self.assertEqual(self.calls[-1], ("pane.rename", {"pane_id": "w1:a", "label": None}))

    def test_pane_the_user_named_is_kept(self):
        self.daemon.pane_titles = {"w1:a": ["Another title"]}
        self.daemon.pane_labels = {"w1:a": "Old title"}
        self.daemon.rename_panes([self.cleared])
        self.assertEqual([c[0] for c in self.calls], ["pane.list"])

    def test_label_met_for_the_first_time_is_herdrs_and_follows_the_sidebar_title(self):
        named = agent("a", tab_id="t1", terminal_title_stripped="New title")
        self.daemon.rename_panes([named])
        self.assertEqual(self.calls[-1], ("pane.rename", {"pane_id": "w1:a", "label": "New title"}))

    def test_tab_shared_by_several_agents_is_left_alone(self):
        agents = [agent(tid, tab_id="t1", terminal_title_stripped="Title " + tid) for tid in ("a", "b")]
        self.daemon.state["b"] = {"last_used": 2}
        self.daemon.rename_tabs(agents, [{"tab_id": "t1", "label": "2"}], {"t1": 2})
        self.assertEqual(self.calls, [])

    def test_tab_takes_the_sidebar_title(self):
        named = agent("a", tab_id="t1", terminal_title_stripped="PAY-1 Fix login")
        self.daemon.rename_tabs([named], [{"tab_id": "t1", "label": "2"}], {"t1": 2})
        self.assertEqual(self.calls, [("tab.rename", {"tab_id": "t1", "label": "2 · PAY-1 Fix login"})])


class SettlePassTest(DaemonTest):
    def setUp(self):
        super().setUp()
        self.rec = agent("a", "idle", agent_session={"kind": "id", "value": "s1"}, terminal_title_stripped="Fix it")
        self.daemon.state = {"a": {"last_used": NOW - 7200, "status": "idle", "unseen_done": False}}
        self.daemon.ticket_status = {"PAY-1": {"done": True, "at": NOW, "ok_at": time.time()}}
        self.daemon.prs = {(self.rec.get("cwd"), "s1"): {"prs": [], "at": time.time(), "ok_at": time.time()}}
        self.daemon.links = {self.rec["pane_id"]: {}}
        self.shelved, self.notified = [], []
        patches = {
            "shelve": lambda rec, store, **extra: self.shelved.append((rec["pane_id"], store, extra)),
            "notify": lambda title, body=None: self.notified.append((title, body)),
            "last_interaction": lambda rec, last_used, touched: NOW - 7200,
        }
        for name, value in patches.items():
            patcher = mock.patch.object(daemon, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in (("ticket_rows", [{"key": "PAY-1", "role": "primary", "sources": ["pr"]}]), ("session_prs", [{"number": 9, "state": "MERGED"}])):
            patcher = mock.patch.object(self.daemon, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_pass(self, mode=None):
        if mode:
            lib.save_json("settle.json", {"mode": mode})
        self.daemon.settle_pass([self.rec], {}, NOW)

    def test_default_mode_only_suggests(self):
        self.run_pass()
        self.assertEqual((self.shelved, self.notified[0][0]), ([], "Ready to settle"))

    def test_suggestion_is_sent_once_per_session(self):
        self.run_pass()
        self.run_pass()
        self.assertEqual(len(self.notified), 1)

    def test_auto_mode_settles_and_records_why(self):
        self.run_pass("auto")
        pane, store, extra = self.shelved[0]
        self.assertEqual((store, extra["settled_by"], "#9 merged" in extra["settle_reason"], self.notified[0][0]), ("settled", "auto", True, "Auto-settled"))

    def test_off_mode_does_nothing_but_still_records_the_verdict(self):
        self.run_pass("off")
        self.assertEqual((self.shelved, self.notified, self.daemon.links[self.rec["pane_id"]]["settle"]["ready"]), ([], [], True))

    def test_unknown_mode_is_treated_as_off(self):
        self.run_pass("yolo")
        self.assertEqual((self.shelved, self.notified), ([], []))

    def test_done_ticket_does_not_override_an_open_pr(self):
        with mock.patch.object(self.daemon, "session_prs", return_value=[{"number": 9, "state": "MERGED"}, {"number": 10, "state": "OPEN"}]):
            self.run_pass("auto")
        self.assertEqual((self.shelved, self.daemon.links[self.rec["pane_id"]]["settle"]["reason"]), ([], "PR #10 is open"))

    def test_stale_pr_data_delays_the_settle_and_queues_a_refresh(self):
        self.daemon.prs[(self.rec.get("cwd"), "s1")]["ok_at"] = time.time() - 3600
        self.run_pass("auto")
        self.assertEqual((self.shelved, self.daemon.pr_queue.qsize()), ([], 1))

    def test_stale_ticket_data_delays_the_settle_and_queues_a_refresh(self):
        self.daemon.ticket_status["PAY-1"]["ok_at"] = time.time() - 3600
        self.run_pass("auto")
        self.assertEqual((self.shelved, self.daemon.ticket_queue.qsize()), ([], 1))

    def test_failed_lookup_does_not_make_old_data_look_fresh(self):
        self.daemon.ticket_status["PAY-1"].update({"ok_at": time.time() - 3600, "at": time.time()})
        self.run_pass("auto")
        self.assertEqual(self.shelved, [])

    def test_suggestions_do_not_wait_for_fresh_data(self):
        self.daemon.ticket_status["PAY-1"]["ok_at"] = 0
        self.run_pass("suggest")
        self.assertEqual(len(self.notified), 1)

    def test_unfinished_session_is_left_alone_and_the_reason_is_recorded(self):
        self.daemon.ticket_status = {"PAY-1": {"done": False, "at": NOW, "ok_at": time.time()}}
        self.run_pass("auto")
        self.assertEqual((self.shelved, self.daemon.links[self.rec["pane_id"]]["settle"]["reason"]), ([], "PAY-1 is not done in Jira"))

    def test_failed_shelve_is_logged_not_raised(self):
        def refuse(rec, store, **extra):
            raise daemon.CannotShelve("nope")

        with mock.patch.object(daemon, "shelve", refuse):
            self.run_pass("auto")
        self.assertEqual(self.notified, [])

    def test_sub_agent_family_member_is_not_settled(self):
        lib.save_json("settle.json", {"mode": "auto"})
        self.daemon.settle_pass([self.rec], {"a": "parent"}, NOW)
        self.assertEqual(self.shelved, [])


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
        state = {"a": {"last_used": 200, "unseen_done": False}, "b": {"last_used": 100, "unseen_done": False}}
        out = daemon.layouts(agents, state, {}, set(), woken)
        return sorted(out, key=lambda tid: out[tid]["sort_key"], reverse=True)

    def test_most_recent_agent_comes_first(self):
        self.assertEqual(self.order(set()), ["a", "b"])

    def test_agent_back_from_a_snooze_comes_first(self):
        self.assertEqual(self.order({"b"}), ["b", "a"])

    def test_woken_flag_is_reported(self):
        out = daemon.layouts([agent("a")], {"a": {"last_used": 1, "unseen_done": False}}, {}, set(), {"a"})
        self.assertTrue(out["a"]["woken"])

    def test_sub_agent_of_a_woken_parent_stays_under_it(self):
        agents = [agent("p"), agent("c"), agent("x")]
        state = {tid: {"last_used": used, "unseen_done": False} for tid, used in (("p", 1), ("c", 2), ("x", 3))}
        out = daemon.layouts(agents, state, {"c": "p"}, set(), {"p"})
        order = sorted(out, key=lambda tid: out[tid]["sort_key"], reverse=True)
        self.assertEqual(order, ["p", "c", "x"])


class NativeTokensTest(unittest.TestCase):
    def test_native_sub_agent_of_a_main_agent_starts_at_the_row_edge(self):
        layout = daemon.layouts([agent("p")], {"p": {"last_used": 1, "unseen_done": False}}, {}, set(), set())["p"]
        tokens = daemon.native_tokens([("general-purpose", "Lazy-load")], layout)
        self.assertEqual(tokens["native_1"], daemon.LAST_SUB_MARK + "general-purpose · Lazy-load")


class UrgencyOrderTest(unittest.TestCase):
    def layout(self, agents, parents=None, unseen_done=()):
        state = {rec["terminal_id"]: {"last_used": n, "unseen_done": rec["terminal_id"] in unseen_done} for n, rec in enumerate(agents)}
        return daemon.layouts(agents, state, parents or {}, set(), set())

    def order(self, out):
        return sorted(out, key=lambda tid: out[tid]["sort_key"], reverse=True)

    def test_needs_input_then_idle_then_done_then_working_whatever_the_recency(self):
        agents = [agent("working", "working"), agent("idle", "idle"), agent("done", "done"), agent("blocked", "blocked")]
        self.assertEqual(self.order(self.layout(agents)), ["blocked", "idle", "done", "working"])

    def test_recency_orders_agents_in_the_same_state(self):
        agents = [agent("old", "idle"), agent("new", "idle")]
        self.assertEqual(self.order(self.layout(agents)), ["new", "old"])

    def test_idle_agent_with_a_working_sub_agent_is_waiting(self):
        out = self.layout([agent("p", "idle"), agent("c", "working")], {"c": "p"})
        self.assertEqual((out["p"]["status"], out["c"]["status"]), ("waiting", "working"))

    def test_done_agent_with_a_working_sub_agent_is_waiting(self):
        out = self.layout([agent("p", "done"), agent("c", "working")], {"c": "p"})
        self.assertEqual(out["p"]["status"], "waiting")

    def test_unseen_result_with_a_working_sub_agent_is_waiting(self):
        out = self.layout([agent("p", "idle"), agent("c", "working")], {"c": "p"}, unseen_done={"p"})
        self.assertEqual(out["p"]["status"], "waiting")

    def test_every_ancestor_of_a_working_sub_agent_is_waiting(self):
        agents = [agent("root", "idle"), agent("mid", "idle"), agent("leaf", "working")]
        out = self.layout(agents, {"mid": "root", "leaf": "mid"})
        self.assertEqual((out["root"]["status"], out["mid"]["status"]), ("waiting", "waiting"))

    def test_agent_needing_input_stays_blocked_while_a_sub_agent_works(self):
        out = self.layout([agent("p", "blocked"), agent("c", "working")], {"c": "p"})
        self.assertEqual(out["p"]["status"], "blocked")

    def test_idle_sub_agents_leave_the_parent_idle(self):
        out = self.layout([agent("p", "idle"), agent("c", "idle")], {"c": "p"})
        self.assertEqual(out["p"]["status"], "idle")

    def test_waiting_family_sorts_with_working_agents_below_idle_ones(self):
        agents = [agent("p", "idle"), agent("c", "working"), agent("other", "idle")]
        order = self.order(self.layout(agents, {"c": "p"}))
        self.assertEqual(order, ["other", "p", "c"])

    def test_working_main_agent_keeps_its_family_with_the_working_agents_even_when_its_sub_agents_are_idle(self):
        agents = [agent("p", "working"), agent("c", "idle"), agent("other", "done")]
        order = self.order(self.layout(agents, {"c": "p"}))
        self.assertEqual(order, ["other", "p", "c"])

    def test_family_with_a_sub_agent_needing_input_sorts_first(self):
        agents = [agent("p", "idle"), agent("c", "blocked"), agent("other", "idle")]
        order = self.order(self.layout(agents, {"c": "p"}))
        self.assertEqual(order, ["p", "c", "other"])


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


class WakeDueTest(DaemonTest):
    ENTRY = {"agent": "claude", "session": "s1", "cwd": "/p/x", "title": "Fix it", "wake_at": NOW - 1, "snoozed_at": NOW - 3600, "shelved_at": NOW - 3600}

    def setUp(self):
        super().setUp()
        self.resumed, self.notified = [], []
        self.prs = [{"number": 9, "state": "MERGED"}]
        self.done = {"PAY-1": True}
        scan = daemon.session_tickets.Scan()
        scan.prompts = ["Fix PAY-1"]
        patches = {
            "resume": lambda entry, focus: self.resumed.append(entry["session"]) or "t1",
            "notify": lambda title, body=None: self.notified.append((title, body)),
        }
        for name, value in patches.items():
            patcher = mock.patch.object(daemon, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target, name, value in (
            (daemon.github_prs, "session_prs", lambda cwd, session_id: self.prs),
            (daemon.jira_status, "is_done", lambda key: self.done[key]),
            (self.daemon, "session_scan", lambda rec: scan),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        lib.save_json("snoozed.json", [self.ENTRY])

    def wake(self):
        self.daemon.wake_due(NOW)

    def test_session_with_merged_prs_and_done_tickets_is_settled_instead_of_resumed(self):
        self.wake()
        self.assertEqual((self.resumed, [e["session"] for e in lib.load_json("settled.json", [])]), ([], ["s1"]))

    def test_settled_entry_says_the_snooze_settled_it_and_why(self):
        self.wake()
        entry = lib.load_json("settled.json", [])[0]
        self.assertEqual((entry["settled_by"], entry["settle_reason"], entry["shelved_at"]), ("snooze", "#9 merged, PAY-1 done", NOW))

    def test_settled_entry_leaves_the_snoozed_list(self):
        self.wake()
        self.assertEqual(lib.load_json("snoozed.json", []), [])

    def test_settled_entry_drops_its_snooze_times(self):
        self.wake()
        self.assertFalse({"wake_at", "snoozed_at"} & set(lib.load_json("settled.json", [])[0]))

    def test_closed_pr_counts_as_finished(self):
        self.prs = [{"number": 9, "state": "CLOSED"}]
        self.wake()
        self.assertEqual(self.resumed, [])

    def test_open_pr_resumes_the_session(self):
        self.prs = [{"number": 9, "state": "OPEN"}]
        self.wake()
        self.assertEqual(self.resumed, ["s1"])

    def test_ticket_not_done_resumes_the_session(self):
        self.done = {"PAY-1": False}
        self.wake()
        self.assertEqual(self.resumed, ["s1"])

    def test_session_without_prs_resumes(self):
        self.prs = []
        self.wake()
        self.assertEqual(self.resumed, ["s1"])

    def test_failed_lookup_resumes_the_session(self):
        with mock.patch.object(daemon.github_prs, "session_prs", side_effect=RuntimeError("gh down")):
            self.wake()
        self.assertEqual(self.resumed, ["s1"])

    def test_entry_not_due_yet_is_not_checked(self):
        lib.save_json("snoozed.json", [{**self.ENTRY, "wake_at": NOW + 60}])
        self.wake()
        self.assertEqual((self.resumed, lib.load_json("settled.json", [])), ([], []))

    def test_settling_tells_the_user(self):
        self.wake()
        self.assertEqual(self.notified[0][0], "Settled after snooze")
