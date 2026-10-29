#!/usr/bin/env python3
"""Popup listing settled agents. enter resumes one in a new tab, d forgets it, q closes."""

import curses
import json
import os
import shlex
import subprocess
import time

from lib import RESUME_COMMANDS, load_json, save_json

HERDR = os.environ.get("HERDR_BIN_PATH", "herdr")


def herdr_json(*args):
    out = subprocess.run([HERDR, *args], check=True, capture_output=True, text=True).stdout
    return json.loads(out)["result"]


def resume(entry):
    """Reopen in the original workspace, or in a new one if it no longer exists."""
    label = entry.get("workspace_label") or os.path.basename(entry["cwd"])
    workspaces = herdr_json("workspace", "list")["workspaces"]
    workspace_id = next(
        (w["workspace_id"] for w in workspaces if w["workspace_id"] == entry["workspace_id"]),
        next((w["workspace_id"] for w in workspaces if w["label"] == label), None),
    )
    if workspace_id:
        pane_id = herdr_json("tab", "create", "--workspace", workspace_id, "--cwd", entry["cwd"], "--focus")["root_pane"]["pane_id"]
        herdr_json("workspace", "focus", workspace_id)
    else:
        pane_id = herdr_json("workspace", "create", "--cwd", entry["cwd"], "--label", label, "--focus")["root_pane"]["pane_id"]
    command = RESUME_COMMANDS[entry["agent"]].format(shlex.quote(entry["session"]))
    herdr_json("pane", "run", pane_id, command)


def ago(timestamp):
    secs = int(time.time() - timestamp)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return "%d%s" % (secs // size, unit)
    return "%ds" % secs


def draw(screen, entries, cursor):
    screen.erase()
    height, width = screen.getmaxyx()
    screen.addnstr(0, 0, "Settled agents   enter resume · d forget · q close", width - 1, curses.A_BOLD)
    if not entries:
        screen.addnstr(2, 0, "Nothing settled.", width - 1)
    for row, entry in enumerate(entries[: height - 2]):
        line = "%-4s %-45s %s" % (ago(entry["settled_at"]), entry["title"][:45], os.path.basename(entry["cwd"]))
        attr = curses.A_REVERSE if row == cursor else curses.A_NORMAL
        screen.addnstr(row + 2, 0, line, width - 1, attr)
    screen.refresh()


def run(screen):
    curses.curs_set(0)
    entries = load_json("settled.json", [])
    cursor = 0
    while True:
        draw(screen, entries, cursor)
        key = screen.getch()
        if key in (ord("q"), 27):
            return
        if key in (curses.KEY_DOWN, ord("j")) and cursor < len(entries) - 1:
            cursor += 1
        elif key in (curses.KEY_UP, ord("k")) and cursor > 0:
            cursor -= 1
        elif key in (10, 13) and entries:
            resume(entries[cursor])
            save_json("settled.json", entries[:cursor] + entries[cursor + 1:])
            return
        elif key == ord("d") and entries:
            entries.pop(cursor)
            save_json("settled.json", entries)
            cursor = min(cursor, max(len(entries) - 1, 0))


if __name__ == "__main__":
    curses.wrapper(run)
