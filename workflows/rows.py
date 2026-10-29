"""Picker rows as colored segments. Pure functions so the layout is testable without curses."""

from datetime import datetime

import schedule

# Nerd Font glyphs, by code point so the source stays ASCII.
ICON_OK = chr(0xF058)
ICON_FAIL = chr(0xF057)
ICON_RUNNING = chr(0xF110)
ICON_WARN = chr(0xF071)
ICON_CLOCK = chr(0xF017)
ICON_BOLT = chr(0xF0E7)
ICON_PLAY = chr(0xF04B)
ICON_NEVER = chr(0xF10C)
ICON_NEXT = chr(0xF073)

STATUS = {
    "succeeded": (ICON_OK, "ok"),
    "failed": (ICON_FAIL, "fail"),
    "running": (ICON_RUNNING, "run"),
    "interrupted": (ICON_WARN, "warn"),
}
NAME_WIDTH = 26
LAST_WIDTH = 12
NEXT_WIDTH = 20
# Every cell is "<icon> <text>": ICON_SLOT cells before the text, and GAP cells between columns.
ICON_SLOT = 2
GAP = 2


def ago(timestamp, now):
    secs = max(int((now - datetime.fromtimestamp(timestamp)).total_seconds()), 0)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return "%d%s ago" % (secs // size, unit)
    return "%ds ago" % secs


def day_label(slot, now):
    days = (slot.date() - now.date()).days
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    return slot.strftime("%a")


def next_run(workflow, now):
    """(icon, text) for the next scheduled start, 'on <event>' for event triggers, or None for manual workflows."""
    slots = [schedule.next_slot(t["schedule"], now) for t in workflow.triggers if "schedule" in t]
    slots = [slot for slot in slots if slot]
    if slots:
        slot = min(slots)
        return ICON_NEXT, "%s %s" % (day_label(slot, now), slot.strftime("%H:%M"))
    events = [t["event"] for t in workflow.triggers if "event" in t]
    if events:
        return ICON_BOLT, "on " + events[0].split(".")[-1]
    return None


def last_runs(records):
    """Latest record per workflow name. records is newest first."""
    latest = {}
    for record in records:
        latest.setdefault(record["workflow"], record)
    return latest


def header():
    icon, gap = " " * ICON_SLOT, " " * GAP
    return [(" " * 3 + "WORKFLOW".ljust(NAME_WIDTH), "dim"), (icon + "LAST RUN".ljust(LAST_WIDTH) + gap, "dim"), (icon + "NEXT RUN".ljust(NEXT_WIDTH) + gap, "dim"), ("DESCRIPTION", "dim")]


def workflow_row(workflow, last, now):
    kind = ICON_CLOCK if any("schedule" in t for t in workflow.triggers) else ICON_BOLT if workflow.triggers else ICON_PLAY
    name_color = "fail" if workflow.error else "plain"
    segments = [(" %s " % kind, "accent"), (workflow.name.ljust(NAME_WIDTH), name_color)]
    if last:
        icon, color = STATUS.get(last["status"], (ICON_WARN, "warn"))
        segments += [(icon + " ", color), (ago(last["started_at"], now).ljust(LAST_WIDTH), "plain")]
    else:
        segments += [(ICON_NEVER + " ", "dim"), ("never".ljust(LAST_WIDTH), "dim")]
    upcoming = next_run(workflow, now)
    segments.append((" " * GAP, "plain"))
    if upcoming:
        segments += [(upcoming[0] + " ", "accent"), (upcoming[1].ljust(NEXT_WIDTH), "plain")]
    else:
        segments += [(" " * ICON_SLOT, "plain"), ("-".ljust(NEXT_WIDTH), "dim")]
    segments.append((" " * GAP, "plain"))
    segments.append((("INVALID: " + workflow.error) if workflow.error else workflow.description, "fail" if workflow.error else "dim"))
    return segments


STATUS_WIDTH = 13
STARTED_WIDTH = 10
DURATION_WIDTH = 10
TRIGGER_WIDTH = 22


def duration(record, now):
    end = record["ended_at"] or now.timestamp()
    secs = max(int(end - record["started_at"]), 0)
    if secs >= 3600:
        return "%dh %02dm" % (secs // 3600, secs % 3600 // 60)
    if secs >= 60:
        return "%dm %02ds" % (secs // 60, secs % 60)
    return "%ds" % secs


def trigger_label(record):
    trigger = record.get("trigger")
    if not trigger:
        return "manual"
    return "schedule" if trigger["event"] == "schedule" else "on " + trigger["event"].replace("_", ".")


def run_header():
    return [
        (" " * ICON_SLOT + " " + "STATUS".ljust(STATUS_WIDTH), "dim"),
        ("WORKFLOW".ljust(NAME_WIDTH), "dim"),
        ("STARTED".ljust(STARTED_WIDTH + GAP), "dim"),
        ("DURATION".ljust(DURATION_WIDTH + GAP), "dim"),
        ("TRIGGER".ljust(TRIGGER_WIDTH), "dim"),
        ("RUN ID", "dim"),
    ]


def run_row(record, now):
    icon, color = STATUS.get(record["status"], (ICON_WARN, "warn"))
    return [
        (" %s " % icon, color),
        (record["status"].ljust(STATUS_WIDTH), color),
        (record["workflow"].ljust(NAME_WIDTH), "plain"),
        (ago(record["started_at"], now).ljust(STARTED_WIDTH + GAP), "plain"),
        (duration(record, now).ljust(DURATION_WIDTH + GAP), "plain"),
        (trigger_label(record).ljust(TRIGGER_WIDTH), "dim"),
        (record["id"], "dim"),
    ]
