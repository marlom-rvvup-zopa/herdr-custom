import os
import tempfile
import unittest
from unittest import mock

import wf


class FakeHerdr:
    def __init__(self, replies=None):
        self.calls = []
        self.replies = replies or {}

    def __call__(self, method, params=None, timeout=10.0):
        self.calls.append((method, params))
        return self.replies.get(method, {})

    def methods(self):
        return [m for m, _ in self.calls]


class LabelTextTest(unittest.TestCase):
    def test_age_text_units(self):
        self.assertEqual([wf.age_text(s) for s in (5, 59, 60, 600, 3600, 7300, 86400 * 2)], ["just now", "just now", "1min ago", "10min ago", "1h ago", "2h ago", "2d ago"])

    def test_label_has_icon_name_and_age(self):
        self.assertEqual(wf.workflow_label("morning", 1000, 1600), wf.WORKFLOW_ICON + " morning (10min ago)")


class VerdictTest(unittest.TestCase):
    def test_final_line_matching_a_token_is_the_verdict(self):
        self.assertEqual(wf.parse_verdict("looks fine\n\nAPPROVE\n", ["APPROVE", "REJECT"]), "APPROVE")

    def test_token_outside_the_final_line_fails(self):
        with self.assertRaises(wf.WorkflowError):
            wf.parse_verdict("APPROVE\nbut wait", ["APPROVE", "REJECT"])

    def test_empty_response_fails(self):
        with self.assertRaises(wf.WorkflowError):
            wf.parse_verdict("", ["APPROVE"])

    def test_prompt_names_result_file_and_tokens(self):
        prompt = wf.build_prompt("Review.", "/r/agent-1.md", ["APPROVE", "REJECT"])
        self.assertIn("/r/agent-1.md", prompt)
        self.assertIn("APPROVE, REJECT", prompt)


