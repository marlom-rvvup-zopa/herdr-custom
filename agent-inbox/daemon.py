#!/usr/bin/env python3
"""Agent inbox daemon.

Sorts the Agents panel by last use (the moment an agent starts working, i.e.
a prompt was sent) and reports one state token per agent so the sidebar can
show working / needs input / done / idle in distinct colors.

Usage: daemon.py            run the daemon (exits if one is already running)
       daemon.py --ensure   start the daemon detached if it is not running
       daemon.py --restart  stop the running daemon and start a fresh one
"""

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
import auto_settle
import tab_titles
import tickets as session_tickets
from lib import CannotShelve, ago_text, family_root, herdr_request, is_stale, last_interaction, lineage, load_json, notify, prune_sub_links, resume, save_json, shelve, socket_path, state_dir

SOURCE = "marlom.agent-inbox"
HINT_CHARS = 100
JIRA_ICON = chr(0xf0303)
# Herdr trims plain whitespace from token values, so indentation uses the braille blank.
BLANK = "\u2800"
# Tree glyphs for sub agents: the last visible child of a parent closes its branch.
SUB_MARK = "├─ "
LAST_SUB_MARK = "└─ "
SUB_GUTTER = "│"
# Native sub agents are listed on their parent's row, in tokens of their own: one report holds at most 16 tokens.
NATIVE_SOURCE = "marlom.agent-inbox.native"
NATIVE_ROWS = 4
PANE_TITLES_KEPT = 20
# Shown after the status of an agent that came back from a snooze and has not been touched since.
SNOOZE_ICON = BLANK + chr(0xf068e)
NATIVE_TOKENS = ["native_%d" % n for n in range(1, NATIVE_ROWS + 1)]
PR_ICON = chr(0xf407)
PR_REFRESH_SECS = 120
PR_DONE_REFRESH_SECS = 1800
TICKET_REFRESH_SECS = 300
TICKET_DONE_REFRESH_SECS = 3600
SUMMARY_PROMPT_CHARS = 1000
SUMMARY_MIN_PROMPTS = 2
SUMMARY_TIMEOUT_SECS = 90
SUMMARY_RETRY_SECS = 600
SUMMARY_KEEP_SECS = 30 * 24 * 3600
TICKET_RESCAN_SECS = 60
# Settling is hard to notice when wrong, so it only goes by PR and Jira data this recent.
SETTLE_EVIDENCE_MAX_AGE_SECS = 300
SUMMARY_SYSTEM_PROMPT = (
    "You get the first two prompts of a coding-agent session. Output exactly two lines and nothing else. "
    "Line 1: a title of at most 5 words naming the task, without ticket ids or quotes. "
    "Line 2: a summary of what the session is about in at most 12 words."
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
}
STATE_TOKENS = {status: "st_" + status for status in STATE_LABELS}


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
    """Ask Claude for (title, summary) of the session, or None on failure."""
    claude = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    text = "First prompt: %s\nSecond prompt: %s" % (prompts[0][:SUMMARY_PROMPT_CHARS], prompts[1][:SUMMARY_PROMPT_CHARS])
    # --bare skips the user's hooks; cwd /tmp keeps project instructions out of the call.
    result = subprocess.run(
        [claude, "-p", "--bare", "--no-session-persistence", "--model", "haiku", "--system-prompt", SUMMARY_SYSTEM_PROMPT],
        input=text, capture_output=True, text=True, timeout=SUMMARY_TIMEOUT_SECS, cwd="/tmp",
    )
    parsed = tab_titles.parse_title_and_summary(result.stdout)
    if result.returncode != 0 or not parsed:
        log("summary failed (exit %d): %s %s" % (result.returncode, result.stderr.strip()[:200], result.stdout.strip()[:200]))
        return None
    return parsed[0], parsed[1][:HINT_CHARS]


