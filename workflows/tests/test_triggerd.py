import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

import runs
import triggerd


def event(name="pane_agent_status_changed", **data):
    return {"event": name, "data": {"pane_id": "w1:p1", "workspace_id": "w1", "pane": {"cwd": "/nowhere", **data.pop("pane", {})}, **data}}


class MatchTest(unittest.TestCase):
    def test_dotted_trigger_matches_underscored_event(self):
        self.assertTrue(triggerd.matches({"event": "pane.agent_status_changed"}, event()))

    def test_different_event_does_not_match(self):
        self.assertFalse(triggerd.matches({"event": "tab.created"}, event()))

    def test_where_must_equal_event_data(self):
        trigger = {"event": "pane.agent_status_changed", "where": {"agent_status": "done"}}
        self.assertEqual([triggerd.matches(trigger, event(agent_status=s)) for s in ("done", "idle")], [True, False])

    def test_where_follows_dotted_paths(self):
        trigger = {"event": "pane.agent_status_changed", "where": {"pane.cwd": "/a"}}
        self.assertTrue(triggerd.matches(trigger, event(pane={"cwd": "/a"})))

    def test_where_on_missing_path_does_not_match(self):
        self.assertFalse(triggerd.matches({"event": "pane.agent_status_changed", "where": {"nope.x": 1}}, event()))


def snapshot(status, pane_id="w1:p1"):
    pane = {"pane_id": pane_id, "tab_id": "w1:t1", "workspace_id": "w1", "agent": "claude", "agent_status": status}
    return {"event": "pane_updated", "data": {"pane": pane}}


