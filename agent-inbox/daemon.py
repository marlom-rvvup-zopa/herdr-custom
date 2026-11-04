#!/usr/bin/env python3
"""Agent inbox daemon.

Sorts the Agents panel by state (needs input, idle, done, working, then waiting on a sub agent or a script),
and within a state by last use (the moment an agent starts working, i.e. a prompt was sent).
Reports one state token per agent so the sidebar can show working / waiting / needs input / done / idle.

Usage: daemon.py            run the daemon (exits if one is already running)
       daemon.py --ensure   start the daemon detached if it is not running
       daemon.py --restart  stop the running daemon and start a fresh one
"""

import collections
import fcntl
import glob
import json
import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

import github_prs
import jira_status
import native_subs
import shells
import auto_settle
import tab_titles
import tickets as session_tickets
from lib import CannotShelve, PaneNotFound, ago_text, family_root, herdr_request, is_stale, last_interaction, lineage, load_json, notify, prune_sub_links, resume, save_json, shelve, socket_path, state_dir

SOURCE = "marlom.agent-inbox"
HINT_CHARS = 100
JIRA_ICON = chr(0xf0303)
# Herdr trims plain whitespace from token values, so indentation uses the braille blank.
BLANK = "\u2800"
# Tree glyphs for sub agents: the last visible child of a parent closes its branch.
SUB_MARK = "├─ "
LAST_SUB_MARK = "└─ "
SUB_GUTTER = "│"
# Cells the gutter line is drawn left of the tree mark above it, with the text below it staying in place.
GUTTER_SHIFT = 2
# Native sub agents are listed on their parent's row, in tokens of their own: one report holds at most 16 tokens.
NATIVE_SOURCE = "marlom.agent-inbox.native"
# Sources hold 16 tokens each: the main one has the first ticket and PR row, the native one the second, this one the rest.
LINKS_SOURCE = "marlom.agent-inbox.links"
TICKET_TOKENS = ["ticket_line", "ticket_line2", "ticket_line3", "ticket_line4"]
# Cells a sidebar row can show before herdr cuts it with an ellipsis, and the cells an icon and its space take.
LINK_ROW_WIDTH = 46
ICON_WIDTH = 3
NATIVE_ROWS = 4
PANE_TITLES_KEPT = 20
# Shown after the status of an agent that came back from a snooze and has not been touched since.
SNOOZE_ICON = BLANK + chr(0xf068e)
# nf-fa-terminal, drawn after the status while the agent has a shell running.
SHELL_ICON = chr(0xf120)
NATIVE_TOKENS = ["native_%d" % n for n in range(1, NATIVE_ROWS + 1)]
PR_ICON = chr(0xf407)
PR_REFRESH_SECS = 120
PR_DONE_REFRESH_SECS = 1800
TICKET_REFRESH_SECS = 300
TICKET_DONE_REFRESH_SECS = 3600
PROJECTS_REFRESH_SECS = 3600
PROJECTS_RETRY_SECS = 300
SUMMARY_PROMPT_CHARS = 1000
SUMMARY_MIN_PROMPTS = 2
SUMMARY_TIMEOUT_SECS = 90
SUMMARY_RETRY_SECS = 600
SUMMARY_KEEP_SECS = 30 * 24 * 3600
TICKET_RESCAN_SECS = 60
# Settling is hard to notice when wrong, so it only goes by PR and Jira data this recent.
SETTLE_EVIDENCE_MAX_AGE_SECS = 300
SUMMARY_SYSTEM_PROMPT = (
    "You get the first two prompts of a coding-agent session. Output one line and nothing else: "
    "a summary of what the session is about in at most 12 words."
)
TICK_SECS = 30
DEBOUNCE_SECS = 0.3
SUBSCRIPTIONS = [
    {"type": "pane.updated"},
    {"type": "pane.agent_detected"},
    {"type": "pane.created"},
    {"type": "pane.closed"},
    {"type": "pane.focused"},
]
STATE_LABELS = {
    "working": " working",
    "blocked": "󱔢 needs input",
    "done": "󰗠 done",
    "idle": "󰝦 idle",
    "waiting": chr(0xf06ad) + " waiting",
}
STATE_TOKENS = {status: "st_" + status for status in STATE_LABELS}
# The main source is full, so the waiting label goes out with the native source to get a color of its own.
WAITING_TOKEN = STATE_TOKENS["waiting"]
# Higher sorts first in the sidebar: needs input, idle, done, working, then waiting.
URGENCY = {"blocked": 4, "idle": 3, None: 3, "done": 2, "working": 1, "waiting": 0}


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, file=sys.stderr, flush=True)


def lock_path():
    return os.path.join(state_dir(), "daemon.lock")


def pid_path():
    return os.path.join(state_dir(), "daemon.pid")


def acquire_lock():
    f = open(lock_path(), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def spawn_detached():
    log_file = open(os.path.join(state_dir(), "daemon.log"), "a")
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__)],
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=log_file,
        start_new_session=True,
    )


def tab_positions(tabs):
    """Position of each tab within its workspace, in tab bar order."""
    counts, positions = {}, {}
    for tab in tabs:
        counts[tab["workspace_id"]] = counts.get(tab["workspace_id"], 0) + 1
        positions[tab["tab_id"]] = counts[tab["workspace_id"]]
    return positions


def transcript_path(session_id):
    if not re.fullmatch(r"[A-Za-z0-9-]+", session_id):
        return None
    paths = glob.glob(os.path.expanduser("~/.claude/projects/*/%s.jsonl" % session_id))
    return paths[0] if paths else None


