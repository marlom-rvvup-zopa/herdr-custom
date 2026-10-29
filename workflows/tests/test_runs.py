import os
import tempfile
import unittest
from unittest import mock

import catalog
import runs

WORKFLOW = catalog.Workflow("review", "/x/review.py", "repo", "Review")


class RunsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": os.path.join(tmp.name, "herdr.sock")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_started_run_is_running(self):
        record = runs.start(WORKFLOW, "/repo", {"a": "1"})
        self.assertEqual(runs.load(record["id"])["status"], "running")

    def test_zero_exit_means_succeeded(self):
        record = runs.start(WORKFLOW, "/repo", {})
        runs.finish(record, 0)
        self.assertEqual(runs.load(record["id"])["status"], "succeeded")

    def test_nonzero_exit_means_failed(self):
        record = runs.start(WORKFLOW, "/repo", {})
        runs.finish(record, 3)
        self.assertEqual(runs.load(record["id"])["exit_code"], 3)

    def test_dead_runner_shows_as_interrupted(self):
        record = runs.start(WORKFLOW, "/repo", {})
        with mock.patch.object(runs, "pid_alive", return_value=False):
            self.assertEqual(runs.load(record["id"])["status"], "interrupted")

    def test_list_is_newest_first(self):
        first = runs.start(WORKFLOW, "/repo", {})
        os.rename(runs.run_dir(first["id"]), runs.run_dir("20000101-000000-aaaa"))
        first["id"] = "20000101-000000-aaaa"
        runs.save(first)
        second = runs.start(WORKFLOW, "/repo", {})
        self.assertEqual([r["id"] for r in runs.list_runs()], [second["id"], "20000101-000000-aaaa"])

    def test_prune_drops_oldest_beyond_retention(self):
        with mock.patch.object(runs, "RETENTION", 1):
            old = runs.start(WORKFLOW, "/repo", {})
            os.rename(runs.run_dir(old["id"]), runs.run_dir("20000101-000000-aaaa"))
            runs.start(WORKFLOW, "/repo", {})
        self.assertNotIn("20000101-000000-aaaa", os.listdir(runs.state_dir()))


if __name__ == "__main__":
    unittest.main()
