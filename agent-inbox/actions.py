#!/usr/bin/env python3
"""Plugin actions. Usage: actions.py settle|open-ticket"""

import json
import os
import re
import signal
import subprocess
import sys
import time

from lib import RESUME_COMMANDS, family_root, herdr_request, load_json, notify, save_json, state_dir

JIRA_BROWSE_URL = "https://zopapos.atlassian.net/browse/"
TICKET_PATTERN = re.compile(r"[A-Z][A-Z0-9]{1,9}-\d+")


def invocation_context():
    return json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")


def focused_pane_id():
    return invocation_context().get("focused_pane_id")


def settle():
    pane_id = focused_pane_id()
    agent = next((a for a in herdr_request("agent.list", {})["agents"] if a["pane_id"] == pane_id), None)
    if not agent:
        notify("Settle failed", "No agent in the focused pane.")
        return 1
    if agent["agent_status"] in ("working", "blocked"):
        notify("Settle failed", "Agent is %s. Wait until it is idle." % agent["agent_status"])
        return 1
    session = agent.get("agent_session") or {}
    if agent["agent"] not in RESUME_COMMANDS or session.get("kind") != "id":
        notify("Settle failed", "Cannot resume %s sessions, so closing would lose it." % agent["agent"])
        return 1
    settled = [e for e in load_json("settled.json", []) if e["session"] != session["value"]]
    settled.insert(0, {
        "agent": agent["agent"],
        "session": session["value"],
        "cwd": agent["cwd"],
        "workspace_id": agent["workspace_id"],
        "workspace_label": herdr_request("workspace.get", {"workspace_id": agent["workspace_id"]})["workspace"]["label"],
        "title": agent.get("terminal_title_stripped") or agent["agent"],
        "settled_at": time.time(),
    })
    save_json("settled.json", settled)
    herdr_request("pane.close", {"pane_id": pane_id})
    notify("Settled", settled[0]["title"])
    return 0


def open_ticket():
    """Open the clicked ticket, or every ticket the focused agent's session mentions."""
    clicked = invocation_context().get("clicked_url")
    if clicked:
        keys = TICKET_PATTERN.findall(clicked)
    else:
        keys = load_json("links.json", {}).get(focused_pane_id(), {}).get("tickets", [])
    if not keys:
        notify("No ticket", "Nothing to open for the focused agent.")
        return 1
    for key in keys:
        subprocess.run(["open", JIRA_BROWSE_URL + key], check=True)
    return 0


def open_pr():
    """Open every PR attached to the focused agent."""
    urls = load_json("links.json", {}).get(focused_pane_id(), {}).get("pr_urls", [])
    if not urls:
        notify("No PR", "No pull request found for the focused agent.")
        return 1
    for url in urls:
        subprocess.run(["open", url], check=True)
    return 0


def toggle_subs():
    """Collapse or expand the sub agents of the focused agent's family."""
    pane_id = focused_pane_id()
    agents = herdr_request("agent.list", {})["agents"]
    agent = next((a for a in agents if a["pane_id"] == pane_id), None)
    if not agent:
        notify("Sub agents", "No agent in the focused pane.")
        return 1
    parents = load_json("subagents.json", {})
    root = family_root(agent["terminal_id"], parents)[0]
    if not any(family_root(child, parents)[0] == root for child in parents):
        notify("Sub agents", "This agent has no sub agents.")
        return 1
    collapsed = set(load_json("collapsed.json", []))
    collapsed ^= {root}
    save_json("collapsed.json", sorted(collapsed))
    with open(os.path.join(state_dir(), "daemon.pid")) as f:
        os.kill(int(f.read()), signal.SIGUSR1)
    notify("Sub agents", "Collapsed." if root in collapsed else "Expanded.")
    return 0


ACTIONS = {"settle": settle, "open-ticket": open_ticket, "open-pr": open_pr, "toggle-subs": toggle_subs}

if __name__ == "__main__":
    action = ACTIONS.get(sys.argv[1] if len(sys.argv) > 1 else "")
    if not action:
        sys.exit("usage: actions.py settle|open-ticket|open-pr|toggle-subs")
    sys.exit(action())
