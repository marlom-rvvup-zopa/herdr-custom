import json
import os
import tempfile
import unittest
from unittest import mock

import catalog
import hwf
import runs


class InvocationContextTest(unittest.TestCase):
    def test_action_context_supplies_pane_and_cwd(self):
        injected = json.dumps({"workspace_id": "w1", "tab_id": "w1:t1", "focused_pane_id": "w1:p2", "focused_pane_cwd": "/repo"})
        with mock.patch.dict(os.environ, {"HERDR_PLUGIN_CONTEXT_JSON": injected}, clear=True):
            ctx = hwf.invocation_context()
        self.assertEqual((ctx["pane_id"], ctx["cwd"]), ("w1:p2", "/repo"))

    def test_pane_environment_wins_over_injected_context(self):
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p9", "HERDR_PLUGIN_CONTEXT_JSON": '{"focused_pane_id": "w1:p2"}'}, clear=True):
            self.assertEqual(hwf.invocation_context()["pane_id"], "w1:p9")

    def test_picker_cwd_override_wins(self):
        with mock.patch.dict(os.environ, {"HWF_INVOKE_CWD": "/repo"}, clear=True):
            self.assertEqual(hwf.invocation_context()["cwd"], "/repo")

    def test_open_picker_does_not_move_the_popup_out_of_the_plugin_dir(self):
        with mock.patch.object(hwf.subprocess, "run") as run, mock.patch.dict(os.environ, {}, clear=True):
            hwf.cmd_open_picker(None)
        self.assertNotIn("--cwd", run.call_args.args[0])

    def test_input_without_equals_is_rejected(self):
        with self.assertRaises(SystemExit):
            hwf.parse_inputs(["oops"])

    def test_input_value_may_contain_equals(self):
        self.assertEqual(hwf.parse_inputs(["q=a=b"]), {"q": "a=b"})


class ExecuteTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        patcher = mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": os.path.join(self.tmp, "herdr.sock")})
        patcher.start()
        self.addCleanup(patcher.stop)
        notify = mock.patch.object(hwf.wf, "notify")
        self.notify = notify.start()
        self.addCleanup(notify.stop)
        self.ctx = {"workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p1", "cwd": self.tmp}

    def last_log(self):
        with open(os.path.join(runs.run_dir(runs.list_runs()[0]["id"]), "output.log")) as f:
            return f.read()

    def workflow(self, body):
        path = os.path.join(self.tmp, "demo.py")
        with open(path, "w") as f:
            f.write(body)
        return catalog.Workflow("demo", path, "repo", "Demo")

    def test_script_can_import_wf_and_read_inputs_and_context(self):
        workflow = self.workflow("import wf\nprint(wf.input('who'), wf.context()['pane_id'])\n")
        self.assertEqual(hwf.execute(workflow, self.ctx, {"who": "bob"}), 0)
        log = self.last_log()
        self.assertEqual(log.strip(), "bob w1:p1")

    def test_failing_script_is_recorded_and_notified(self):
        code = hwf.execute(self.workflow("raise SystemExit(3)\n"), self.ctx, {})
        self.assertEqual((code, runs.list_runs()[0]["status"]), (3, "failed"))
        self.assertIn("failed", self.notify.call_args.args[1])

    def test_trigger_event_reaches_the_script(self):
        workflow = self.workflow("import wf\nprint(wf.event()['event'])\n")
        hwf.execute(workflow, self.ctx, {}, {"event": "tab_created"})
        log = self.last_log()
        self.assertEqual(log.strip(), "tab_created")


if __name__ == "__main__":
    unittest.main()