def typed_prompts(session_id):
    """Prompts typed in a Claude session, oldest first; empty if there is no transcript yet."""
    path = transcript_path(session_id)
    if not path:
        return []
    prompts = []
    with open(path) as f:
        for line in f:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            text = session_tickets.prompt_text((entry.get("message") or {}).get("content"))
            # Skip tool results, meta messages and command/system wrappers.
            if entry.get("type") == "user" and not entry.get("isMeta") and text and not text.startswith("<"):
                prompts.append(" ".join(text.split()))
    return prompts


def summarize(prompts):
    """Ask Claude for a one-line summary of the session, or None on failure."""
    claude = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    text = "First prompt: %s\nSecond prompt: %s" % (prompts[0][:SUMMARY_PROMPT_CHARS], prompts[1][:SUMMARY_PROMPT_CHARS])
    # --bare skips the user's hooks; cwd /tmp keeps project instructions out of the call.
    result = subprocess.run(
        [claude, "-p", "--bare", "--no-session-persistence", "--model", "haiku", "--system-prompt", SUMMARY_SYSTEM_PROMPT],
        input=text, capture_output=True, text=True, timeout=SUMMARY_TIMEOUT_SECS, cwd="/tmp",
    )
    summary = tab_titles.parse_summary(result.stdout)
    if result.returncode != 0 or not summary:
        log("summary failed (exit %d): %s %s" % (result.returncode, result.stderr.strip()[:200], result.stdout.strip()[:200]))
        return None
    return summary[:HINT_CHARS]


def shown_statuses(live, state, parents, shell_waiting):
    """The status each terminal shows. An agent that is not working or asking for input, but has a working
    sub agent somewhere below it or a script running that is not a server, is waiting for it."""
    shown = {tid: display_status(rec["agent_status"], state[tid]["unseen_done"]) for tid, rec in live.items()}
    for tid in shell_waiting & shown.keys():
        if shown[tid] in ("idle", "done", None):
            shown[tid] = "waiting"
    for tid, rec in live.items():
        if rec["agent_status"] != "working":
            continue
        for ancestor in lineage(tid, parents)[1:]:
            if shown[ancestor] in ("idle", "done", None):
                shown[ancestor] = "waiting"
    return shown


def layouts(agents, state, parents, collapsed, woken, shell_waiting=frozenset()):
    """Per terminal: status shown, sub agent depth, sort key and visibility. A family sorts by the main agent's status
    unless a sub agent needs input, then by its most recent member, and a family with an agent back from a snooze sorts before every other one."""
    live = {a["terminal_id"]: a for a in agents}
    parents = {child: parent for child, parent in parents.items() if child in live and parent in live}
    shown = shown_statuses(live, state, parents, shell_waiting)
    families = {}
    for tid in live:
        families.setdefault(family_root(tid, parents)[0], []).append(tid)
    out = {}
    for root, members in families.items():
        family_time = max(state[tid]["last_used"] for tid in members)
        back = any(tid in woken for tid in members)
        urgency = URGENCY["blocked"] if "blocked" in (shown[tid] for tid in members) else URGENCY[shown[root]]
        for tid in members:
            depth = family_root(tid, parents)[1]
            # The key spells out the path from the root, one recency segment per sub agent, closed by "9".
            # Sorted desc, a parent ("9") comes before its children ("5...") and each subtree stays contiguous.
            path = lineage(tid, parents)[-2::-1]
            tail = "".join("5%012d%s" % (state[node]["last_used"], node[-6:].rjust(6)) for node in path) + "9"
            out[tid] = {
                "status": shown[tid],
                "depth": depth,
                "woken": tid in woken,
                "sort_key": "%d%d%012d-%s-%s" % (1 + back, urgency, family_time, root[-6:], tail),
                # A sub agent that needs input stays visible even when its family is collapsed.
                "hidden": depth > 0 and root in collapsed and live[tid]["agent_status"] != "blocked",
                "hidden_subs": len(members) - 1 if depth == 0 and root in collapsed else 0,
            }
    mark_last_children(out, parents)
    return out


def mark_last_children(out, parents):
    """Flag the sub agent drawn last under each parent, reading the order the sidebar sorts into,
    and build the guide columns each sub agent row starts with (a gutter under every open branch)."""
    drawn = sorted((tid for tid, lay in out.items() if lay["depth"] and not lay["hidden"]), key=lambda tid: out[tid]["sort_key"], reverse=True)
    last = {parents[tid]: tid for tid in drawn}
    for tid, lay in out.items():
        lay["last_child"] = last.get(parents.get(tid)) == tid
        lay["has_subs"] = tid in last
    for tid, lay in out.items():
        # Ancestors between the root and this row, outermost first.
        between = lineage(tid, parents)[1:-1][::-1]
        lay["guides"] = BLANK * 2 + "".join(
            (BLANK if out[node]["last_child"] else SUB_GUTTER) + BLANK * 2 for node in between
        )


def continuation_pad(layout):
    """What a sub agent's rows below the first start with: its branch continues with a gutter line, except under the closing child."""
    mark = LAST_SUB_MARK if layout["last_child"] else SUB_MARK
    gutter = BLANK if layout["last_child"] else SUB_GUTTER
    return layout["guides"][GUTTER_SHIFT:] + gutter + BLANK * (len(mark) - 1 + GUTTER_SHIFT)


def display_width(text):
    return len(text.replace("\u0336", ""))


