import os
import tempfile
import unittest
from unittest import mock

import catalog


def write(directory, filename, body):
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, filename)
    with open(path, "w") as f:
        f.write(body)
    return path


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = os.path.join(self.tmp.name, "repo")
        self.global_dir = os.path.join(self.tmp.name, "config")
        os.makedirs(self.repo)
        patcher = mock.patch.dict(os.environ, {"HERDR_PLUGIN_CONFIG_DIR": self.global_dir})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo_workflows = os.path.join(self.repo, ".herdr", "workflows")

    def test_reads_metadata_without_running_the_script(self):
        path = write(self.repo_workflows, "review.py", '"""Review the diff.\n\nMore."""\nTITLE = "Review"\nTRIGGERS = [{"event": "pane.created"}]\nraise SystemExit(7)\n')
        title, description, triggers = catalog.read_metadata(path)
        self.assertEqual((title, description, triggers), ("Review", "Review the diff.", [{"event": "pane.created"}]))

    def test_title_defaults_to_the_file_name(self):
        write(self.repo_workflows, "branch-check.py", "pass\n")
        self.assertEqual(catalog.discover(self.repo)[0].title, "Branch Check")

    def test_repo_workflow_shadows_global_one(self):
        write(self.repo_workflows, "review.py", "pass\n")
        write(os.path.join(self.global_dir, "workflows"), "review.py", "pass\n")
        self.assertEqual([(w.name, w.scope) for w in catalog.discover(self.repo)], [("review", "repo")])

    def test_ignores_files_with_invalid_names(self):
        write(self.repo_workflows, "Bad Name.py", "pass\n")
        write(self.repo_workflows, "notes.md", "x")
        self.assertEqual(catalog.discover(self.repo), [])

    def test_syntax_error_is_reported_on_the_workflow(self):
        write(self.repo_workflows, "broken.py", "def (\n")
        self.assertIn("invalid syntax", catalog.discover(self.repo)[0].error)

    def test_non_literal_triggers_are_reported_on_the_workflow(self):
        write(self.repo_workflows, "dynamic.py", "TRIGGERS = [make()]\n")
        self.assertNotEqual(catalog.discover(self.repo)[0].error, "")

    def test_trigger_with_both_event_and_schedule_is_reported(self):
        write(self.repo_workflows, "both.py", 'TRIGGERS = [{"event": "tab.created", "schedule": "* * * * *"}]\n')
        self.assertIn("exactly one", catalog.discover(self.repo)[0].error)

    def test_invalid_cron_is_reported(self):
        write(self.repo_workflows, "cron.py", 'TRIGGERS = [{"schedule": "99 * * * *"}]\n')
        self.assertIn("invalid cron field", catalog.discover(self.repo)[0].error)

    def test_bad_catch_up_time_is_reported(self):
        write(self.repo_workflows, "late.py", 'TRIGGERS = [{"schedule": "30 9 * * *", "catch_up_until": "25:00"}]\n')
        self.assertIn("HH:MM", catalog.discover(self.repo)[0].error)

    def test_discover_global_ignores_repo_workflows(self):
        write(self.repo_workflows, "mine.py", "pass\n")
        write(os.path.join(self.global_dir, "workflows"), "shared.py", "pass\n")
        self.assertEqual([w.name for w in catalog.discover_global()], ["shared"])

    def test_find_unknown_name_names_the_searched_directories(self):
        with self.assertRaisesRegex(LookupError, "nope"):
            catalog.find(self.repo, "nope")


if __name__ == "__main__":
    unittest.main()
