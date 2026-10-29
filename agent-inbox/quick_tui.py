#!/usr/bin/env python3
"""Quick actions for the focused agent, without opening the inbox: c close and settle · s snooze · q cancel.

Opened by the quick-actions action, which passes the focused pane in QUICK_PANE_ID.
"""

import curses
import os
import time

import inbox_tui
from lib import SNOOZE_CHOICES, CannotShelve, herdr_request, shelve

MAIN_HELP = ["c   close and settle", "s   snooze", "q   cancel"]


def focused_agent():
    pane_id = os.environ.get("QUICK_PANE_ID")
    agent = next((a for a in herdr_request("agent.list", {})["agents"] if a["pane_id"] == pane_id), None)
    if not agent:
        raise LookupError("no agent in the focused pane")
    return agent


def settle(agent):
    shelve(agent, "settled")


def snooze(agent, secs):
    inbox_tui.snooze({"rec": agent}, inbox_tui.OPEN, secs, time.time())


def draw(screen, title, lines, cursor, message):
    screen.erase()
    height, width = screen.getmaxyx()
    screen.addnstr(0, 1, inbox_tui.fit(title, width - 2), width - 2, curses.A_BOLD)
    for n, line in enumerate(lines):
        selected = cursor is not None and n == cursor
        screen.addnstr(2 + n, 1, line, width - 2, curses.A_REVERSE if selected else curses.A_NORMAL)
    if message:
        screen.addnstr(height - 1, 1, message, width - 2, curses.A_BOLD)
    screen.refresh()


def run(screen):
    curses.curs_set(0)
    curses.set_escdelay(inbox_tui.ESC_DELAY_MS)
    try:
        agent = focused_agent()
    except LookupError as e:
        draw(screen, "Quick actions", [], None, str(e))
        screen.getch()
        return
    title = agent.get("terminal_title_stripped") or agent["agent"]
    snoozing, cursor, message = False, 0, ""
    while True:
        lines = ["%d   %s" % (n + 1, name) for n, (name, _) in enumerate(SNOOZE_CHOICES)] if snoozing else MAIN_HELP
        draw(screen, ("Snooze: " if snoozing else "") + title, lines, cursor if snoozing else None, message)
        key = screen.getch()
        try:
            if snoozing:
                cursor, secs, closed = inbox_tui.menu_key(key, cursor)
                if secs:
                    snooze(agent, secs)
                    return
                snoozing = not closed
            elif key == ord("c"):
                settle(agent)
                return
            elif key == ord("s"):
                snoozing, cursor = True, 0
            elif key in (ord("q"), 27):
                return
        except CannotShelve as e:
            message = str(e)
            snoozing = False


if __name__ == "__main__":
    curses.wrapper(run)
