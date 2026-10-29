import os
import tempfile
import unittest
from unittest import mock

import catalog
import edit
import runs
import wf


class EditTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        patcher = mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": os.path.join(self.tmp, "herdr.sock")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.real = os.path.join(self.tmp, "library", "morning.py")
        os.makedirs(os.path.dirname(self.real))
        open(self.real, "w").close()
        self.link = os.path.join(self.tmp, "global", "morning.py")
        os.makedirs(os.path.dirname(self.link))
        os.symlink(self.real, self.link)
        self.workflow = catalog.Workflow("morning", self.link, "global", "Morning", triggers=[{"schedule": "30 9 * * 1-5"}])

    def test_vscode_opens_the_symlink_target_not_the_link(self):
        with mock.patch.object(edit.os.path, "exists", return_value=False), mock.patch.object(edit.shutil, "which", return_value="/usr/bin/code"):
            command = edit.vscode_command(self.workflow)
        self.assertEqual((command[0], command[-1]), ("/usr/bin/code", os.path.realpath(self.real)))

    def test_vscode_falls_back_to_open_when_no_cli(self):
        with mock.patch.object(edit.os.path, "exists", return_value=False), mock.patch.object(edit.shutil, "which", return_value=None):
            self.assertEqual(edit.vscode_command(self.workflow)[:3], ["open", "-a", "Visual Studio Code"])

    def test_prompt_names_skill_real_file_symlink_and_triggers(self):
        prompt = edit.context_prompt(self.workflow)
        self.assertEqual([x in prompt for x in ("herdr-workflows skill", os.path.realpath(self.real), "is a symlink", "30 9 * * 1-5", "never run")], [True] * 5)

    def test_prompt_asks_before_changing_anything(self):
        self.assertIn("ask me what to change", edit.context_prompt(self.workflow))

    def test_prompt_points_to_the_last_run_log(self):
        record = runs.start(self.workflow, self.tmp, {})
        runs.finish(record, 1)
        prompt = edit.context_prompt(self.workflow)
        self.assertEqual(("failed" in prompt, record["id"] in prompt), (True, True))

    def test_claude_session_is_started_then_prompted_without_waiting(self):
        calls = []

        def fake_request(method, params=None, timeout=10.0):
            calls.append((method, params))
            return {"root_pane": {"pane_id": "w1:p9"}, "tab": {"tab_id": "w1:t3"}} if method == "tab.create" else {}

        with mock.patch.object(wf, "request", fake_request):
            name = edit.edit_with_claude(self.workflow, "w1")
        prompt = calls[-1]
        self.assertEqual(([m for m, _ in calls], "wait" in prompt[1], prompt[1]["target"] == name), (["tab.create", "agent.start", "agent.wait", "agent.prompt"], False, True))
        self.assertEqual(calls[0][1]["workspace_id"], "w1")


if __name__ == "__main__":
    unittest.main()