def layouts(agents, state, parents, collapsed, woken):
    """Per terminal: sub agent depth, sort key and visibility. A family sorts by its most recent member,
    and a family with an agent back from a snooze sorts before every other one."""
    live = {a["terminal_id"]: a for a in agents}
    parents = {child: parent for child, parent in parents.items() if child in live and parent in live}
    families = {}
    for tid in live:
        families.setdefault(family_root(tid, parents)[0], []).append(tid)
    out = {}
    for root, members in families.items():
        family_time = max(state[tid]["last_used"] for tid in members)
        back = any(tid in woken for tid in members)
        for tid in members:
            depth = family_root(tid, parents)[1]
            # The key spells out the path from the root, one recency segment per sub agent, closed by "9".
            # Sorted desc, a parent ("9") comes before its children ("5...") and each subtree stays contiguous.
            path = lineage(tid, parents)[-2::-1]
            tail = "".join("5%012d%s" % (state[node]["last_used"], node[-6:].rjust(6)) for node in path) + "9"
            out[tid] = {
                "depth": depth,
                "woken": tid in woken,
                "sort_key": "%d%012d-%s-%s" % (1 + back, family_time, root[-6:], tail),
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
    # Rows below the first continue the branch with a gutter line, except under the closing child.
    gutter = BLANK if layout["last_child"] else SUB_GUTTER
    cont = pad + gutter + BLANK * (len(mark) - 1)
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


def native_tokens(subs, layout):
    """One tree row per running native sub agent, hanging under the parent's own rows."""
    tokens = {name: None for name in NATIVE_TOKENS}
    if not subs or layout["hidden_subs"]:
        return tokens
    # Rows hang one level below the parent, in the column its own children use.
    pad = BLANK * 2 if not layout["depth"] else layout["guides"] + (BLANK if layout["last_child"] else SUB_GUTTER) + BLANK * 2
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
        self.topics = {}
        self.ticket_status = {}
        self.ticket_queue = queue.Queue()
        self.ticket_queued = set()
        self.links = {}
        self.prs = {}
        self.pr_queue = queue.Queue()
        self.pr_queued = set()

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
        layout = layouts(agents, self.state, links, set(load_json("collapsed.json", [])), woken)
        for rec in agents:
            self.report(rec, self.state[rec["terminal_id"]], now, layout[rec["terminal_id"]])
            self.report_extra(rec, layout[rec["terminal_id"]])
        self.settle_pass(agents, links, now)
        self.rename_tabs(agents, tab_list, positions)
        self.rename_panes(agents)
        self.report_topics(agents)
        live = {a["terminal_id"] for a in agents}
        prune_sub_links(live)
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
            try:
                woken[resume(entry, focus=False)] = now
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as e:
                # Try again in five minutes rather than every tick.
                log("wake failed for %s: %s" % (entry["title"], e))
                save_json("snoozed.json", load_json("snoozed.json", []) + [{**entry, "wake_at": now + 300}])
        save_json("woken.json", woken)

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
        """Tokens beyond the 16 one report can hold: native sub agent rows and the snooze mark."""
        session = rec.get("agent_session") or {}
        is_claude = rec["agent"] == "claude" and session.get("kind") == "id"
        tokens = native_tokens(native_subs.running(session["value"]) if is_claude else [], layout)
        tokens["snooze_mark"] = SNOOZE_ICON if layout["woken"] else None
        params = {"pane_id": rec["pane_id"], "source": NATIVE_SOURCE, "tokens": tokens}
        key = rec["pane_id"] + NATIVE_SOURCE
        if self.reported.get(key) == params:
            return
        herdr_request("pane.report_metadata", params)
        self.reported[key] = params

    def report(self, rec, st, now, layout):
        shown = display_status(rec["agent_status"], st["unseen_done"])
        tokens = {token: None for token in STATE_TOKENS.values()}
        if shown:
            tokens[STATE_TOKENS[shown]] = STATE_LABELS[shown]
        tokens["sort_key"] = layout["sort_key"]
        tokens["hidden"] = "1" if layout["hidden"] else None
        tokens["title"] = rec.get("terminal_title_stripped")
        tokens["last_reply"] = reply_age(rec["agent_status"], st, now)
        tokens["hint"] = self.hint(rec)
        rows = self.ticket_rows(rec)
        keys, mentioned = session_tickets.display(rows)
        if keys:
            tokens["ticket_line"] = "%s %s%s" % (JIRA_ICON, self.ticket_text(" ".join(keys)), " +%d" % mentioned if mentioned else "")
        else:
            tokens["ticket_line"] = None
        prs = self.session_prs(rec)
        tokens.update(github_prs.tokens(prs, PR_ICON))
        # Herdr allows 16 tokens per pane, so the actions read links from a file instead.
        self.links[rec["pane_id"]] = {"tickets": [r["key"] for r in rows], "ticket_roles": rows, "pr_urls": [p["url"] for p in prs]}
        hint = tokens["hint"]
        indent_tokens(tokens, layout, shown)
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
        """Name each tab after its newest agent's generated title, unless the user renamed it."""
        newest = self.newest_titled_agent_by(agents, lambda rec: rec["tab_id"])
        self.tab_labels = {tab_id: label for tab_id, label in self.tab_labels.items() if tab_id in positions}
        for tab in tab_list:
            rec = newest.get(tab["tab_id"])
            if not rec:
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
        Herdr labels a pane once, so it keeps the first title after Claude renames the session."""
        panes = {p["pane_id"]: p for p in herdr_request("pane.list", {}).get("panes", [])}
        live = {rec["pane_id"] for rec in agents}
        self.pane_titles = {pane_id: seen for pane_id, seen in self.pane_titles.items() if pane_id in live}
        for rec in agents:
            title = rec.get("terminal_title_stripped")
            pane = panes.get(rec["pane_id"])
            if not title or not pane or title in tab_titles.GENERIC_PANE_TITLES:
                continue
            seen = self.pane_titles.setdefault(rec["pane_id"], [])
            if pane.get("label") != title and tab_titles.may_rename_pane(pane.get("label"), seen, pane.get("cwd")):
                herdr_request("pane.rename", {"pane_id": rec["pane_id"], "label": title})
            if title not in seen:
                seen.append(title)
                del seen[:-PANE_TITLES_KEPT]
        save_json("pane-titles.json", self.pane_titles)

    def report_topics(self, agents):
        """Workspace topic for the sidebar: the generated title of the workspace's newest agent."""
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
        # Summaries saved before titles existed are regenerated once.
        if cached and "title" in cached:
            return cached["text"]
        prompts = typed_prompts(session_id)
        if len(prompts) >= SUMMARY_MIN_PROMPTS and time.time() - self.summary_requested.get(session_id, 0) > SUMMARY_RETRY_SECS:
            self.summary_requested[session_id] = time.time()
            self.summary_queue.put(session_id)
        if cached:
            return cached["text"]
        return prompts[0][:HINT_CHARS] if prompts else None

    def session_title(self, rec):
        session = rec.get("agent_session") or {}
        return (self.summaries.get(session.get("value")) or {}).get("title")

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
        return session_tickets.classify(found, [p.get("title") for p in self.session_prs(rec)])

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

    def ticket_text(self, tickets):
        """Ticket keys, struck through once Jira reports them done; status is looked up in the background."""
        keys = []
        for key in tickets.split():
            cached = self.ticket_status.get(key)
            max_age = TICKET_DONE_REFRESH_SECS if cached and cached["done"] else TICKET_REFRESH_SECS
            if (not cached or time.time() - cached["at"] > max_age) and key not in self.ticket_queued:
                self.ticket_queued.add(key)
                self.ticket_queue.put(key)
            keys.append(github_prs.strike(key) if cached and cached["done"] else key)
        return " ".join(keys)

    def ticket_loop(self):
        while True:
            key = self.ticket_queue.get()
            previous = self.ticket_status.get(key, {"done": False})
            try:
                self.ticket_status[key] = {"done": jira_status.is_done(key), "at": time.time(), "ok_at": time.time()}
                self.dirty.set()
            except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as e:
                log("ticket lookup failed for %s: %s" % (key, e))
                # Keep any previous status and wait out the refresh interval before retrying.
                self.ticket_status[key] = {"done": previous["done"], "at": time.time(), "ok_at": previous.get("ok_at", 0)}
            finally:
                self.ticket_queued.discard(key)

    def summary_loop(self):
        while True:
            session_id = self.summary_queue.get()
            try:
                generated = summarize(typed_prompts(session_id))
            except (OSError, subprocess.SubprocessError) as e:
                log("summary failed: %s" % e)
                continue
            if generated:
                self.summaries[session_id] = {"title": generated[0], "text": generated[1], "seen": time.time()}
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
