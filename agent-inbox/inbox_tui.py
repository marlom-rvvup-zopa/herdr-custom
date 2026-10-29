#!/usr/bin/env python3
"""Inbox popup: open, stale, settled and snoozed agents in tabs.

tab/shift-tab switch tabs · j/k move · enter focus or resume · s snooze · m settle · d forget · q close.
"""

import curses
import locale
import os
import time

from lib import SNOOZE_CHOICES, CannotShelve, forget, herdr_request, is_stale, last_interaction, load_json, resume, save_json, shelve

OPEN, STALE, SETTLED, SNOOZED = range(4)
# Nerd Font Material Design icons: inbox, archive-clock, archive, alarm-snooze.
TABS = [(OPEN, chr(0xf0687), "Open"), (STALE, chr(0xf174f), "Stale"), (SETTLED, chr(0xf003c), "Settled"), (SNOOZED, chr(0xf068e), "Snoozed")]
HINTS = {
    OPEN: "enter focus · s snooze · m settle · tab next · q close",
    STALE: "enter resume · s snooze · d forget · tab next · q close",
    SETTLED: "enter resume · s snooze · d forget · tab next · q close",
    SNOOZED: "enter wake now · s change time · d forget · tab next · q close",
}
STATUS_TOKENS = ("st_working", "st_blocked", "st_done", "st_idle")
# curses color pair per status token.
STATUS_COLORS = {"st_working": 1, "st_blocked": 2, "st_done": 3, "st_idle": 4}
# Selected row and tab: dark text on a cyan bar. Reverse video draws default colors the same on both sides in some
# popups, which hides the text.
SELECTED_PAIR = 5
SEPARATOR = " │ "
ESC_DELAY_MS = 25


