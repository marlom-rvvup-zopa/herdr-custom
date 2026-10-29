import unittest
from datetime import datetime

import catalog
import rows

NOW = datetime(2026, 10, 29, 22, 0)


def text(segments):
    return "".join(t for t, _ in segments)


def workflow(**kwargs):
    return catalog.Workflow(kwargs.pop("name", "demo"), "/x/demo.py", "global", "Demo", **kwargs)


def record(status, minutes_ago=5, name="demo", **extra):
    started = NOW.timestamp() - minutes_ago * 60
    return {"id": "20261029-215500-ab12", "workflow": name, "status": status, "started_at": started, "ended_at": started + 125, "trigger": None, **extra}


class WorkflowRowTest(unittest.TestCase):
    def test_never_run_manual_workflow(self):
        row = rows.workflow_row(workflow(description="Does a thing"), None, NOW)
        self.assertEqual((rows.ICON_PLAY in text(row), "never" in text(row), "Does a thing" in text(row)), (True, True, True))

    def test_last_run_shows_status_color_and_age(self):
        row = rows.workflow_row(workflow(), record("failed", 125), NOW)
        self.assertEqual(([c for t, c in row if rows.ICON_FAIL in t], "2h ago" in text(row)), (["fail"], True))

    def test_scheduled_workflow_shows_next_slot_and_clock_icon(self):
        trigger = [{"schedule": "30 9 * * 1-5"}]
        row = rows.workflow_row(workflow(triggers=trigger), None, NOW)
        self.assertEqual((rows.ICON_CLOCK in text(row), "today" in text(row), "tomorrow 09:30" in text(row)), (True, False, True))

    def test_friday_night_schedule_points_to_monday(self):
        row = rows.workflow_row(workflow(triggers=[{"schedule": "30 9 * * 1-5"}]), None, datetime(2026, 10, 30, 22, 0))
        self.assertIn("Mon 09:30", text(row))

    def test_event_workflow_shows_event_name(self):
        row = rows.workflow_row(workflow(triggers=[{"event": "pane.agent_status_changed"}]), None, NOW)
        self.assertIn("on agent_status_changed", text(row))

    def test_invalid_workflow_shows_its_error(self):
        row = rows.workflow_row(workflow(error="bad cron"), None, NOW)
        self.assertIn("INVALID: bad cron", text(row))

    def test_last_runs_keeps_the_newest_per_workflow(self):
        newest, older = record("succeeded", 1), record("failed", 60)
        self.assertEqual(rows.last_runs([newest, older, record("failed", 5, "other")])["demo"], newest)


class ColumnAlignmentTest(unittest.TestCase):
    def test_header_labels_sit_above_the_cell_text(self):
        header = text(rows.header())
        scheduled = text(rows.workflow_row(workflow(description="Desc", triggers=[{"schedule": "30 9 * * 1-5"}]), record("succeeded", 125), NOW))
        self.assertEqual(
            (header.index("LAST RUN"), header.index("NEXT RUN"), header.index("DESCRIPTION")),
            (scheduled.index("2h ago"), scheduled.index("tomorrow"), scheduled.index("Desc")),
        )

    def test_placeholders_for_never_and_no_schedule_use_the_same_columns(self):
        header = text(rows.header())
        manual = text(rows.workflow_row(workflow(description="Desc"), None, NOW))
        self.assertEqual(
            (header.index("LAST RUN"), header.index("NEXT RUN"), header.index("DESCRIPTION")),
            (manual.index("never"), manual.index("- "), manual.index("Desc")),
        )

    def test_name_column_starts_under_the_header_label(self):
        self.assertEqual(text(rows.header()).index("WORKFLOW"), text(rows.workflow_row(workflow(name="demo"), None, NOW)).index("demo"))


class RunRowTest(unittest.TestCase):
    def test_run_row_has_status_workflow_age_and_duration(self):
        row = text(rows.run_row(record("succeeded", 3), NOW))
        self.assertEqual([x in row for x in (rows.ICON_OK, "demo", "3m ago", "2m 05s", "manual", "20261029-215500-ab12")], [True] * 6)

    def test_running_run_counts_duration_up_to_now(self):
        row = text(rows.run_row(record("running", 3, ended_at=None), NOW))
        self.assertIn("3m 00s", row)

    def test_trigger_labels(self):
        labels = [rows.trigger_label(record("succeeded", trigger=t)) for t in (None, {"event": "schedule"}, {"event": "tab_created"})]
        self.assertEqual(labels, ["manual", "schedule", "on tab.created"])

    def test_hour_long_duration(self):
        self.assertEqual(rows.duration(record("succeeded", ended_at=NOW.timestamp() - 5 * 60 + 3725), NOW), "1h 02m")

    def test_run_header_labels_sit_above_the_cell_text(self):
        header = text(rows.run_header())
        row = text(rows.run_row(record("succeeded", 3, trigger={"event": "schedule"}), NOW))
        self.assertEqual(
            [header.index(x) for x in ("STATUS", "WORKFLOW", "STARTED", "DURATION", "TRIGGER", "RUN ID")],
            [row.index(x) for x in ("succeeded", "demo", "3m ago", "2m 05s", "schedule", "20261029")],
        )


if __name__ == "__main__":
    unittest.main()
