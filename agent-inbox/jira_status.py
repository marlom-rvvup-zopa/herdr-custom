"""Jira lookups through the acli CLI: ticket status and the projects we can see."""

import json
import os
import re
import subprocess
import time

ACLI_TIMEOUT_SECS = 30
# A login the user ignored is offered again after this long.
LOGIN_RETRY_SECS = 600
JIRA_SITE = "zopapos.atlassian.net"
NOT_FOUND = "Issue does not exist or you do not have permission"

_login_started = 0.0


class LoginRequired(RuntimeError):
    """acli is not logged in, so nothing can be looked up."""


class TicketNotFound(RuntimeError):
    """Jira has no such ticket, or we cannot see it: not one of ours."""


def logged_in():
    """True when acli is logged in to our site. A login to another site finds none of our tickets and projects."""
    result = subprocess.run(["acli", "jira", "auth", "status"], capture_output=True, text=True, timeout=ACLI_TIMEOUT_SECS)
    return result.returncode == 0 and "Site: " + JIRA_SITE in result.stdout


def start_login():
    """Open the login in a popup of its own, because acli also asks which site to use in the terminal (pick JIRA_SITE).
    Not repeated within LOGIN_RETRY_SECS. True when it opens one."""
    global _login_started
    if time.time() - _login_started < LOGIN_RETRY_SECS:
        return False
    _login_started = time.time()
    argv = [os.environ.get("HERDR_BIN_PATH", "herdr"), "plugin", "pane", "open", "--plugin", "marlom.agent-inbox", "--entrypoint", "jira-login", "--focus"]
    subprocess.run(argv, check=False, capture_output=True)
    return True


def acli_json(args):
    result = subprocess.run(["acli", "jira", *args, "--json"], capture_output=True, text=True, timeout=ACLI_TIMEOUT_SECS)
    if result.returncode == 0:
        return json.loads(result.stdout)
    message = (result.stderr.strip() or result.stdout.strip())[:200]
    if not logged_in():
        raise LoginRequired("acli is not logged in to %s" % JIRA_SITE)
    if NOT_FOUND in message:
        raise TicketNotFound(message)
    raise RuntimeError(message)


def is_done(key):
    """True when the ticket's status category is Done. Raises TicketNotFound for a ticket that is not in our Jira,
    LoginRequired when acli is logged out and RuntimeError for any other failure."""
    if not re.fullmatch(r"[A-Z][A-Z0-9]{1,9}-\d+", key):
        raise RuntimeError("not a ticket key: %s" % key)
    return acli_json(["workitem", "view", key, "--fields", "status"])["fields"]["status"]["statusCategory"]["key"] == "done"


def project_keys():
    """Keys of every project we can see, such as PAY."""
    keys = sorted(p["key"] for p in acli_json(["project", "list", "--paginate"]))
    if not logged_in():
        raise LoginRequired("acli is not logged in to %s" % JIRA_SITE)
    return keys
