#!/usr/bin/env python3
"""Quick menu, without opening the inbox. Claude: n new tab · p new pane to the right. Inbox: c close and settle · s snooze. Arrows or the shortcut keys; q cancels.

Opened by the quick-actions action, which passes the focused pane in QUICK_PANE_ID.
"""

import curses
import locale
import os
import time

import inbox_tui
from lib import SNOOZE_CHOICES, CannotShelve, herdr_cli, herdr_request, shelve

# Nerd font icons: nf-cod-claude, nf-md-inbox, nf-md-tab_plus, nf-md-view_split_vertical, nf-md-check, nf-md-sleep.
GROUPS = [
    (("\uec82", "Claude"), [("n", "\U000f075c", "new tab"), ("p", "\U000f0bcc", "new pane to the right")]),
    (("\U000f0687", "Inbox"), [("c", "\U000f012c", "close and settle"), ("s", "\U000f04b2", "snooze")]),
]
ITEMS = [item for _, items in GROUPS for item in items]
NO_AGENT = "no agent in the focused pane"


def focused_agent():
    pane_id = os.environ.get("QUICK_PANE_ID")
    agent = next((a for a in herdr_request("agent.list", {})["agents"] if a["pane_id"] == pane_id), None)
    if not agent:
        raise LookupError(NO_AGENT)
    return agent


def new_claude_tab():
    """Open a focused tab running claude, in the workspace and directory of the focused pane."""
    pane = herdr_cli("pane", "get", os.environ["QUICK_PANE_ID"])["pane"]
    root = herdr_cli("tab", "create", "--workspace", pane["workspace_id"], "--cwd", pane["cwd"], "--focus")["root_pane"]
    herdr_cli("pane", "run", root["pane_id"], "claude")


def new_claude_pane():
    """Split the focused pane to its right and run claude there, in the same directory."""
    pane_id = os.environ["QUICK_PANE_ID"]
    cwd = herdr_cli("pane", "get", pane_id)["pane"]["cwd"]
    new = herdr_cli("pane", "split", pane_id, "--direction", "right", "--cwd", cwd, "--focus")["pane"]
    herdr_cli("pane", "run", new["pane_id"], "claude")


def settle(agent):
    shelve(agent, "settled")


def snooze(agent, secs):
    inbox_tui.snooze({"rec": agent}, inbox_tui.OPEN, secs, time.time())


class Muted(str):
    """A heading or hint: drawn dim and never selected."""


def main_lines():
    """The main menu's lines and, for each item, the index of its line."""
    lines, rows = [], []
    for n, ((heading_icon, heading), items) in enumerate(GROUPS):
        if n:
            lines.append("")
        lines.append(Muted("%s  %s" % (heading_icon, heading.upper())))
        for key, icon, label in items:
            rows.append(len(lines))
            lines.append("%s  %s  %s" % (key, icon, label))
    return lines + ["", Muted("↑↓ move · ⏎ select · q cancel")], rows


def main_key(key, cursor):
    """One key in the main menu: (new cursor, shortcut chosen, closed)."""
    if key in (ord("q"), 27):
        return cursor, None, True
    if key in (curses.KEY_DOWN, ord("j")):
        return min(cursor + 1, len(ITEMS) - 1), None, False
    if key in (curses.KEY_UP, ord("k")):
        return max(cursor - 1, 0), None, False
    if key in (10, 13):
        return cursor, ITEMS[cursor][0], False
    if 0 < key < 256 and chr(key) in [shortcut for shortcut, _, _ in ITEMS]:
        return cursor, chr(key), False
    return cursor, None, False


def draw(screen, title, lines, cursor, message):
    screen.erase()
    height, width = screen.getmaxyx()
    screen.addnstr(0, 1, inbox_tui.fit(title, width - 2), width - 2, curses.A_BOLD)
    for n, line in enumerate(lines):
        selected = cursor is not None and n == cursor
        style = curses.A_REVERSE if selected else curses.A_DIM if isinstance(line, Muted) else curses.A_NORMAL
        screen.addnstr(2 + n, 1, line, width - 2, style)
    if message:
        screen.addnstr(height - 1, 1, message, width - 2, curses.A_BOLD)
    screen.refresh()


def run(screen):
    locale.setlocale(locale.LC_ALL, "")
    curses.curs_set(0)
    curses.set_escdelay(inbox_tui.ESC_DELAY_MS)
    try:
        agent = focused_agent()
        title, message = agent.get("terminal_title_stripped") or agent["agent"], ""
    except LookupError as e:
        agent, title, message = None, "Quick actions", str(e)
    snoozing, cursor, snooze_cursor = False, 0, 0
    while True:
        if snoozing:
            lines = ["%d   %s" % (n + 1, name) for n, (name, _) in enumerate(SNOOZE_CHOICES)] + ["", Muted("↑↓ move · ⏎ select · esc back")]
            line = snooze_cursor
        else:
            lines, rows = main_lines()
            line = rows[cursor]
        draw(screen, ("Snooze: " if snoozing else "") + title, lines, line, message)
        key = screen.getch()
        try:
            if snoozing:
                snooze_cursor, secs, closed = inbox_tui.menu_key(key, snooze_cursor)
                if secs:
                    snooze(agent, secs)
                    return
                snoozing = not closed
            else:
                cursor, chosen, closed = main_key(key, cursor)
                if closed:
                    return
                if chosen == "n":
                    new_claude_tab()
                    return
                if chosen == "p":
                    new_claude_pane()
                    return
                if chosen and not agent:
                    message = NO_AGENT
                elif chosen == "c":
                    settle(agent)
                    return
                elif chosen == "s":
                    snoozing, snooze_cursor = True, 0
        except CannotShelve as e:
            message = str(e)
            snoozing = False


if __name__ == "__main__":
    curses.wrapper(run)