class StatusTrackerTest(unittest.TestCase):
    def setUp(self):
        self.tracker = triggerd.StatusTracker()

    def test_first_snapshot_only_records(self):
        self.assertEqual(self.tracker.feed(snapshot("working")), [])

    def test_unchanged_status_emits_nothing(self):
        self.tracker.feed(snapshot("working"))
        self.assertEqual(self.tracker.feed(snapshot("working")), [])

    def test_change_emits_event_with_previous_status(self):
        self.tracker.feed(snapshot("working"))
        [changed] = self.tracker.feed(snapshot("done"))
        self.assertEqual((changed["event"], changed["data"]["agent_status"], changed["data"]["previous_status"]), ("pane_agent_status_changed", "done", "working"))

    def test_closed_pane_is_forgotten(self):
        self.tracker.feed(snapshot("working"))
        self.tracker.feed({"event": "pane_closed", "data": {"pane_id": "w1:p1"}})
        self.assertEqual(self.tracker.feed(snapshot("done")), [])

    def test_other_events_pass_through(self):
        event = {"event": "tab_created", "data": {}}
        self.assertEqual(self.tracker.feed(event), [event])


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        workflows = os.path.join(tmp.name, "workflows")
        os.makedirs(workflows)
        with open(os.path.join(workflows, "morning.py"), "w") as f:
            f.write('TRIGGERS = [{"schedule": "30 9 * * 1-5", "catch_up_until": "13:00"}]\n')
        patcher = mock.patch.dict(os.environ, {"HERDR_PLUGIN_CONFIG_DIR": tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.started = []
        self.now = datetime(2026, 10, 26, 9, 30, 10)
        self.running = set()
        self.state_path = os.path.join(tmp.name, "schedule.json")
        self.scheduler = triggerd.Scheduler(start=lambda *a: self.started.append(a), clock=lambda: self.now, running=lambda: self.running, state_path=self.state_path)

    def test_starts_at_the_slot_with_a_schedule_event(self):
        self.scheduler.tick()
        name, ctx, inputs, trigger = self.started[0]
        self.assertEqual((name, trigger["event"], trigger["data"]["slot"]), ("morning", "schedule", "2026-10-26T09:30:00"))

    def test_second_tick_in_the_same_slot_does_nothing(self):
        self.scheduler.tick()
        self.scheduler.tick()
        self.assertEqual(len(self.started), 1)

    def test_fired_slot_survives_a_daemon_restart(self):
        self.scheduler.tick()
        triggerd.Scheduler(start=lambda *a: self.started.append(a), clock=lambda: self.now, running=lambda: set(), state_path=self.state_path).tick()
        self.assertEqual(len(self.started), 1)

    def test_late_wake_up_catches_up(self):
        self.now = datetime(2026, 10, 26, 11, 40)
        self.scheduler.tick()
        self.assertEqual(len(self.started), 1)

    def test_running_workflow_defers_without_consuming_the_slot(self):
        self.running = {"morning"}
        self.scheduler.tick()
        self.running = set()
        self.scheduler.tick()
        self.assertEqual(len(self.started), 1)

    def test_weekend_starts_nothing(self):
        self.now = datetime(2026, 10, 31, 9, 30, 10)
        self.scheduler.tick()
        self.assertEqual(self.started, [])


class LabelRefresherTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": os.path.join(tmp.name, "herdr.sock")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.now = 10_000.0
        self.reported = []
        self.fail_with = None

        def report(pane_id, label):
            if self.fail_with:
                raise triggerd.wf.WorkflowError(self.fail_with)
            self.reported.append((pane_id, label))

        self.refresher = triggerd.LabelRefresher(report=report, clock=lambda: self.now)
        workflow = triggerd.catalog.Workflow("morning", "/x.py", "global", "Morning")
        self.record = runs.start(workflow, "/", {})
        self.record["started_at"] = self.now - 600
        runs.save(self.record)
        with open(os.path.join(runs.run_dir(self.record["id"]), triggerd.wf.PANES_FILE), "w") as f:
            f.write('["w4:p1"]')

    def test_reports_the_current_age_for_a_tagged_pane(self):
        self.refresher.tick()
        self.assertEqual(self.reported, [("w4:p1", triggerd.wf.WORKFLOW_ICON + " morning (10min ago)")])

    def test_unchanged_label_is_not_reported_again(self):
        self.refresher.tick()
        self.now += 30
        self.refresher.tick()
        self.assertEqual(len(self.reported), 1)

    def test_label_is_refreshed_when_the_minute_changes(self):
        self.refresher.tick()
        self.now += 60
        self.refresher.tick()
        self.assertIn("(11min ago)", self.reported[-1][1])

    def test_closed_pane_is_dropped_after_one_failure(self):
        self.fail_with = "pane.report_metadata: {'code': 'pane_not_found'}"
        self.refresher.tick()
        self.fail_with = None
        self.now += 120
        self.refresher.tick()
        self.assertEqual(self.reported, [])

    def test_run_older_than_a_day_is_left_alone(self):
        self.now += 90_000
        self.refresher.tick()
        self.assertEqual(self.reported, [])


class DispatcherTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        workflows = os.path.join(tmp.name, "workflows")
        os.makedirs(workflows)
        with open(os.path.join(workflows, "on-done.py"), "w") as f:
            f.write('TRIGGERS = [{"event": "pane.agent_status_changed", "where": {"agent_status": "done"}, "cooldown": 10}]\n')
        patcher = mock.patch.dict(os.environ, {"HERDR_PLUGIN_CONFIG_DIR": tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.started = []
        self.now = 100.0
        self.running = set()
        self.dispatcher = triggerd.Dispatcher(start=lambda *a: self.started.append(a), clock=lambda: self.now, running=lambda _: self.running)

    def test_matching_event_starts_the_workflow_with_event_context(self):
        self.dispatcher.handle(event(agent_status="done"))
        name, ctx, inputs, trigger = self.started[0]
        self.assertEqual((name, ctx["pane_id"], trigger["event"]), ("on-done", "w1:p1", "pane_agent_status_changed"))

    def test_non_matching_event_starts_nothing(self):
        self.dispatcher.handle(event(agent_status="working"))
        self.assertEqual(self.started, [])

    def test_cooldown_blocks_a_second_start(self):
        self.dispatcher.handle(event(agent_status="done"))
        self.now += 5
        self.dispatcher.handle(event(agent_status="done"))
        self.assertEqual(len(self.started), 1)

    def test_start_allowed_after_cooldown(self):
        self.dispatcher.handle(event(agent_status="done"))
        self.now += 11
        self.dispatcher.handle(event(agent_status="done"))
        self.assertEqual(len(self.started), 2)

    def test_still_running_workflow_is_not_started_again(self):
        self.running = {"on-done"}
        self.dispatcher.handle(event(agent_status="done"))
        self.assertEqual(self.started, [])


if __name__ == "__main__":
    unittest.main()
