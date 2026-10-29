#!/usr/bin/env python3
"""Claude Code PreToolUse hook: inside herdr, send sub agent requests to spawn_sub.py instead."""

import json
import os
import sys

SPAWN_SUB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spawn_sub.py")
REASON = (
    "Inside herdr, do not use the built-in Agent tool. Spawn a herdr sub agent instead: run this in Bash with "
    "run_in_background: true and wait for the completion notification:\n"
    "python3 {spawn} '<short label>' '<full, self-contained prompt>'\n"
    "It starts a Claude session in its own herdr tab, linked to this session and visible nested under it in the "
    "sidebar, and prints the sub agent's final answer when it finishes. The sub agent has no memory of this "
    "conversation, so put all the context it needs in the prompt. Start independent sub agents as separate "
    "background Bash commands so they run in parallel."
).format(spawn=SPAWN_SUB)

if os.environ.get("HERDR_ENV") == "1":
    json.dump({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": REASON,
    }}, sys.stdout)
