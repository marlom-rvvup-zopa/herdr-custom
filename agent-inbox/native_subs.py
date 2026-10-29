"""Claude Code's own sub agents (Agent tool) have no herdr pane. Claude writes one meta file per sub agent
under ~/.claude/projects/<project>/<session id>/subagents/, so a parent agent's running sub agents can be found
from its session id, which herdr reports for every Claude pane."""

import glob
import json
import os
import time

PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
# A running sub agent writes its transcript constantly; a silent one is a crash that never got marked finished.
STALE_SECS = 15 * 60


def last_record(path):
    """The last JSON line of a transcript, read from the end because transcripts run to megabytes."""
    with open(path, "rb") as f:
        size = f.seek(0, os.SEEK_END)
        chunk = b""
        while size > 0:
            step = min(1 << 16, size)
            size -= step
            f.seek(size)
            chunk = f.read(step) + chunk
            lines = chunk.rstrip(b"\n").split(b"\n")
            if len(lines) > 1 or size == 0:
                return json.loads(lines[-1])
    return {}


def finished_turn(transcript):
    """A named sub agent never gets the finished flag: it ends its turn and waits for a message."""
    last = last_record(transcript)
    return last.get("type") == "assistant" and last.get("message", {}).get("stop_reason") == "end_turn"


def running(session_id):
    """Running native sub agents of a Claude session as (name, description), oldest first."""
    found = []
    for meta_path in glob.glob(os.path.join(PROJECTS_DIR, "*", session_id, "subagents", "agent-*.meta.json")):
        transcript = meta_path[: -len(".meta.json")] + ".jsonl"
        try:
            with open(meta_path) as f:
                meta = json.load(f)
            started = os.stat(meta_path).st_mtime
            idle_secs = time.time() - os.stat(transcript).st_mtime
            ended = finished_turn(transcript)
        except (OSError, ValueError):
            continue
        if meta.get("finished") or ended or idle_secs > STALE_SECS:
            continue
        found.append((started, meta.get("name") or meta.get("agentType") or "agent", meta.get("description") or ""))
    return [(name, description) for _, name, description in sorted(found)]
