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


class RunningTest(unittest.TestCase):
    def test_reads_the_process_table(self):
        with mock.patch.object(shells.subprocess, "run") as run:
            run.return_value.stdout = "  1 7 " + CLAUDE_SHELL
            self.assertEqual(shells.running_parents(), {7})
