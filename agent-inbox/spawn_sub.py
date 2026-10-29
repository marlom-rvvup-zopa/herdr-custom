#!/usr/bin/env python3
"""Run a prompt in a herdr sub agent linked to the calling Claude session, and print its answer.

Usage: spawn_sub.py <label> <prompt>

The sub agent is a Claude session in its own herdr tab: visible in the sidebar under its parent,
resumable on its own. This command blocks until the sub agent finishes, so run it in the
background (or with a long timeout) when the answer is not needed immediately.
"""

import os
import secrets
import sys

from lib import herdr_request, link_sub_agent, state_dir

WAIT_TIMEOUT_MS = 30 * 60 * 1000
START_TIMEOUT_MS = 60 * 1000
TAB_LABEL_CHARS = 28


def terminal_id(pane_id):
    return next(a["terminal_id"] for a in herdr_request("agent.list", {})["agents"] if a["pane_id"] == pane_id)


def main(label, prompt):
    if os.environ.get("HERDR_ENV") != "1":
        sys.exit("Not running inside herdr: use the built-in Agent tool instead.")
    parent_pane = os.environ["HERDR_PANE_ID"]
    name = "sub-" + secrets.token_hex(3)
    result_file = os.path.join(state_dir(), "sub-results", name + ".md")
    os.makedirs(os.path.dirname(result_file), exist_ok=True)

    tab = herdr_request("tab.create", {
        "workspace_id": os.environ["HERDR_WORKSPACE_ID"],
        "cwd": os.getcwd(),
        "label": ("sub · " + label)[:TAB_LABEL_CHARS],
        "focus": False,
    })
    pane_id = tab["root_pane"]["pane_id"]
    herdr_request("agent.start", {"name": name, "kind": "claude", "pane_id": pane_id}, timeout=60)
    link_sub_agent(terminal_id(pane_id), terminal_id(parent_pane))
    # agent.start can return while Claude is still booting; prompts are rejected until it is idle.
    herdr_request("agent.wait", {"target": name, "until": ["idle"], "timeout_ms": START_TIMEOUT_MS}, timeout=START_TIMEOUT_MS / 1000 + 10)

    full_prompt = "%s\n\nWhen you are completely done, write your full final answer as Markdown to %s and then stop." % (prompt, result_file)
    herdr_request(
        "agent.prompt",
        {"target": name, "text": full_prompt, "wait": {"until": ["idle", "done"], "timeout_ms": WAIT_TIMEOUT_MS}},
        timeout=WAIT_TIMEOUT_MS / 1000 + 30,
    )

    print("[sub agent %s, herdr tab %s]" % (name, tab["tab"]["tab_id"]))
    if os.path.exists(result_file):
        with open(result_file) as f:
            print(f.read())
        return
    # The agent did not write the file: fall back to what is on its screen.
    print(herdr_request("agent.read", {"target": name, "source": "recent-unwrapped", "lines": 200})["text"])


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