class InputTest(unittest.TestCase):
    def test_missing_input_without_default_names_the_flag(self):
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(wf.WorkflowError, "--input branch"):
            wf.input("branch")

    def test_input_comes_from_environment(self):
        with mock.patch.dict(os.environ, {"HWF_INPUT_branch": "main"}):
            self.assertEqual(wf.input("branch"), "main")

    def test_event_is_none_for_manual_runs(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(wf.event())


class AgentTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = {"HWF_RUN_ID": "20260101-000000-ab12", "HWF_RUN_DIR": tmp.name, "HWF_WORKFLOW": "morning", "HWF_WORKSPACE_ID": "w1", "HWF_PANE_ID": "w1:p1", "HWF_CWD": "/repo"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fake = FakeHerdr({
            "tab.create": {"root_pane": {"pane_id": "w1:p9"}, "tab": {"tab_id": "w1:t3"}},
            "pane.split": {"pane": {"pane_id": "w1:p8"}},
            "agent.read": {"read": {"text": "screen answer\nAPPROVE"}},
        })
        request = mock.patch.object(wf, "request", self.fake)
        request.start()
        self.addCleanup(request.stop)

    def test_starts_waits_then_prompts_in_that_order(self):
        wf.agent("claude", "hi")
        self.assertEqual(self.fake.methods(), ["tab.create", "agent.start", "pane.report_metadata", "agent.wait", "agent.prompt", "agent.read", "agent.read"])

    def test_falls_back_to_screen_when_no_result_file(self):
        self.assertEqual(wf.agent("claude", "hi", verdict=["APPROVE"]).verdict, "APPROVE")

    def test_reads_result_file_when_agent_wrote_it(self):
        with open(os.path.join(os.environ["HWF_RUN_DIR"], "agent-%d.md" % (wf._agent_count + 1)), "w") as f:
            f.write("from file")
        self.assertEqual(wf.agent("claude", "hi").response, "from file")

    def test_beside_splits_the_invoking_pane(self):
        wf.agent("claude", "hi", where="beside")
        self.assertEqual(self.fake.calls[0][1]["target_pane_id"], "w1:p1")

    def test_close_on_success_closes_the_created_tab(self):
        wf.agent("claude", "hi", close_on_success=True)
        self.assertEqual(self.fake.calls[-1], ("tab.close", {"tab_id": "w1:t3"}))

    def test_target_prompts_existing_agent_without_opening_a_pane(self):
        wf.agent("claude", "hi", target="reviewer")
        self.assertEqual(self.fake.methods(), ["agent.prompt", "agent.read", "agent.read"])

    def test_bad_verdict_token_fails_before_any_api_call(self):
        with self.assertRaises(wf.WorkflowError):
            wf.agent("claude", "hi", verdict=["approve"])
        self.assertEqual(self.fake.calls, [])

    def test_interrupted_agent_fails_instead_of_returning_a_response(self):
        self.fake.replies["agent.read"] = {"read": {"text": "Starting\n  Interrupted \u00b7 What should Claude do instead?"}}
        with self.assertRaisesRegex(wf.WorkflowError, "interrupted"):
            wf.agent("claude", "hi")

    def test_interrupt_check_is_skipped_for_agents_without_a_known_marker(self):
        self.fake.replies["agent.read"] = {"read": {"text": "Interrupted \u00b7 x"}}
        self.assertEqual(wf.agent("codex", "hi").response, "Interrupted \u00b7 x")

    def test_new_agent_pane_is_tagged_with_the_workflow_name(self):
        wf.agent("claude", "hi")
        params = next(p for m, p in self.fake.calls if m == "pane.report_metadata")
        self.assertEqual((params["pane_id"], params["tokens"], params["source"]), ("w1:p9", {"workflow": wf.WORKFLOW_ICON + " morning (just now)"}, "marlom.workflows"))

    def test_tagged_pane_is_recorded_for_label_refresh(self):
        wf.agent("claude", "hi")
        with open(os.path.join(os.environ["HWF_RUN_DIR"], wf.PANES_FILE)) as f:
            self.assertEqual(f.read(), '["w1:p9"]')

    def test_label_age_counts_from_the_run_start_not_the_agent_start(self):
        with mock.patch.dict(os.environ, {"HWF_RUN_STARTED": str(wf.time.time() - 600)}):
            wf.agent("claude", "hi")
        params = next(p for m, p in self.fake.calls if m == "pane.report_metadata")
        self.assertIn("(10min ago)", params["tokens"]["workflow"])

    def test_label_failure_does_not_fail_the_run(self):
        original = self.fake.__call__

        def failing(method, params=None, timeout=10.0):
            if method == "pane.report_metadata":
                raise wf.WorkflowError("nope")
            return original(method, params, timeout)

        with mock.patch.object(wf, "request", failing):
            self.assertEqual(wf.agent("claude", "hi").pane_id, "w1:p9")

    def test_no_label_for_manual_scripts_outside_a_run(self):
        with mock.patch.dict(os.environ, {"HWF_WORKFLOW": ""}):
            wf.agent("claude", "hi")
        self.assertNotIn("pane.report_metadata", self.fake.methods())

    def test_target_agent_is_not_relabelled(self):
        wf.agent("claude", "hi", target="reviewer")
        self.assertNotIn("pane.report_metadata", self.fake.methods())

    def test_raw_prompt_is_sent_unchanged(self):
        wf.agent("claude", "/renovate-pr-triage org/repo", raw=True)
        prompt = next(p for m, p in self.fake.calls if m == "agent.prompt")
        self.assertEqual(prompt["text"], "/renovate-pr-triage org/repo")

    def test_raw_with_verdict_fails(self):
        with self.assertRaises(wf.WorkflowError):
            wf.agent("claude", "hi", raw=True, verdict=["OK"])

    def test_workspace_id_overrides_the_invoking_workspace(self):
        wf.agent("claude", "hi", workspace_id="w4")
        self.assertEqual(self.fake.calls[0][1]["workspace_id"], "w4")

    def test_find_workspace_requires_exactly_one_match(self):
        self.fake.replies["workspace.list"] = {"workspaces": [{"workspace_id": "w4", "label": "[3] pos-launchpad"}, {"workspace_id": "w5", "label": "frontend-monorepo"}]}
        self.assertEqual(wf.find_workspace("pos-launchpad"), "w4")
        with self.assertRaisesRegex(wf.WorkflowError, "found 0"):
            wf.find_workspace("nope")

    def test_unknown_placement_fails(self):
        with self.assertRaisesRegex(wf.WorkflowError, "where must be"):
            wf.agent("claude", "hi", where="floating")


if __name__ == "__main__":
    unittest.main()