def link_rows(tickets, prs, width):
    """Tickets, then PRs by state, packed onto the sidebar rows of `width` cells; what overflows the last row stays on it.
    Each row is {"ticket": ticket texts, "pr": PR dicts}. Every row pays for the icon before its first ticket and first PR."""
    order = list(github_prs.PR_TOKENS)
    items = [("ticket", text, text) for text in tickets]
    items += [("pr", pr, github_prs.pr_text(pr)) for pr in sorted(prs, key=lambda pr: order.index(github_prs.pr_state(pr)))]
    rows = [{"ticket": [], "pr": []} for _ in TICKET_TOKENS]
    used, current = 0, 0
    for kind, item, text in items:
        needed = display_width(text) + (0 if rows[current][kind] else ICON_WIDTH)
        if used and used + 1 + needed > width and current < len(rows) - 1:
            current, used = current + 1, 0
            needed = display_width(text) + ICON_WIDTH
        used += needed + (1 if used else 0)
        rows[current][kind].append(item)
    return rows


def indent_tokens(tokens, layout, shown):
    """Indent sub agents under their parent by prefixing the first token of each sidebar row,
    and note on a collapsed parent how many sub agents are hidden."""
    state_token = STATE_TOKENS.get(shown)
    if layout["hidden_subs"] and state_token:
        tokens[state_token] += " (+%d)" % layout["hidden_subs"]
    if not layout["depth"]:
        return
    pad = layout["guides"]
    mark = LAST_SUB_MARK if layout["last_child"] else SUB_MARK
    cont = continuation_pad(layout)
    if state_token:
        tokens[state_token] = pad + mark + tokens[state_token]
    for name in ("title", "hint"):
        if tokens[name]:
            tokens[name] = cont + tokens[name]
    # Row 4 starts with the ticket if there is one, otherwise with the first PR token.
    row_tokens = ["ticket_line", *github_prs.PR_TOKENS.values()]
    first = next((name for name in row_tokens if tokens[name]), None)
    if first:
        tokens[first] = cont + tokens[first]


def link_row_tokens(row, ticket_name, pr_names):
    tokens = {ticket_name: "%s %s" % (JIRA_ICON, " ".join(row["ticket"])) if row["ticket"] else None}
    tokens.update(github_prs.tokens(row["pr"], PR_ICON, pr_names))
    return tokens


def native_tokens(subs, layout):
    """One tree row per running native sub agent, hanging under the parent's own rows."""
    tokens = {name: None for name in NATIVE_TOKENS}
    if not subs or layout["hidden_subs"]:
        return tokens
    # Rows hang one level below the parent, in the column its own children use.
    pad = "" if not layout["depth"] else layout["guides"] + (BLANK if layout["last_child"] else SUB_GUTTER) + BLANK * 2
    shown = subs[:NATIVE_ROWS]
    hidden = len(subs) - len(shown)
    if hidden:
        shown[-1] = ("+%d more" % (hidden + 1), "")
    for n, (name, description) in enumerate(shown):
        closes = n == len(shown) - 1 and not layout["has_subs"]
        text = name + (" · " + description if description else "")
        tokens[NATIVE_TOKENS[n]] = pad + (LAST_SUB_MARK if closes else SUB_MARK) + text
    return tokens


def reply_age(status, st, now):
    """How long ago the agent last answered; nothing while it works or before its first answer is seen."""
    if status == "working" or not st.get("last_response"):
        return None
    return ago_text(int(now - st["last_response"]))


def display_status(status, unseen_done):
    if status in ("working", "blocked"):
        return status
    if status == "done" or unseen_done:
        return "done"
    return "idle" if status == "idle" else None


