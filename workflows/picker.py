#!/usr/bin/env python3
"""Popup to run workflows and browse runs. tab switches list, enter runs/opens, q closes."""

import curses
import os
import subprocess
from datetime import datetime

import catalog
import hwf
import rows
import runs

TABS = ("workflows", "runs")
COLORS = {"ok": curses.COLOR_GREEN, "fail": curses.COLOR_RED, "run": curses.COLOR_YELLOW, "warn": curses.COLOR_YELLOW, "accent": curses.COLOR_CYAN}
REFRESH_MS = 2000


def init_colors():
    curses.start_color()
    curses.use_default_colors()
    pairs = {}
    for index, (name, color) in enumerate(COLORS.items(), start=1):
        curses.init_pair(index, color, -1)
        pairs[name] = curses.color_pair(index)
    pairs["dim"] = curses.A_DIM
    pairs["plain"] = curses.A_NORMAL
    return pairs


def empty_hint(tab, cwd):
    if tab == 1:
        return ["No runs yet."]
    return ["No workflows found. Put <name>.py files in:"] + ["  " + directory for _, directory in catalog.workflow_dirs(cwd)]


def trigger_summary(workflow):
    parts = [t["schedule"] if "schedule" in t else "on " + t["event"] for t in workflow.triggers]
    return "  ".join(parts)


def put(screen, y, segments, width, pairs, selected=False):
    x = 0
    for text, color in segments:
        if x >= width - 1:
            break
        attr = curses.A_REVERSE if selected else pairs[color]
        screen.addnstr(y, x, text, width - 1 - x, attr)
        x += len(text)


def draw(screen, pairs, tab, segment_rows, cursor, footer, hint):
    screen.erase()
    height, width = screen.getmaxyx()
    title = [("  ".join(("[%s]" if i == tab else " %s ") % name for i, name in enumerate(TABS)), "accent"), ("   enter run · e edit · tab switch · q close", "dim")]
    put(screen, 0, title, width, pairs)
    if not segment_rows:
        for y, text in enumerate(hint):
            put(screen, y + 2, [(text, "plain")], width, pairs)
    else:
        put(screen, 2, rows.header() if tab == 0 else rows.run_header(), width, pairs)
        for y, segments in enumerate(segment_rows[: height - 6]):
            put(screen, y + 3, segments, width, pairs, selected=y == cursor)
    put(screen, height - 1, footer, width, pairs)
    screen.refresh()


def show_log(screen, record):
    curses.endwin()
    subprocess.run(["less", "-R", os.path.join(runs.run_dir(record["id"]), "output.log")], check=False)
    screen.refresh()


def footer_for(tab, items, cursor, message, confirming=False, editing=False):
    if confirming:
        return [("Run %s now? y/n" % items[cursor].name, "warn")]
    if editing:
        return [("Edit %s:  v VS Code · c Claude session · esc cancel" % items[cursor].name, "warn")]
    if message:
        return [(message, "fail")]
    if tab == 0 and items:
        workflow = items[cursor]
        return [(workflow.description or workflow.name, "plain"), ("   " + trigger_summary(workflow), "dim")]
    return []


def run(screen):
    curses.curs_set(0)
    pairs = init_colors()
    ctx = hwf.invocation_context()
    tab, cursor, message, confirming, editing = 0, 0, "", False, False
    while True:
        now = datetime.now()
        workflows = catalog.discover(ctx["cwd"])
        records = runs.list_runs(200)
        items = workflows if tab == 0 else records
        latest = rows.last_runs(records)
        segment_rows = [rows.workflow_row(w, latest.get(w.name), now) for w in workflows] if tab == 0 else [rows.run_row(r, now) for r in records]
        cursor = min(cursor, max(len(segment_rows) - 1, 0))
        draw(screen, pairs, tab, segment_rows, cursor, footer_for(tab, items, cursor, message, confirming, editing), empty_hint(tab, ctx["cwd"]))
        screen.timeout(REFRESH_MS)
        key = screen.getch()
        if key == -1:
            continue  # refresh tick: redraw, and leave a pending prompt alone
        if confirming:
            if key == ord("y"):
                hwf.spawn_detached(items[cursor].name, ctx, {})
                return
            confirming = False
            continue
        if editing:
            choice = {ord("v"): "vscode", ord("c"): "claude"}.get(key)
            if choice:
                hwf.spawn_hwf(["edit", items[cursor].name, "--with", choice], ctx)
                return
            editing = False
            continue
        if key in (ord("q"), 27):
            return
        if key == ord("e") and tab == 0 and items:
            editing = True
        elif key == 9:
            tab, cursor, message = 1 - tab, 0, ""
        elif key in (curses.KEY_DOWN, ord("j")) and cursor < len(segment_rows) - 1:
            cursor, message = cursor + 1, ""
        elif key in (curses.KEY_UP, ord("k")) and cursor > 0:
            cursor, message = cursor - 1, ""
        elif key in (10, 13) and items:
            if tab == 1:
                show_log(screen, items[cursor])
            elif items[cursor].error:
                message = items[cursor].error
            else:
                confirming = True


if __name__ == "__main__":
    curses.wrapper(run)
