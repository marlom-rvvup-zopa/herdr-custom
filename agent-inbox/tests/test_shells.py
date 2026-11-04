import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import shells

CLAUDE_SHELL = "/bin/zsh -c source /Users/me/.claude/shell-snapshots/snapshot-zsh-1.sh 2>/dev/null || true && eval '/tmp/watch-ci.sh repo 1'"


class ParentsWithShellsTest(unittest.TestCase):
    def parents(self, *lines):
        return shells.parents_with_shells("\n".join(lines))

    def test_claude_shell_marks_its_parent(self):
        self.assertEqual(self.parents("  92441 74565 " + CLAUDE_SHELL), {74565})

    def test_other_children_of_claude_are_ignored(self):
        self.assertEqual(self.parents("  26556 74565 caffeinate -i -t 300", "  74608 74565 /opt/homebrew/bin/snyk mcp -t stdio"), set())

    def test_two_shells_of_one_claude_mark_it_once(self):
        self.assertEqual(self.parents("  1 7 " + CLAUDE_SHELL, "  2 7 " + CLAUDE_SHELL), {7})

    def test_shells_of_different_claudes_are_all_marked(self):
        self.assertEqual(self.parents("  1 7 " + CLAUDE_SHELL, "  2 8 " + CLAUDE_SHELL), {7, 8})


class ParentsWithWaitingShellsTest(unittest.TestCase):
    def waiting(self, listening, *lines):
        return shells.parents_with_waiting_shells("\n".join(lines), listening)

    def test_shell_that_serves_nothing_makes_its_parent_wait(self):
        self.assertEqual(self.waiting(set(), "  9 7 " + CLAUDE_SHELL), {7})

    def test_shell_that_listens_itself_does_not(self):
        self.assertEqual(self.waiting({9}, "  9 7 " + CLAUDE_SHELL), set())

    def test_shell_whose_grandchild_listens_does_not(self):
        self.assertEqual(self.waiting({30}, "  9 7 " + CLAUDE_SHELL, " 20 9 pnpm dev", " 30 20 node server.js"), set())

    def test_listener_elsewhere_in_the_tree_does_not_count(self):
        self.assertEqual(self.waiting({30}, "  9 7 " + CLAUDE_SHELL, " 20 9 sleep 60", " 30 8 node server.js"), {7})

    def test_one_listening_shell_does_not_hide_a_waiting_one(self):
        self.assertEqual(self.waiting({9}, "  9 7 " + CLAUDE_SHELL, " 10 7 " + CLAUDE_SHELL), {7})

    def test_parent_without_shells_never_waits(self):
        self.assertEqual(self.waiting(set(), "  26556 74565 caffeinate -i -t 300"), set())


class ListeningPidsTest(unittest.TestCase):
    def test_reads_the_pids_lsof_reports(self):
        with mock.patch.object(shells.subprocess, "run") as run:
            run.return_value.stdout = "p30\np31\n"
            self.assertEqual(shells.listening_pids(), {30, 31})

    def test_no_listener_makes_lsof_exit_1_with_no_output(self):
        with mock.patch.object(shells.subprocess, "run") as run:
            run.return_value.stdout = ""
            self.assertEqual(shells.listening_pids(), set())


class RunningTest(unittest.TestCase):
    def test_reads_the_process_table(self):
        with mock.patch.object(shells.subprocess, "run") as run:
            run.return_value.stdout = "  1 7 " + CLAUDE_SHELL
            self.assertEqual(shells.running_parents(), {7})


class WaitingParentsTest(unittest.TestCase):
    def test_combines_the_process_table_with_the_listeners(self):
        with mock.patch.object(shells.subprocess, "run") as run:
            run.side_effect = [mock.Mock(stdout="  9 7 " + CLAUDE_SHELL), mock.Mock(stdout="")]
            self.assertEqual(shells.waiting_parents(), {7})