class Daemon:
    def __init__(self):
        self.dirty = threading.Event()
        self.state = load_json("state.json", {})
        self.reported = {}
        now = time.time()
        # Older versions stored the bare summary text.
        self.summaries = {
            sid: s if isinstance(s, dict) else {"text": s, "seen": now}
            for sid, s in load_json("summaries.json", {}).items()
        }
        self.summary_queue = queue.Queue()
        self.summary_requested = {}
        self.scans = {}
        self.tab_labels = load_json("tabs.json", {})
        self.pane_titles = load_json("pane-titles.json", {})
        self.pane_labels = load_json("pane-labels.json", {})
        self.topics = {}
        self.ticket_status = {}
        # Keys of the Jira projects we can see, looked up with acli and cached on disk.
        self.projects = load_json("jira-projects.json", {})
        self.ticket_queue = queue.Queue()
        self.ticket_queued = set()
        self.links = {}
        self.prs = {}
        self.pr_queue = queue.Queue()
        self.pr_queued = set()
        self.shell_parents = set()
        self.claude_pids = {}
        self.waiting_labels = {}

    def assert_view(self):
        herdr_request(
            "agent.view.set",
            {
                "source": SOURCE,
                "label": "Recent",
                "filter": {"op": "not", "filter": {"op": "eq", "field": {"token": "hidden"}, "value": "1"}},
                "sort": [{"field": {"token": "sort_key"}, "order": "desc"}],
            },
        )

    def refresh(self):
        # Agents still launching have no "agent" kind yet.
        agents = [a for a in herdr_request("agent.list", {}).get("agents", []) if a.get("agent")]
        tab_list = herdr_request("tab.list", {}).get("tabs", [])
        positions = tab_positions(tab_list)
        now = time.time()
        self.wake_due(now)
        self.links = {}
        # Terminals seen for the first time keep herdr's own recency order.
        new = sorted(
            (a for a in agents if a["terminal_id"] not in self.state),
            key=lambda a: a.get("state_change_seq") or 0,
        )
        for offset, rec in enumerate(new):
            self.state[rec["terminal_id"]] = {
                "last_used": now - (len(new) - offset),
                "status": rec["agent_status"],
                "unseen_done": False,
            }
        for rec in agents:
            st = self.state[rec["terminal_id"]]
            status = rec["agent_status"]
            if status == "working" and st["status"] != "working":
                st["last_used"] = now
            if st["status"] == "working" and status != "working":
                st["last_response"] = now
            if st["status"] == "working" and status in ("idle", "done"):
                st["unseen_done"] = True
            if status == "working" or rec.get("focused"):
                st["unseen_done"] = False
            st["status"] = status
        links = load_json("subagents.json", {})
        agents = self.archive_stale(agents, links, now)
        woken = self.update_woken(agents, now)
        self.shell_parents = shells.running_parents()
        self.claude_pids = {}
        waiting_parents = shells.waiting_parents()
        shell_waiting = {rec["terminal_id"] for rec in agents if rec["agent"] == "claude" and self.claude_pid(rec) in waiting_parents}
        layout = layouts(agents, self.state, links, set(load_json("collapsed.json", [])), woken, shell_waiting)
        for rec in agents:
            self.report(rec, self.state[rec["terminal_id"]], now, layout[rec["terminal_id"]])
            self.report_extra(rec, layout[rec["terminal_id"]])
        self.settle_pass(agents, links, now)
        self.rename_tabs(agents, tab_list, positions)
        self.rename_panes(agents)
        self.report_topics(agents)
        live = {a["terminal_id"] for a in agents}
        prune_sub_links(lambda: {a["terminal_id"] for a in herdr_request("agent.list", {}).get("agents", [])})
        self.state = {tid: st for tid, st in self.state.items() if tid in live}
        save_json("state.json", self.state)
        save_json("links.json", self.links)
        self.prune_summaries()

    def wake_due(self, now):
        """Resume snoozed sessions whose time has come, each in a new tab, and mark them woken."""
        snoozed = load_json("snoozed.json", [])
        due = [e for e in snoozed if e["wake_at"] <= now]
        if not due:
            return
        save_json("snoozed.json", [e for e in snoozed if e["wake_at"] > now])
        woken = load_json("woken.json", {})
        for entry in due:
            verdict = self.finished_work(entry)
            if verdict.ready:
                self.settle_snoozed(entry, verdict.reason, now)
                continue
            try:
                woken[resume(entry, focus=False)] = now
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as e:
                # Try again in five minutes rather than every tick.
                log("wake failed for %s: %s" % (entry["title"], e))
                save_json("snoozed.json", load_json("snoozed.json", []) + [{**entry, "wake_at": now + 300}])
        save_json("woken.json", woken)

    def finished_work(self, entry):
        """Whether the snoozed session's PRs are all merged or closed and the tickets it owns are all done, looked up now.
        A failed lookup counts as not finished, so the session comes back rather than staying hidden."""
        rec = {"agent": entry["agent"], "agent_session": {"kind": "id", "value": entry["session"]}}
        try:
            prs = github_prs.session_prs(entry["cwd"], entry["session"])
            found = self.session_scan(rec)
            rows = session_tickets.classify(found, [p.get("title") for p in prs]) if found else []
            done = {}
            for row in rows:
                if row["role"] != "primary":
                    continue
                try:
                    done[row["key"]] = jira_status.is_done(row["key"])
                except jira_status.TicketNotFound:
                    rows = [r for r in rows if r["key"] != row["key"]]
            return auto_settle.work_verdict(prs, rows, done, closed_unmerged_counts=True)
        except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as e:
            log("finished-work check failed for %s: %s" % (entry["title"], e))
            self.ask_for_jira_login(e)
            return auto_settle.Verdict(False, "lookup failed")

    def settle_snoozed(self, entry, reason, now):
        """Move a due snoozed session to the settled list instead of resuming it."""
        settled = {k: v for k, v in entry.items() if k not in ("wake_at", "snoozed_at")}
        settled.update(shelved_at=now, settled_by="snooze", settle_reason=reason)
        kept = [e for e in load_json("settled.json", []) if e["session"] != entry["session"]]
        save_json("settled.json", [settled] + kept)
        log("settled %s after snooze: %s" % (entry["session"], reason))
        notify("Settled after snooze", "%s (%s)" % (entry["title"], reason))

    def update_woken(self, agents, now):
        """Terminals back from a snooze that the user has not touched since. Prompting one clears it."""
        woken = load_json("woken.json", {})
        status = {rec["terminal_id"]: rec["agent_status"] for rec in agents}
        # A resumed pane is not an agent for a few seconds, so an unlisted terminal gets ten minutes.
        kept = {
            tid: at for tid, at in woken.items()
            if (status[tid] != "working" if tid in status else now - at < 600)
        }
        if kept != woken:
            save_json("woken.json", kept)
        return set(kept)

    def archive_stale(self, agents, links, now):
        """Close agents nobody has touched for a day and keep their sessions in the stale list.
        Agents in a sub agent family stay: closing one would orphan the others."""
        touched = load_json("touched.json", {})
        family = set(links) | set(links.values())
        kept = []
        for rec in agents:
            tid = rec["terminal_id"]
            if tid in family or not is_stale(rec, self.state[tid]["last_used"], touched, now):
                kept.append(rec)
                continue
            try:
                shelve(rec, "stale", stale_at=now)
                log("archived stale agent %s" % rec["pane_id"])
            except CannotShelve:
                kept.append(rec)
        return kept

    def settle_pass(self, agents, sub_links, now):
        """Settle sessions whose PRs are merged and tickets done, or only suggest it, as settle.json says.
        mode: "suggest" (default, one toast per session), "auto" or "off"; quiet_minutes: how long it must sit idle."""
        config = load_json("settle.json", {})
        mode = config.get("mode", auto_settle.DEFAULT_MODE)
        if mode not in auto_settle.SETTLE_MODES:
            log("settle.json: unknown mode %r, using off" % mode)
            mode = "off"
        quiet_secs = config.get("quiet_minutes", auto_settle.QUIET_MINUTES) * 60
        touched = load_json("touched.json", {})
        family = set(sub_links) | set(sub_links.values())
        done = {key: status["done"] for key, status in self.ticket_status.items()}
        suggested = set(load_json("settle-suggested.json", []))
        for rec in agents:
            st = self.state[rec["terminal_id"]]
            last_active = last_interaction(rec, st["last_used"], touched)
            verdict = auto_settle.evaluate(
                rec, st["unseen_done"], last_active, now, rec["terminal_id"] in family,
                self.ticket_rows(rec), self.session_prs(rec), done, quiet_secs,
            )
            self.links.setdefault(rec["pane_id"], {})["settle"] = {"ready": verdict.ready, "reason": verdict.reason}
            if mode == "off" or not verdict.ready:
                continue
            title = rec.get("terminal_title_stripped") or rec["agent"]
            if mode == "auto":
                if not self.evidence_fresh(rec):
                    continue
                try:
                    shelve(rec, "settled", settled_by="auto", settle_reason=verdict.reason)
                except (CannotShelve, OSError, RuntimeError) as e:
                    log("auto settle of %s failed: %s" % (rec["pane_id"], e))
                    continue
                log("auto settled %s: %s" % (rec["pane_id"], verdict.reason))
                notify("Auto-settled", "%s (%s)" % (title, verdict.reason))
            elif rec["agent_session"]["value"] not in suggested:
                suggested.add(rec["agent_session"]["value"])
                notify("Ready to settle", "%s (%s)" % (title, verdict.reason))
        save_json("settle-suggested.json", sorted(suggested))

    def evidence_fresh(self, rec):
        """True when the session's PRs and primary tickets were last looked up successfully a few minutes ago.
        Otherwise queue the lookups and say no: the next pass decides on the fresh data."""
        now = time.time()
        session = rec.get("agent_session") or {}
        pr_key = (rec.get("cwd"), session.get("value"))
        fresh = True
        if now - self.prs.get(pr_key, {}).get("ok_at", 0) > SETTLE_EVIDENCE_MAX_AGE_SECS:
            fresh = False
            if pr_key not in self.pr_queued:
                self.pr_queued.add(pr_key)
                self.pr_queue.put(pr_key)
        for row in self.ticket_rows(rec):
            if row["role"] != "primary":
                continue
            if now - self.ticket_status.get(row["key"], {}).get("ok_at", 0) > SETTLE_EVIDENCE_MAX_AGE_SECS:
                fresh = False
                if row["key"] not in self.ticket_queued:
                    self.ticket_queued.add(row["key"])
                    self.ticket_queue.put(row["key"])
        return fresh

    def report_extra(self, rec, layout):
        """Tokens beyond the 16 one report can hold: native sub agent rows, the shell mark, the snooze mark and the second ticket and PR row; the rows after it go in a source of their own."""
        session = rec.get("agent_session") or {}
        is_claude = rec["agent"] == "claude" and session.get("kind") == "id"
        tokens = native_tokens(native_subs.running(session["value"]) if is_claude else [], layout)
        tokens["snooze_mark"] = SNOOZE_ICON if layout["woken"] else None
        tokens["shell_mark"] = SHELL_ICON if is_claude and self.has_shell(rec) else None
        tokens[WAITING_TOKEN] = self.waiting_labels.get(rec["pane_id"])
        tokens.update(self.link_tokens(rec, layout, [1]))
        self.report_source(rec, NATIVE_SOURCE, tokens)
        self.report_source(rec, LINKS_SOURCE, self.link_tokens(rec, layout, [2, 3]))

    def report_source(self, rec, source, tokens):
        params = {"pane_id": rec["pane_id"], "source": source, "tokens": tokens}
        key = rec["pane_id"] + source
        if self.reported.get(key) == params:
            return
        herdr_request("pane.report_metadata", params)
        self.reported[key] = params

    def has_shell(self, rec):
        """True when the agent's claude process has a shell command running, in the foreground or the background."""
        return self.claude_pid(rec) in self.shell_parents

    def claude_pid(self, rec):
        if rec["pane_id"] not in self.claude_pids:
            try:
                self.claude_pids[rec["pane_id"]] = herdr_request("pane.process_info", {"pane_id": rec["pane_id"]})["process_info"]["foreground_process_group_id"]
            except PaneNotFound:
                self.claude_pids[rec["pane_id"]] = None
        return self.claude_pids[rec["pane_id"]]

    def report(self, rec, st, now, layout):
        shown = layout["status"]
        tokens = {token: None for token in STATE_TOKENS.values()}
        if shown:
            tokens[STATE_TOKENS[shown]] = STATE_LABELS[shown]
        tokens["sort_key"] = layout["sort_key"]
        tokens["hidden"] = "1" if layout["hidden"] else None
        tokens["title"] = rec.get("terminal_title_stripped")
        tokens["last_reply"] = reply_age(rec["agent_status"], st, now)
        tokens["hint"] = self.hint(rec)
        rows = self.ticket_rows(rec)
        prs = self.session_prs(rec)
        tokens.update(self.link_tokens(rec, layout, [0]))
        # Herdr allows 16 tokens per pane, so the actions read links from a file instead.
        self.links[rec["pane_id"]] = {"tickets": [r["key"] for r in rows], "ticket_roles": rows, "pr_urls": [p["url"] for p in prs]}
        hint = tokens["hint"]
        indent_tokens(tokens, layout, shown)
        self.waiting_labels[rec["pane_id"]] = tokens.pop(WAITING_TOKEN)
        params = {"pane_id": rec["pane_id"], "source": SOURCE, "tokens": tokens}
        # display_agent replaces the agent label herdr draws on pane borders.
        if hint:
            params["display_agent"] = hint
        else:
            params["clear_display_agent"] = True
        if self.reported.get(rec["pane_id"]) == params:
            return
        herdr_request("pane.report_metadata", params)
        self.reported[rec["pane_id"]] = params

    def newest_titled_agent_by(self, agents, key):
        """Per tab or workspace, the most recently used agent that has a generated title."""
        best = {}
        for rec in agents:
            if not self.session_title(rec):
                continue
            last_used = self.state[rec["terminal_id"]]["last_used"]
            if key(rec) not in best or last_used > best[key(rec)][0]:
                best[key(rec)] = (last_used, rec)
        return {group: rec for group, (_, rec) in best.items()}

    def rename_tabs(self, agents, tab_list, positions):
        """Name each tab after its agent's sidebar title, unless the user renamed it or several agents share the tab."""
        newest = self.newest_titled_agent_by(agents, lambda rec: rec["tab_id"])
        agent_counts = collections.Counter(rec["tab_id"] for rec in agents)
        self.tab_labels = {tab_id: label for tab_id, label in self.tab_labels.items() if tab_id in positions}
        for tab in tab_list:
            if agent_counts[tab["tab_id"]] > 1:
                continue
            rec = newest.get(tab["tab_id"])
            if not rec:
                # A cleared session has no title yet: drop the old one the plugin wrote.
                if agent_counts[tab["tab_id"]] and tab["label"] == self.tab_labels.get(tab["tab_id"]):
                    herdr_request("tab.rename", {"tab_id": tab["tab_id"], "label": str(positions[tab["tab_id"]])})
                    del self.tab_labels[tab["tab_id"]]
                continue
            tickets = (self.ticket(rec) or "").split()
            label = tab_titles.tab_label(positions[tab["tab_id"]], tickets[0] if tickets else None, self.session_title(rec))
            if tab["label"] != label and tab_titles.may_rename(tab["label"], self.tab_labels.get(tab["tab_id"])):
                herdr_request("tab.rename", {"tab_id": tab["tab_id"], "label": label})
            if tab["label"] == label or tab_titles.may_rename(tab["label"], self.tab_labels.get(tab["tab_id"])):
                self.tab_labels[tab["tab_id"]] = label
        save_json("tabs.json", self.tab_labels)

    def rename_panes(self, agents):
        """Label each pane with the session title the sidebar shows, unless the user renamed it.
        Herdr labels a pane once, so it keeps the first title after Claude renames the session.
        A label met for the first time on a pane is taken as herdr's, so it can be replaced."""
        panes = {p["pane_id"]: p for p in herdr_request("pane.list", {}).get("panes", [])}
        live = {rec["pane_id"] for rec in agents}
        self.pane_titles = {pane_id: seen for pane_id, seen in self.pane_titles.items() if pane_id in live}
        met = self.pane_labels
        self.pane_labels = {pane_id: label for pane_id, label in met.items() if pane_id in live}
        for rec in agents:
            title = rec.get("terminal_title_stripped")
            pane = panes.get(rec["pane_id"])
            if not title or not pane:
                continue
            seen = self.pane_titles.setdefault(rec["pane_id"], [])
            label = pane.get("label")
            if rec["pane_id"] not in met and label and label not in seen and label != os.path.basename(pane.get("cwd") or ""):
                seen.append(label)
            self.pane_labels[rec["pane_id"]] = label
            if title in tab_titles.GENERIC_PANE_TITLES:
                # A cleared session is unnamed again: drop the old title, back to herdr's default label.
                if label in seen:
                    herdr_request("pane.rename", {"pane_id": rec["pane_id"], "label": None})
                continue
            if label != title and tab_titles.may_rename_pane(label, seen, pane.get("cwd")):
                herdr_request("pane.rename", {"pane_id": rec["pane_id"], "label": title})
            if title not in seen:
                seen.append(title)
                del seen[:-PANE_TITLES_KEPT]
        save_json("pane-titles.json", self.pane_titles)
        save_json("pane-labels.json", self.pane_labels)

    def report_topics(self, agents):
        """Workspace topic for the sidebar: the title of the workspace's newest agent."""
        for workspace_id, rec in self.newest_titled_agent_by(agents, lambda rec: rec["workspace_id"]).items():
            topic = self.session_title(rec)
            if self.topics.get(workspace_id) != topic:
                herdr_request("workspace.report_metadata", {"workspace_id": workspace_id, "source": SOURCE, "tokens": {"topic": topic}})
                self.topics[workspace_id] = topic

    def prune_summaries(self):
        """Drop summaries of sessions not seen live for a month, unless they are settled."""
        settled = {e["session"] for e in load_json("settled.json", [])}
        cutoff = time.time() - SUMMARY_KEEP_SECS
        self.summaries = {sid: s for sid, s in self.summaries.items() if s["seen"] > cutoff or sid in settled}
        save_json("summaries.json", self.summaries)

    def hint(self, rec):
        """Summary generated once from the first two prompts; until then the first prompt."""
        session = rec.get("agent_session") or {}
        if rec["agent"] != "claude" or session.get("kind") != "id":
            return None
        session_id = session["value"]
        cached = self.summaries.get(session_id)
        if cached:
            cached["seen"] = time.time()
            return cached["text"]
        prompts = typed_prompts(session_id)
        if len(prompts) >= SUMMARY_MIN_PROMPTS and time.time() - self.summary_requested.get(session_id, 0) > SUMMARY_RETRY_SECS:
            self.summary_requested[session_id] = time.time()
            self.summary_queue.put(session_id)
        return prompts[0][:HINT_CHARS] if prompts else None

    def session_title(self, rec):
        """The title the sidebar shows, once Claude has named the session."""
        title = rec.get("terminal_title_stripped")
        return title if title and title not in tab_titles.GENERIC_PANE_TITLES else None

    def session_scan(self, rec):
        """Transcript scan of a Claude session. Rescans when the transcript grows, at most once per TICKET_RESCAN_SECS."""
        session = rec.get("agent_session") or {}
        if rec["agent"] != "claude" or session.get("kind") != "id":
            return None
        session_id = session["value"]
        path = transcript_path(session_id)
        if not path:
            return None
        stat = os.stat(path)
        signature = (stat.st_mtime_ns, stat.st_size)
        cached = self.scans.get(session_id)
        if cached and (cached["signature"] == signature or time.time() - cached["at"] < TICKET_RESCAN_SECS):
            return cached["scan"]
        found = session_tickets.scan(path)
        self.scans[session_id] = {"signature": signature, "at": time.time(), "scan": found}
        return found

    def ticket_rows(self, rec):
        """The session's tickets with their role and why: primary, touched or mentioned (see tickets.py)."""
        found = self.session_scan(rec)
        if not found:
            return []
        rows = session_tickets.classify(found, [p.get("title") for p in self.session_prs(rec)])
        prefixes = self.ticket_prefixes()
        return [r for r in rows if (prefixes is None or r["key"].rsplit("-", 1)[0] in prefixes) and not self.ticket_status.get(r["key"], {}).get("missing")]

    def ticket_prefixes(self):
        """Project keys a ticket may have: every project we can see plus tracked-prefixes.json, a list of extra ones to track.
        None until the project list has been fetched, so tickets are not hidden while it is unknown."""
        if not self.projects.get("keys"):
            return None
        return set(self.projects["keys"]) | set(load_json("tracked-prefixes.json", []))

    def ticket(self, rec):
        """Space separated keys worth showing, owned ones first."""
        shown, _ = session_tickets.display(self.ticket_rows(rec))
        return " ".join(shown) or None

    def session_prs(self, rec):
        """Cached PRs for the agent's branch and the PRs its session created; refreshed in the background."""
        session = rec.get("agent_session") or {}
        session_id = session.get("value") if rec["agent"] == "claude" and session.get("kind") == "id" else None
        key = (rec.get("cwd"), session_id)
        cached = self.prs.get(key)
        all_done = cached and all(p["state"] in ("MERGED", "CLOSED") for p in cached["prs"])
        max_age = PR_DONE_REFRESH_SECS if all_done else PR_REFRESH_SECS
        if (not cached or time.time() - cached["at"] > max_age) and key not in self.pr_queued:
            self.pr_queued.add(key)
            self.pr_queue.put(key)
        return cached["prs"] if cached else []

    def pr_loop(self):
        while True:
            key = self.pr_queue.get()
            try:
                self.prs[key] = {"prs": github_prs.session_prs(*key), "at": time.time(), "ok_at": time.time()}
                self.dirty.set()
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as e:
                log("PR lookup failed for %s: %s" % (key[0], e))
                # Keep any previous result and wait out the refresh interval before retrying.
                previous = self.prs.get(key, {})
                self.prs[key] = {"prs": previous.get("prs", []), "at": time.time(), "ok_at": previous.get("ok_at", 0)}
            finally:
                self.pr_queued.discard(key)

    def shown_tickets(self, rows):
        """(keys to show, number of mentioned keys left out). A ticket is shown once Jira has confirmed it exists."""
        keys, mentioned = session_tickets.display(rows)
        return self.checked_tickets(keys), mentioned

    def checked_tickets(self, keys):
        """Queue a lookup for every key that is due one, and return those Jira has confirmed exist."""
        checked = []
        for key in keys:
            cached = self.ticket_status.get(key)
            max_age = TICKET_DONE_REFRESH_SECS if cached and cached["done"] else TICKET_REFRESH_SECS
            if (not cached or time.time() - cached["at"] > max_age) and key not in self.ticket_queued:
                self.ticket_queued.add(key)
                self.ticket_queue.put(key)
            if cached and cached["ok_at"] and not cached.get("missing"):
                checked.append(key)
        # Done tickets last; sorted() keeps the existing order otherwise.
        return sorted(checked, key=lambda key: self.ticket_status[key]["done"])

    def ticket_cells(self, rec):
        """Ticket keys that are not done yet; the done and mentioned ones are only counted, as a +N after the last."""
        keys, mentioned = self.shown_tickets(self.ticket_rows(rec))
        open_keys = [key for key in keys if not self.ticket_status[key]["done"]]
        hidden = mentioned + len(keys) - len(open_keys)
        if not hidden:
            return open_keys
        if not open_keys:
            return ["+%d" % hidden]
        return [*open_keys[:-1], "%s +%d" % (open_keys[-1], hidden)]

    def rows_for(self, rec, layout):
        indent = len(continuation_pad(layout)) if layout["depth"] else 0
        return link_rows(self.ticket_cells(rec), self.session_prs(rec), LINK_ROW_WIDTH - indent)

    def link_tokens(self, rec, layout, indexes):
        """Tokens of the given ticket and PR rows. The first row is indented by indent_tokens, the others here."""
        rows = self.rows_for(rec, layout)
        tokens = {}
        for n in indexes:
            row = link_row_tokens(rows[n], TICKET_TOKENS[n], github_prs.PR_ROW_TOKENS[n])
            first = next((name for name in (TICKET_TOKENS[n], *github_prs.PR_ROW_TOKENS[n].values()) if row[name]), None)
            if first and n and layout["depth"]:
                row[first] = continuation_pad(layout) + row[first]
            tokens.update(row)
        return tokens

    def ticket_loop(self):
        while True:
            key = self.ticket_queue.get()
            previous = self.ticket_status.get(key, {"done": False})
            try:
                self.ticket_status[key] = {"done": jira_status.is_done(key), "at": time.time(), "ok_at": time.time()}
                self.dirty.set()
            except jira_status.TicketNotFound:
                self.ticket_status[key] = {"done": False, "missing": True, "at": time.time(), "ok_at": time.time()}
                self.dirty.set()
            except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as e:
                log("ticket lookup failed for %s: %s" % (key, e))
                self.ask_for_jira_login(e)
                # Keep any previous status and wait out the refresh interval before retrying.
                self.ticket_status[key] = {"done": previous["done"], "at": time.time(), "ok_at": previous.get("ok_at", 0), "missing": previous.get("missing", False)}
            finally:
                self.ticket_queued.discard(key)

    def ask_for_jira_login(self, error):
        if isinstance(error, jira_status.LoginRequired) and jira_status.start_login():
            notify("Jira login needed", "Log in to Jira in the popup so ticket status keeps working.")

    def projects_loop(self):
        while True:
            if time.time() - self.projects.get("at", 0) > PROJECTS_REFRESH_SECS:
                try:
                    self.projects = {"keys": jira_status.project_keys(), "at": time.time()}
                    save_json("jira-projects.json", self.projects)
                    self.dirty.set()
                except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as e:
                    log("project lookup failed: %s" % e)
                    self.ask_for_jira_login(e)
                    time.sleep(PROJECTS_RETRY_SECS)
                    continue
            time.sleep(PROJECTS_REFRESH_SECS)

    def summary_loop(self):
        while True:
            session_id = self.summary_queue.get()
            try:
                generated = summarize(typed_prompts(session_id))
            except (OSError, subprocess.SubprocessError) as e:
                log("summary failed: %s" % e)
                continue
            if generated:
                self.summaries[session_id] = {"text": generated, "seen": time.time()}
                self.dirty.set()

    def events_loop(self):
        backoff = 1.0
        while True:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.connect(socket_path())
                    request = {"id": "sub", "method": "events.subscribe", "params": {"subscriptions": SUBSCRIPTIONS}}
                    s.sendall((json.dumps(request) + "\n").encode())
                    backoff = 1.0
                    # A reconnect can mean herdr restarted and lost the view and metadata.
                    self.reported.clear()
                    self.topics.clear()
                    self.assert_view()
                    self.dirty.set()
                    while chunk := s.recv(1 << 16):
                        self.dirty.set()
                    raise OSError("event stream closed")
            except (OSError, RuntimeError) as e:
                log("event stream down (%s); retrying in %.0fs" % (e, backoff))
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def run(self):
        # SIGUSR1 (sent by the toggle action) re-renders right away after collapsed.json changes.
        signal.signal(signal.SIGUSR1, lambda *_: self.dirty.set())
        threading.Thread(target=self.events_loop, daemon=True).start()
        threading.Thread(target=self.summary_loop, daemon=True).start()
        threading.Thread(target=self.pr_loop, daemon=True).start()
        threading.Thread(target=self.ticket_loop, daemon=True).start()
        threading.Thread(target=self.projects_loop, daemon=True).start()
        while True:
            if self.dirty.wait(TICK_SECS):
                time.sleep(DEBOUNCE_SECS)
                self.dirty.clear()
            try:
                self.refresh()
            except (OSError, RuntimeError, ValueError) as e:
                log("refresh failed: %s" % e)


def stop_running():
    try:
        with open(pid_path()) as f:
            os.kill(int(f.read()), signal.SIGTERM)
    except (OSError, ValueError):
        return
    for _ in range(50):
        lock = acquire_lock()
        if lock:
            lock.close()
            return
        time.sleep(0.1)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if mode == "--restart":
        stop_running()
        spawn_detached()
        return
    if mode == "--ensure":
        lock = acquire_lock()
        if lock:
            lock.close()
            spawn_detached()
        return
    lock = acquire_lock()
    if not lock:
        return
    with open(pid_path(), "w") as f:
        f.write(str(os.getpid()))
    log("daemon started (pid %d)" % os.getpid())
    Daemon().run()


if __name__ == "__main__":
    main()
