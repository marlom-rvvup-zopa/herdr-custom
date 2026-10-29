import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tickets


def user(text):
    return {"type": "user", "message": {"content": text}}


def tool(name, **inputs):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": inputs}]}}


def scan_of(*entries):
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        f.writelines(json.dumps(e) + "\n" for e in entries)
    try:
        return tickets.scan(f.name)
    finally:
        os.unlink(f.name)


def roles(rows):
    return {r["key"]: r["role"] for r in rows}


class BranchTest(unittest.TestCase):
    def test_lowercase_branch_start_is_a_key(self):
        self.assertEqual(tickets.branch_tickets("pay-1251-fix-thing"), ["PAY-1251"])

    def test_key_after_a_type_prefix(self):
        self.assertEqual(tickets.branch_tickets("feature/PAY-9_x"), ["PAY-9"])

    def test_release_branch_is_not_a_ticket(self):
        self.assertEqual(tickets.branch_tickets("release-2024-10"), [])

    def test_branch_without_digits(self):
        self.assertEqual(tickets.branch_tickets("dor-skill"), [])


class ScanTest(unittest.TestCase):
    def test_slash_command_counts_as_its_arguments(self):
        found = scan_of(user("<command-message>work-on</command-message><command-name>/work-on</command-name><command-args>PAY-5</command-args>"))
        self.assertEqual(found.prompts, ["PAY-5"])

    def test_other_wrapped_messages_are_skipped(self):
        self.assertEqual(scan_of(user("<system-reminder>PAY-1</system-reminder>")).prompts, [])

    def test_meta_messages_are_skipped(self):
        self.assertEqual(scan_of({**user("PAY-1 skill text"), "isMeta": True}).prompts, [])

    def test_image_prompt_text_is_read(self):
        self.assertEqual(scan_of(user([{"type": "image"}, {"type": "text", "text": "PAY-7"}])).prompts, ["PAY-7"])

    def test_last_branch_wins(self):
        self.assertEqual(scan_of({**user("a"), "gitBranch": "main"}, {**user("b"), "gitBranch": "pay-1-x"}).branch, "pay-1-x")

    def test_jira_write_tools_are_touched_but_reads_are_not(self):
        found = scan_of(tool("mcp__jiracloud__transitionJiraIssue", issueIdOrKey="PAY-2"), tool("mcp__jiracloud__getJiraIssue", issueIdOrKey="PAY-3"))
        self.assertEqual(found.touched, ["PAY-2"])


class ClassifyTest(unittest.TestCase):
    def test_branch_pr_and_first_prompt_make_a_ticket_primary(self):
        found = scan_of({**user("start PAY-3"), "gitBranch": "pay-1-x"})
        self.assertEqual(roles(tickets.classify(found, ["PAY-2: title"])), {"PAY-1": "primary", "PAY-2": "primary", "PAY-3": "primary"})

    def test_link_in_a_later_prompt_is_only_mentioned(self):
        found = scan_of(user("hello"), user("see https://x.atlassian.net/browse/PAY-9?atlOrigin=abc"))
        self.assertEqual(roles(tickets.classify(found, [])), {"PAY-9": "mentioned"})

    def test_jira_write_makes_a_ticket_touched(self):
        found = scan_of(user("hello"), tool("mcp__jiracloud__editJiraIssue", issueIdOrKey="PAY-4"))
        self.assertEqual(roles(tickets.classify(found, [])), {"PAY-4": "touched"})

    def test_sources_list_every_signal(self):
        found = scan_of({**user("PAY-1 go"), "gitBranch": "pay-1-x"})
        [row] = tickets.classify(found, ["PAY-1: t"])
        self.assertEqual(row["sources"], ["branch", "pr", "first-prompt", "mention"])

    def test_owned_tickets_sort_before_mentions(self):
        found = scan_of(user("hello"), user("PAY-9 and PAY-1"), {**user("x"), "gitBranch": "pay-1-x"})
        self.assertEqual([r["key"] for r in tickets.classify(found, [])], ["PAY-1", "PAY-9"])

    def test_pr_title_without_a_key_adds_nothing(self):
        self.assertEqual(tickets.classify(scan_of(user("hi")), ["chore: bump deps", None]), [])


class DisplayTest(unittest.TestCase):
    def rows(self, **by_role):
        return [{"key": k, "role": role, "sources": []} for role, keys in by_role.items() for k in keys]

    def test_mentions_collapse_to_a_count_next_to_owned_tickets(self):
        self.assertEqual(tickets.display(self.rows(primary=["PAY-1"], mentioned=["PAY-8", "PAY-9"])), (["PAY-1"], 2))

    def test_touched_tickets_are_shown_with_primary(self):
        self.assertEqual(tickets.display(self.rows(primary=["PAY-1"], touched=["PAY-2"])), (["PAY-1", "PAY-2"], 0))

    def test_mentions_are_shown_when_nothing_is_owned(self):
        self.assertEqual(tickets.display(self.rows(mentioned=["PAY-9"])), (["PAY-9"], 0))

    def test_no_tickets(self):
        self.assertEqual(tickets.display([]), ([], 0))


if __name__ == "__main__":
    unittest.main()
