"""Jira ticket status lookup through the acli CLI."""

import json
import re
import subprocess

ACLI_TIMEOUT_SECS = 30


def is_done(key):
    """True when the ticket's status category is Done; raises RuntimeError if acli fails."""
    if not re.fullmatch(r"[A-Z][A-Z0-9]{1,9}-\d+", key):
        raise RuntimeError("not a ticket key: %s" % key)
    result = subprocess.run(
        ["acli", "jira", "workitem", "view", key, "--json", "--fields", "status"],
        capture_output=True, text=True, timeout=ACLI_TIMEOUT_SECS,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[:200] or result.stdout.strip()[:200])
    return json.loads(result.stdout)["fields"]["status"]["statusCategory"]["key"] == "done"