def age(secs):
    """Compact duration: 45s, 12m, 3h, 2d."""
    secs = max(int(secs), 0)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return "%d%s" % (secs // size, unit)
    return "%ds" % secs


def fit(text, width):
    return text if len(text) <= width else text[: max(width - 1, 0)] + "…"


def table(headers, rows, width):
    """Header, separator and rows as aligned lines, with the widest column (the title) taking the slack.
    Returns the lines and each column's start offset."""
    widths = [max([len(h)] + [len(row[i]) for row in rows]) for i, h in enumerate(headers)]
    flex = widths.index(max(widths))
    widths[flex] = max(width - sum(w for i, w in enumerate(widths) if i != flex) - len(SEPARATOR) * (len(headers) - 1), len(headers[flex]))
    offsets, at = [], 0
    for w in widths:
        offsets.append(at)
        at += w + len(SEPARATOR)

    def line(cells):
        return SEPARATOR.join(fit(cell, w).ljust(w) for cell, w in zip(cells, widths))

    rule = "─┼─".join("─" * w for w in widths)
    return [line(headers), rule] + [line(row) for row in rows], offsets


class Inbox:
    def __init__(self):
        self.now = time.time()

    def load(self):
        self.now = time.time()
        workspaces = {w["workspace_id"]: w["label"] for w in herdr_request("workspace.list", {}).get("workspaces", [])}
        state = load_json("state.json", {})
        touched = load_json("touched.json", {})
        links = load_json("subagents.json", {})
        agents = [a for a in herdr_request("agent.list", {}).get("agents", []) if a.get("agent")]
        agents.sort(key=lambda a: a["tokens"].get("sort_key", ""), reverse=True)
        tickets = load_json("links.json", {})
        woken = load_json("woken.json", {})
        open_rows = []
        for rec in agents:
            token = next((t for t in STATUS_TOKENS if rec["tokens"].get(t)), None)
            idle = self.now - last_interaction(rec, state.get(rec["terminal_id"], {}).get("last_used", self.now), touched)
            depth = 0
            parent = rec["terminal_id"]
            while parent in links and depth < 5:
                parent, depth = links[parent], depth + 1
            title = ("   " * (depth - 1) + "└─ " if depth else "") + (rec.get("terminal_title_stripped") or rec["agent"])
            mark = chr(0xf068e) if rec["terminal_id"] in woken else " "
            ticket = ", ".join(tickets.get(rec["pane_id"], {}).get("tickets", [])[:2])
            open_rows.append({
                "key": rec["pane_id"], "rec": rec, "token": token, "stale": is_stale(rec, state.get(rec["terminal_id"], {}).get("last_used", self.now), touched, self.now),
                "cells": [rec["tokens"].get(token, "") if token else "", mark, age(idle), title, workspaces.get(rec["workspace_id"], ""), ticket],
            })
        self.tabs = {
            OPEN: open_rows,
            STALE: self.stored("stale", workspaces, "stale_at"),
            SETTLED: self.stored("settled", workspaces, "shelved_at"),
            SNOOZED: self.stored("snoozed", workspaces, "wake_at"),
        }

    def stored(self, store, workspaces, when):
        rows = []
        for entry in load_json(store + ".json", []):
            # Entries settled before the inbox kept a different timestamp key.
            at = entry.get(when) or entry.get("settled_at") or entry.get("shelved_at") or 0
            shown = "in " + age(at - self.now) if when == "wake_at" else age(self.now - at) + " ago"
            where = [entry["title"], entry.get("workspace_label") or "", os.path.basename(entry["cwd"])]
            # The settled tab says who settled each session, so wrong automatic settles are easy to spot.
            cells = [shown, *settled_by_cells(entry), *where] if store == "settled" else [shown, *where]
            rows.append({"key": entry["session"], "entry": entry, "cells": cells})
        if when == "wake_at":
            rows.sort(key=lambda r: r["entry"]["wake_at"])
        return rows


def settled_by_cells(entry):
    """Who settled the session and, for an automatic settle, the evidence it went by."""
    if entry.get("settled_by") == "auto":
        return ["auto", entry.get("settle_reason") or ""]
    return ["you", ""]


HEADERS = {
    OPEN: ["Status", " ", "Idle", "Title", "Workspace", "Ticket"],
    STALE: ["Stale", "Title", "Workspace", "Directory"],
    SETTLED: ["Settled", "By", "Why", "Title", "Workspace", "Directory"],
    SNOOZED: ["Wakes", "Title", "Workspace", "Directory"],
}


def draw(screen, inbox, tab, cursor, message, menu=None):
    screen.erase()
    height, width = screen.getmaxyx()
    if menu is not None:
        # The picker owns the whole screen: painting it over table rows that hold icon glyphs drew its selection
        # bar in the wrong place, because the terminal and curses disagree on how wide those glyphs are.
        draw_menu(screen, menu, inbox.tabs[tab][cursor]["cells"][HEADERS[tab].index("Title")])
        screen.refresh()
        return
    x = 0
    for kind, icon, name in TABS:
        label = " %s %s %d " % (icon, name, len(inbox.tabs[kind]))
        attr = curses.color_pair(SELECTED_PAIR) | curses.A_BOLD if kind == tab else curses.A_DIM
        screen.addnstr(0, x, label, max(width - x - 1, 0), attr)
        x += len(label) + 1
    rows = inbox.tabs[tab]
    if not rows:
        screen.addnstr(2, 1, "Nothing here.", width - 2, curses.A_DIM)
    else:
        lines, offsets = table(HEADERS[tab], [r["cells"] for r in rows], width - 2)
        screen.addnstr(2, 1, lines[0], width - 2, curses.A_BOLD)
        screen.addnstr(3, 1, lines[1], width - 2, curses.A_DIM)
        visible = height - 6
        first = min(max(cursor - visible + 1, 0), max(len(rows) - visible, 0))
        for n, row in enumerate(rows[first : first + visible]):
            y = 4 + n
            selected = first + n == cursor
            screen.addnstr(y, 1, lines[2 + first + n], width - 2, curses.color_pair(SELECTED_PAIR) if selected else curses.A_NORMAL)
            if tab == OPEN and not selected:
                if row["token"]:
                    screen.chgat(y, 1 + offsets[0], len(HEADERS[OPEN][0]) + 6, curses.color_pair(STATUS_COLORS[row["token"]]) | curses.A_BOLD)
                if row["cells"][1].strip():
                    screen.chgat(y, 1 + offsets[1], 1, curses.color_pair(1) | curses.A_BOLD)
                if row["stale"]:
                    screen.chgat(y, 1 + offsets[2], len(row["cells"][2]), curses.A_DIM)
    screen.addnstr(height - 1, 1, message or HINTS[tab], width - 2, curses.A_DIM if not message else curses.A_BOLD)
    screen.refresh()


MENU_WIDTH = 26


def draw_menu(screen, cursor, title):
    """Duration picker for the agent called `title`, centred on an otherwise empty screen."""
    height, width = screen.getmaxyx()
    top, left = max(height // 2 - 4, 0), max(width // 2 - MENU_WIDTH // 2, 0)
    screen.addnstr(max(top - 2, 0), 1, fit(title, width - 2), width - 2, curses.A_BOLD)
    inner = MENU_WIDTH - 2
    heading = " Snooze for "
    screen.addnstr(top, left, "┌─" + heading + "─" * (inner - len(heading) - 1) + "┐", MENU_WIDTH, curses.A_BOLD)
    for n in range(len(SNOOZE_CHOICES) + 2):
        screen.addnstr(top + 1 + n, left, "│" + " " * inner + "│", MENU_WIDTH)
    for n, (name, _) in enumerate(SNOOZE_CHOICES):
        label = " %s %d  %s" % ("▸" if n == cursor else " ", n + 1, name)
        screen.addnstr(top + 2 + n, left + 2, label.ljust(inner - 2), inner - 2, curses.color_pair(SELECTED_PAIR) if n == cursor else curses.A_NORMAL)
    screen.addnstr(top + len(SNOOZE_CHOICES) + 3, left, "└" + "─" * inner + "┘", MENU_WIDTH)


def menu_key(key, cursor):
    """One key in the duration picker: (new cursor, seconds picked, closed)."""
    if key in (ord("q"), 27):
        return cursor, None, True
    if key in (curses.KEY_DOWN, ord("j")):
        return min(cursor + 1, len(SNOOZE_CHOICES) - 1), None, False
    if key in (curses.KEY_UP, ord("k")):
        return max(cursor - 1, 0), None, False
    if ord("1") <= key < ord("1") + len(SNOOZE_CHOICES):
        return cursor, SNOOZE_CHOICES[key - ord("1")][1], True
    if key in (10, 13):
        return cursor, SNOOZE_CHOICES[cursor][1], True
    return cursor, None, False


def snooze(row, tab, secs, now):
    """Snooze an open agent (closing its pane) or reschedule a stored session."""
    if tab == OPEN:
        shelve(row["rec"], "snoozed", wake_at=now + secs, snoozed_at=now)
        return
    entry = {**row["entry"], "wake_at": now + secs, "snoozed_at": now}
    for store in ("stale", "settled"):
        forget(store, entry["session"])
    snoozed = [e for e in load_json("snoozed.json", []) if e["session"] != entry["session"]]
    save_json("snoozed.json", [entry] + snoozed)


def run(screen):
    locale.setlocale(locale.LC_ALL, "")
    # ncurses waits a second after Escape to see whether an arrow-key sequence follows.
    curses.set_escdelay(ESC_DELAY_MS)
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    for pair, color in ((1, curses.COLOR_YELLOW), (2, curses.COLOR_RED), (3, curses.COLOR_CYAN), (4, curses.COLOR_GREEN)):
        curses.init_pair(pair, color, -1)
    curses.init_pair(SELECTED_PAIR, curses.COLOR_BLACK, curses.COLOR_CYAN)
    screen.timeout(2000)
    inbox = Inbox()
    inbox.load()
    tab, cursor, message, menu = OPEN, 0, "", None
    while True:
        rows = inbox.tabs[tab]
        cursor = min(cursor, max(len(rows) - 1, 0))
        draw(screen, inbox, tab, cursor, message, menu)
        key = screen.getch()
        message = ""
        if key == -1:
            inbox.load()
        elif menu is not None:
            menu, secs, closed = menu_key(key, menu)
            if closed:
                menu = None
            if secs:
                try:
                    snooze(rows[cursor], tab, secs, time.time())
                    message = "Snoozed."
                except CannotShelve as e:
                    message = str(e)
                inbox.load()
        elif key in (ord("q"), 27):
            return
        elif key in (9, curses.KEY_RIGHT, ord("l")):
            tab, cursor = (tab + 1) % len(TABS), 0
        elif key in (curses.KEY_BTAB, curses.KEY_LEFT, ord("h")):
            tab, cursor = (tab - 1) % len(TABS), 0
        elif key in (ord("1"), ord("2"), ord("3"), ord("4")):
            tab, cursor = key - ord("1"), 0
        elif key in (curses.KEY_DOWN, ord("j")):
            cursor = min(cursor + 1, max(len(rows) - 1, 0))
        elif key in (curses.KEY_UP, ord("k")):
            cursor = max(cursor - 1, 0)
        elif not rows:
            continue
        elif key in (10, 13):
            row = rows[cursor]
            if tab == OPEN:
                herdr_request("agent.focus", {"target": row["rec"]["pane_id"]})
                return
            resume(row["entry"], focus=True)
            forget({STALE: "stale", SETTLED: "settled", SNOOZED: "snoozed"}[tab], row["entry"]["session"])
            return
        elif key == ord("s"):
            menu = 0
        elif key == ord("m") and tab == OPEN:
            try:
                shelve(rows[cursor]["rec"], "settled")
                message = "Settled."
            except CannotShelve as e:
                message = str(e)
            inbox.load()
        elif key == ord("d") and tab != OPEN:
            forget({STALE: "stale", SETTLED: "settled", SNOOZED: "snoozed"}[tab], rows[cursor]["entry"]["session"])
            inbox.load()


if __name__ == "__main__":
    curses.wrapper(run)
