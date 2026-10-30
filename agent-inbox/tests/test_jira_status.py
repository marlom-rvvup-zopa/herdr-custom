import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import jira_status


OURS = "✓ Authenticated\n  Site: zopapos.atlassian.net\n"


def completed(returncode, stdout="", stderr=""):
    return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)


class LookupTest(unittest.TestCase):
    def setUp(self):
        jira_status._login_started = 0.0

    def test_a_missing_ticket_is_not_one_of_ours(self):
        runs = [completed(1, stderr="✗ Error: Issue does not exist or you do not have permission to see it."), completed(0, stdout=OURS)]
        with mock.patch.object(jira_status.subprocess, "run", side_effect=runs):
            with self.assertRaises(jira_status.TicketNotFound):
                jira_status.is_done("VITE-21802694")

    def test_a_missing_ticket_on_another_site_asks_for_a_login_instead(self):
        runs = [completed(1, stderr="✗ Error: Issue does not exist or you do not have permission to see it."), completed(0, stdout="Site: dividebuy.atlassian.net")]
        with mock.patch.object(jira_status.subprocess, "run", side_effect=runs):
            with self.assertRaises(jira_status.LoginRequired):
                jira_status.is_done("PAY-1296")

    def test_a_done_ticket_is_done(self):
        out = '{"fields": {"status": {"statusCategory": {"key": "done"}}}}'
        with mock.patch.object(jira_status.subprocess, "run", return_value=completed(0, stdout=out)):
            self.assertTrue(jira_status.is_done("PAY-1"))

    def test_a_failed_lookup_while_logged_out_asks_for_a_login(self):
        with mock.patch.object(jira_status.subprocess, "run", return_value=completed(1, stderr="failed")):
            with self.assertRaises(jira_status.LoginRequired):
                jira_status.is_done("PAY-1")

    def test_a_failed_lookup_while_logged_in_is_an_ordinary_error(self):
        runs = [completed(1, stderr="failed"), completed(0, stdout=OURS)]
        with mock.patch.object(jira_status.subprocess, "run", side_effect=runs):
            with self.assertRaises(RuntimeError) as raised:
                jira_status.is_done("PAY-1")
        self.assertNotIsInstance(raised.exception, (jira_status.LoginRequired, jira_status.TicketNotFound))

    def test_project_keys_are_sorted(self):
        out = '[{"key": "PAY", "name": "x"}, {"key": "COTO"}]'
        runs = [completed(0, stdout=out), completed(0, stdout=OURS)]
        with mock.patch.object(jira_status.subprocess, "run", side_effect=runs):
            self.assertEqual(jira_status.project_keys(), ["COTO", "PAY"])

    def test_the_projects_of_another_site_are_not_taken(self):
        runs = [completed(0, stdout='[{"key": "DA"}]'), completed(0, stdout="Site: dividebuy.atlassian.net")]
        with mock.patch.object(jira_status.subprocess, "run", side_effect=runs):
            with self.assertRaises(jira_status.LoginRequired):
                jira_status.project_keys()


class LoginTest(unittest.TestCase):
    def setUp(self):
        jira_status._login_started = 0.0

    def test_the_login_opens_in_a_popup(self):
        with mock.patch.object(jira_status.subprocess, "run") as run:
            self.assertTrue(jira_status.start_login())
        self.assertIn("jira-login", run.call_args.args[0])

    def test_the_login_is_not_repeated_straight_away(self):
        with mock.patch.object(jira_status.subprocess, "run") as run:
            self.assertEqual([jira_status.start_login(), jira_status.start_login()], [True, False])
        self.assertEqual(run.call_count, 1)

    def test_the_login_is_offered_again_after_the_retry_interval(self):
        with mock.patch.object(jira_status.subprocess, "run"):
            jira_status.start_login()
            jira_status._login_started -= jira_status.LOGIN_RETRY_SECS + 1
            self.assertTrue(jira_status.start_login())


if __name__ == "__main__":
    unittest.main()
