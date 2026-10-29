import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import auto_settle

NOW = 1_000_000.0
LONG_AGO = NOW - 3600
MERGED = {"number": 54, "state": "MERGED"}
ROWS = [{"key": "PAY-1035", "role": "primary", "sources": ["pr"]}]
REC = {"agent": "claude", "agent_status": "idle", "agent_session": {"kind": "id", "value": "s1"}}


def verdict(**overrides):
    args = {"rec": REC, "unseen_done": False, "last_active": LONG_AGO, "now": NOW, "in_family": False, "rows": ROWS, "prs": [MERGED], "ticket_done": {"PAY-1035": True}}
    args.update(overrides)
    return auto_settle.evaluate(**args)


class EvaluateTest(unittest.TestCase):
    def test_merged_pr_and_done_ticket_on_a_quiet_idle_session_is_ready(self):
        result = verdict()
        self.assertEqual((result.ready, result.reason), (True, "#54 merged, PAY-1035 done"))

    def test_open_pr_blocks(self):
        self.assertEqual(verdict(prs=[MERGED, {"number": 55, "state": "OPEN"}]).reason, "PR #55 is open")

    def test_pr_closed_without_merging_blocks(self):
        self.assertIn("closed without merging", verdict(prs=[{"number": 9, "state": "CLOSED"}]).reason)

    def test_ticket_not_done_blocks(self):
        self.assertEqual(verdict(ticket_done={"PAY-1035": False}).reason, "PAY-1035 is not done in Jira")

    def test_unknown_ticket_status_blocks(self):
        self.assertIn("not looked up", verdict(ticket_done={}).reason)

    def test_no_pr_blocks(self):
        self.assertEqual(verdict(prs=[]).reason, "no PR known")

    def test_mentioned_ticket_is_not_enough(self):
        self.assertEqual(verdict(rows=[{"key": "PAY-9", "role": "mentioned", "sources": ["mention"]}]).reason, "no ticket owned by the session")

    def test_working_agent_blocks(self):
        self.assertEqual(verdict(rec={**REC, "agent_status": "working"}).reason, "agent is working")

    def test_unseen_result_blocks(self):
        self.assertEqual(verdict(unseen_done=True).reason, "agent is showing an unseen result")

    def test_focused_agent_blocks(self):
        self.assertEqual(verdict(rec={**REC, "focused": True}).reason, "you are looking at it")

    def test_recent_activity_blocks(self):
        self.assertIn("needs 30 quiet minutes", verdict(last_active=NOW - 600).reason)

    def test_sub_agent_family_blocks(self):
        self.assertEqual(verdict(in_family=True).reason, "part of a sub agent family")

    def test_non_claude_agent_blocks(self):
        self.assertIn("can be resumed", verdict(rec={**REC, "agent": "codex"}).reason)


if __name__ == "__main__":
    unittest.main()
