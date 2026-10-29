"""Shared helpers: herdr socket requests and the plugin's on-disk state."""

import fcntl
import json
import os
import socket
import subprocess

MAX_SUB_DEPTH = 5
RESUME_COMMANDS = {
    "claude": "claude --resume {}",
    "codex": "codex resume {}",
}


def socket_path():
    return os.environ.get("HERDR_SOCKET_PATH") or os.path.expanduser("~/.config/herdr/herdr.sock")


def state_dir():
    path = os.path.join(os.path.dirname(socket_path()), "agent-inbox-state")
    os.makedirs(path, exist_ok=True)
    return path


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
        raise RuntimeError("%s: %s" % (method, resp["error"]))
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


def prune_sub_links(live_terminals):
    """Forget sub agents whose terminals are gone."""
    with open(os.path.join(state_dir(), "subagents.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        links = load_json("subagents.json", {})
        kept = {c: p for c, p in links.items() if c in live_terminals}
        if kept != links:
            save_json("subagents.json", kept)


def family_root(terminal_id, parents):
    """Top-most ancestor of a terminal and how many levels it sits below it."""
    depth = 0
    while terminal_id in parents and depth < MAX_SUB_DEPTH:
        terminal_id = parents[terminal_id]
        depth += 1
    return terminal_id, depth
