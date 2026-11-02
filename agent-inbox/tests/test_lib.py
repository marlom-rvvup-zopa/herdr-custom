import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import lib
import worktrees

NOW = 1_000_000.0


def rec(**fields):
    return {"terminal_id": "t1", "agent_status": "idle", "agent": "claude", **fields}


class StateDirTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": os.path.join(tmp.name, "herdr.sock")})
        patcher.start()
        self.addCleanup(patcher.stop)


class StaleTest(unittest.TestCase):
    def test_idle_for_over_a_day_is_stale(self):
        self.assertTrue(lib.is_stale(rec(), NOW - lib.STALE_AFTER_SECS - 1, {}, NOW))

    def test_idle_for_under_a_day_is_not_stale(self):
        self.assertFalse(lib.is_stale(rec(), NOW - 3600, {}, NOW))

    def test_working_agent_is_never_stale(self):
        self.assertFalse(lib.is_stale(rec(agent_status="working"), 0, {}, NOW))

    def test_agent_waiting_on_the_user_is_never_stale(self):
        self.assertFalse(lib.is_stale(rec(agent_status="blocked"), 0, {}, NOW))

    def test_focused_agent_is_never_stale(self):
        self.assertFalse(lib.is_stale(rec(focused=True), 0, {}, NOW))

    def test_resumed_session_counts_from_the_resume(self):
        self.assertFalse(lib.is_stale(rec(), 0, {"t1": NOW - 60}, NOW))

    def test_transcript_write_counts_as_interaction(self):
        with mock.patch.object(lib, "transcript_mtime", return_value=NOW - 60):
            session = {"agent_session": {"kind": "id", "value": "abc"}}
            self.assertFalse(lib.is_stale(rec(**session), 0, {}, NOW))


class ShelveTest(StateDirTest):
    AGENT = {
        "agent": "claude", "agent_status": "idle", "agent_session": {"kind": "id", "value": "s1"},
        "cwd": "/p", "workspace_id": "w1", "pane_id": "w1:p1", "terminal_title_stripped": "Title",
    }

    def shelve(self, store, **extra):
        with mock.patch.object(lib, "herdr_request", return_value={"workspace": {"label": "ws"}}) as request:
            entry = lib.shelve(self.AGENT, store, **extra)
        return entry, request

    def test_shelved_session_is_stored_and_its_pane_closed(self):
        entry, request = self.shelve("snoozed", wake_at=5)
        self.assertEqual(lib.load_json("snoozed.json", [])[0]["wake_at"], 5)
        request.assert_called_with("pane.close", {"pane_id": "w1:p1"})
        self.assertEqual(entry["title"], "Title")

    def test_session_moves_between_stores(self):
        self.shelve("settled")
        self.shelve("snoozed", wake_at=5)
        self.assertEqual(lib.load_json("settled.json", []), [])

    def test_working_agent_cannot_be_shelved(self):
        with self.assertRaises(lib.CannotShelve):
            lib.shelve({**self.AGENT, "agent_status": "working"}, "settled")

    def test_unresumable_agent_cannot_be_shelved(self):
        with self.assertRaises(lib.CannotShelve):
            lib.shelve({**self.AGENT, "agent": "gemini"}, "settled")


class ResumeTest(StateDirTest):
    ENTRY = {"agent": "claude", "session": "s1", "cwd": "/p", "workspace_id": "w1", "workspace_label": "ws", "title": "T"}

    def resume(self, focus, entry=None):
        calls = []

        def cli(*args):
            calls.append(args)
            if args[:2] == ("workspace", "list"):
                return {"workspaces": [{"workspace_id": "w1", "label": "ws"}]}
            if args[:2] == ("tab", "create"):
                return {"root_pane": {"pane_id": "w1:p9", "terminal_id": "term9"}}
            return {}

        with mock.patch.object(lib, "herdr_cli", side_effect=cli):
            return lib.resume(entry or self.ENTRY, focus), calls

    def test_session_is_resumed_in_a_new_tab_of_its_workspace(self):
        _, calls = self.resume(focus=False)
        self.assertIn(("pane", "run", "w1:p9", "claude --resume s1"), calls)

    def test_resumed_terminal_is_marked_touched(self):
        terminal, _ = self.resume(focus=False)
        self.assertEqual(terminal, "term9")
        self.assertIn("term9", lib.load_json("touched.json", {}))

    def test_background_resume_does_not_take_focus(self):
        _, calls = self.resume(focus=False)
        self.assertNotIn(("workspace", "focus", "w1"), calls)

    def test_foreground_resume_focuses_the_workspace(self):
        _, calls = self.resume(focus=True)
        self.assertIn(("workspace", "focus", "w1"), calls)

    def test_removed_worktree_is_restored_before_the_tab_opens(self):
        tree = {"path": "/nowhere/feat", "git_dir": "/repo/.git", "branch": "feat", "head": "abc"}
        with mock.patch.object(worktrees, "restore") as restore:
            _, calls = self.resume(focus=False, entry={**self.ENTRY, "cwd": "/nowhere/feat/sub", "worktree": tree})
        restore.assert_called_once_with(tree)

    def test_worktree_that_still_exists_is_left_alone(self):
        tree = {"path": tempfile.gettempdir(), "git_dir": "/repo/.git", "branch": "feat", "head": "abc"}
        with mock.patch.object(worktrees, "restore") as restore:
            self.resume(focus=False, entry={**self.ENTRY, "worktree": tree})
        restore.assert_not_called()

    def test_failed_restore_opens_no_tab(self):
        tree = {"path": "/nowhere/feat", "git_dir": "/repo/.git", "branch": "feat", "head": "abc"}
        calls = []
        with mock.patch.object(worktrees, "restore", side_effect=RuntimeError("branch is checked out")), mock.patch.object(lib, "herdr_cli", side_effect=lambda *a: calls.append(a) or {}):
            with self.assertRaisesRegex(RuntimeError, "branch is checked out"):
                lib.resume({**self.ENTRY, "worktree": tree}, False)
        self.assertEqual(calls, [])


