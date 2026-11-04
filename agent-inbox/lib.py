"""Shared helpers: herdr socket requests and the plugin's on-disk state."""

import fcntl
import glob
import json
import os
import re
import shlex
import socket
import subprocess
import time

MAX_SUB_DEPTH = 5
RESUME_COMMANDS = {
    "claude": "claude --resume {}",
    "codex": "codex resume {}",
}


# Snooze durations offered in the inbox, in the order they are listed.
SNOOZE_CHOICES = [("4 hours", 4 * 3600), ("1 day", 86400), ("3 days", 3 * 86400), ("7 days", 7 * 86400)]
# An agent nobody has touched for this long is archived as stale.
STALE_AFTER_SECS = 24 * 3600
# Sessions shelved out of the agents list, one file each: name -> file.
STORES = ("settled", "stale", "snoozed")


class CannotShelve(Exception):
    """The agent cannot be closed without losing its session."""


def socket_path():
    return os.environ.get("HERDR_SOCKET_PATH") or os.path.expanduser("~/.config/herdr/herdr.sock")


def state_dir():
    path = os.path.join(os.path.dirname(socket_path()), "agent-inbox-state")
    os.makedirs(path, exist_ok=True)
    return path


class PaneNotFound(RuntimeError):
    """The pane closed between listing it and asking about it."""


def herdr_request(method, params, timeout=10.0):
    """One request per connection; herdr closes it after replying."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(socket_path())
        s.sendall((json.dumps({"id": "inbox", "method": method, "params": params}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(1 << 16)
            if not chunk:
                break
            buf += chunk
    if not buf:
        raise RuntimeError("%s: connection closed without a reply" % method)
    resp = json.loads(buf)
    if "error" in resp:
        error = PaneNotFound if resp["error"].get("code") == "pane_not_found" else RuntimeError
        raise error("%s: %s" % (method, resp["error"]))
    return resp.get("result") or {}


def load_json(name, default):
    try:
        with open(os.path.join(state_dir(), name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(name, data):
    path = os.path.join(state_dir(), name)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def notify(title, body=None):
    argv = [os.environ.get("HERDR_BIN_PATH", "herdr"), "notification", "show", title]
    if body:
        argv += ["--body", body]
    subprocess.run(argv, check=False, capture_output=True)


def link_sub_agent(child_terminal_id, parent_terminal_id):
    """Record that a sub agent was spawned by a parent agent. Parallel spawns serialize on a lock."""
    with open(os.path.join(state_dir(), "subagents.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        links = load_json("subagents.json", {})
        links[child_terminal_id] = parent_terminal_id
        save_json("subagents.json", links)


def prune_sub_links(read_live_terminals):
    """Forget sub agents whose terminals are gone. The terminals are read under the lock: a link is written after
    its terminal is listed, so a list read after taking the lock always has the terminal of every link."""
    with open(os.path.join(state_dir(), "subagents.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        live_terminals = read_live_terminals()
        links = load_json("subagents.json", {})
        kept = {c: p for c, p in links.items() if c in live_terminals}
        if kept != links:
            save_json("subagents.json", kept)


def lineage(terminal_id, parents):
    """The terminal followed by its ancestors, nearest first, ending at the family root."""
    chain = [terminal_id]
    while chain[-1] in parents and len(chain) <= MAX_SUB_DEPTH:
        chain.append(parents[chain[-1]])
    return chain


def family_root(terminal_id, parents):
    """Top-most ancestor of a terminal and how many levels it sits below it."""
    chain = lineage(terminal_id, parents)
    return chain[-1], len(chain) - 1


def transcript_mtime(session_id):
    """Last write to a Claude session's transcript, or None when it has none."""
    if not re.fullmatch(r"[A-Za-z0-9-]+", session_id or ""):
        return None
    paths = glob.glob(os.path.expanduser("~/.claude/projects/*/%s.jsonl" % session_id))
    return os.stat(paths[0]).st_mtime if paths else None


