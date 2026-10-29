import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import github_prs

URL = "https://github.com/rvvup-internal/plugin-woocommerce/pull/%d"


def call(tool_id, command):
    return {"message": {"content": [{"type": "tool_use", "id": tool_id, "input": {"command": command}}]}}


def result(tool_id, text):
    return {"message": {"content": [{"type": "tool_result", "tool_use_id": tool_id, "content": text}]}}


class WorkedPrUrlsTest(unittest.TestCase):
    def urls(self, *entries):
        with tempfile.TemporaryDirectory() as home:
            project = os.path.join(home, ".claude", "projects", "p")
            os.makedirs(project)
            with open(os.path.join(project, "s1.jsonl"), "w") as f:
                f.writelines(json.dumps(e) + "\n" for e in entries)
            with mock.patch.dict(os.environ, {"HOME": home}):
                return github_prs.worked_pr_urls("s1")

    def test_created_pr_is_found(self):
        self.assertEqual(self.urls(call("a", "gh pr create --title x"), result("a", URL % 53)), [URL % 53])

    def test_edited_pr_is_found(self):
        self.assertEqual(self.urls(call("a", "gh pr edit 54 --title x 2>&1 | tail -2"), result("a", URL % 54)), [URL % 54])

    def test_merged_and_commented_prs_are_found(self):
        entries = [call("a", "gh pr merge 5"), result("a", URL % 5), call("b", "gh pr comment 6 -b hi"), result("b", URL % 6)]
        self.assertEqual(self.urls(*entries), [URL % 5, URL % 6])

    def test_viewing_a_pr_does_not_attach_it(self):
        self.assertEqual(self.urls(call("a", "gh pr view 99 --json url"), result("a", URL % 99)), [])

    def test_unrelated_command_output_is_ignored(self):
        self.assertEqual(self.urls(call("a", "git log"), result("a", URL % 7)), [])

    def test_same_pr_touched_twice_is_listed_once(self):
        entries = [call("a", "gh pr create"), result("a", URL % 1), call("b", "gh pr edit 1"), result("b", URL % 1)]
        self.assertEqual(self.urls(*entries), [URL % 1])


if __name__ == "__main__":
    unittest.main()