class HerdrCliTest(unittest.TestCase):
    def test_command_with_no_output_returns_an_empty_result(self):
        with mock.patch.object(lib.subprocess, "run", return_value=mock.Mock(stdout="")):
            self.assertEqual(lib.herdr_cli("pane", "run", "p", "x"), {})


class AgoTextTest(unittest.TestCase):
    def test_units(self):
        self.assertEqual([lib.ago_text(s) for s in (0, 59, 60, 3599, 3600, 90000, 200000)], ["just now", "just now", "1m ago", "59m ago", "1h ago", "1d ago", "2d ago"])


class SettleWorktreeTest(StateDirTest):
    TREE = {"path": "/trees/feat", "git_dir": "/repo/.git", "branch": "feat", "head": "abc"}

    def setUp(self):
        super().setUp()
        self.removed = []
        self.blocked = None
        self.panes = [{"pane_id": "w1:p1", "cwd": "/trees/feat", "foreground_cwd": "/trees/feat"}]
        self.tree = self.TREE
        self.remove_error = None
        for name, value in (
            ("linked_worktree", lambda cwd: self.tree),
            ("blocker", lambda info, others: self.blocked if self.blocked else (self.others.append(others) or None)),
            ("remove", self.remove),
        ):
            patcher = mock.patch.object(worktrees, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.others = []

    def remove(self, info):
        if self.remove_error:
            raise RuntimeError(self.remove_error)
        self.removed.append(info)

    def request(self, method, params, timeout=10.0):
        if method == "pane.list":
            return {"panes": self.panes}
        return {"workspace": {"label": "ws"}}

    def settle(self):
        with mock.patch.object(lib, "herdr_request", side_effect=self.request) as request:
            entry = lib.settle(ShelveTest.AGENT)
        return entry, request

    def test_settling_removes_the_worktree_the_session_runs_in(self):
        self.settle()
        self.assertEqual(self.removed, [self.TREE])

    def test_settled_entry_remembers_the_worktree_for_resume(self):
        entry, _ = self.settle()
        self.assertEqual(entry["worktree"], self.TREE)

    def test_the_sessions_own_pane_is_not_another_pane(self):
        self.settle()
        self.assertEqual(self.others, [[]])

    def test_other_panes_cwds_are_checked_against_the_worktree(self):
        self.panes.append({"pane_id": "w1:p2", "cwd": "/a", "foreground_cwd": "/trees/feat/bin"})
        self.settle()
        self.assertEqual(self.others, [["/a", "/trees/feat/bin"]])

    def test_blocked_worktree_stops_the_settle_before_anything_changes(self):
        self.blocked = "it has uncommitted changes"
        with mock.patch.object(lib, "herdr_request", side_effect=self.request) as request:
            with self.assertRaisesRegex(lib.CannotShelve, "Cannot remove worktree feat: it has uncommitted changes"):
                lib.settle(ShelveTest.AGENT)
        self.assertEqual((self.removed, lib.load_json("settled.json", []), [c for c in request.call_args_list if c.args[0] == "pane.close"]), ([], [], []))

    def test_failed_removal_stops_the_settle_before_the_pane_closes(self):
        self.remove_error = "locked"
        with mock.patch.object(lib, "herdr_request", side_effect=self.request) as request:
            with self.assertRaisesRegex(lib.CannotShelve, "Cannot remove worktree feat: locked"):
                lib.settle(ShelveTest.AGENT)
        self.assertEqual((lib.load_json("settled.json", []), [c for c in request.call_args_list if c.args[0] == "pane.close"]), ([], []))

    def test_session_outside_a_worktree_settles_as_before(self):
        self.tree = None
        entry, _ = self.settle()
        self.assertEqual((self.removed, "worktree" in entry), ([], False))

    def test_other_ways_of_shelving_leave_the_worktree_alone(self):
        with mock.patch.object(lib, "herdr_request", side_effect=self.request):
            lib.shelve(ShelveTest.AGENT, "settled", settled_by="auto")
        self.assertEqual(self.removed, [])


if __name__ == "__main__":
    unittest.main()