def last_interaction(rec, last_used, touched):
    """When the agent was last touched: its transcript's last write or the last time it worked, whichever the
    agent has; a session the inbox resumed counts from the resume, because its transcript is still old."""
    session = rec.get("agent_session") or {}
    written = transcript_mtime(session["value"]) if session.get("kind") == "id" else None
    return max(written or last_used, touched.get(rec["terminal_id"], 0))


def is_stale(rec, last_used, touched, now):
    """Idle for over a day. An agent working, waiting on the user or in front of them is never stale."""
    if rec["agent_status"] in ("working", "blocked") or rec.get("focused"):
        return False
    return now - last_interaction(rec, last_used, touched) > STALE_AFTER_SECS


def shelve(agent, store, **extra):
    """Close an agent's pane and keep its session in a store so it can be resumed later."""
    session = agent.get("agent_session") or {}
    if agent["agent_status"] in ("working", "blocked"):
        raise CannotShelve("Agent is %s. Wait until it is idle." % agent["agent_status"])
    if agent["agent"] not in RESUME_COMMANDS or session.get("kind") != "id":
        raise CannotShelve("Cannot resume %s sessions, so closing would lose it." % agent["agent"])
    links = load_json("links.json", {}).get(agent["pane_id"], {})
    entry = {
        "agent": agent["agent"],
        "session": session["value"],
        "cwd": agent["cwd"],
        "workspace_id": agent["workspace_id"],
        "workspace_label": herdr_request("workspace.get", {"workspace_id": agent["workspace_id"]})["workspace"]["label"],
        "title": agent.get("terminal_title_stripped") or agent["agent"],
        "tickets": links.get("tickets", []),
        "pr_urls": links.get("pr_urls", []),
        "shelved_at": time.time(),
        **extra,
    }
    # A session sits in one store only.
    for name in STORES:
        kept = [e for e in load_json(name + ".json", []) if e["session"] != entry["session"]]
        if name == store:
            kept.insert(0, entry)
        if name == store or len(kept) != len(load_json(name + ".json", [])):
            save_json(name + ".json", kept)
    herdr_request("pane.close", {"pane_id": agent["pane_id"]})
    return entry


def forget(store, session_id):
    save_json(store + ".json", [e for e in load_json(store + ".json", []) if e["session"] != session_id])


def herdr_cli(*args):
    out = subprocess.run([os.environ.get("HERDR_BIN_PATH", "herdr"), *args], check=True, capture_output=True, text=True).stdout
    # Some commands, such as `pane run`, print nothing on success.
    return json.loads(out)["result"] if out.strip() else {}


def resume(entry, focus):
    """Reopen a stored session in its original workspace, or in a new one if that is gone.
    Returns the new terminal id and marks it touched, so a stale transcript does not shelve it again at once."""
    label = entry.get("workspace_label") or os.path.basename(entry["cwd"])
    workspaces = herdr_cli("workspace", "list")["workspaces"]
    workspace_id = next(
        (w["workspace_id"] for w in workspaces if w["workspace_id"] == entry["workspace_id"]),
        next((w["workspace_id"] for w in workspaces if w["label"] == label), None),
    )
    focus_flag = ["--focus"] if focus else []
    if workspace_id:
        pane = herdr_cli("tab", "create", "--workspace", workspace_id, "--cwd", entry["cwd"], *focus_flag)["root_pane"]
        if focus:
            herdr_cli("workspace", "focus", workspace_id)
    else:
        pane = herdr_cli("workspace", "create", "--cwd", entry["cwd"], "--label", label, *focus_flag)["root_pane"]
    herdr_cli("pane", "run", pane["pane_id"], RESUME_COMMANDS[entry["agent"]].format(shlex.quote(entry["session"])))
    touched = load_json("touched.json", {})
    touched[pane["terminal_id"]] = time.time()
    save_json("touched.json", touched)
    return pane["terminal_id"]


def ago_text(seconds):
    """Compact age such as "just now", "5m ago", "2h ago", "3d ago"."""
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return "%d%s ago" % (seconds // size, unit)
    return "just now"
