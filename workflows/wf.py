"""SDK for workflow scripts: a thin layer over the herdr socket API.

A workflow is a plain Python script. `import wf` works because the runner puts this
directory on PYTHONPATH. The runner also exports the invocation context as HWF_* variables.
"""

import json
import os
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass

DEFAULT_AGENT_TIMEOUT_SECS = 30 * 60
STARTUP_TIMEOUT_SECS = 60
TAB_LABEL_CHARS = 28
SIDEBAR_SOURCE = "marlom.workflows"
# Prefix of the $workflow sidebar token. Swap the code point to change the icon.
WORKFLOW_ICON = chr(0xF073)
LABEL_TTL_MS = 24 * 60 * 60 * 1000
# Panes a run tagged, so the trigger daemon can keep the "ago" in their label current.
PANES_FILE = "panes.json"
RESPONSE_LINES = 200
VERDICT_TOKEN = re.compile(r"[A-Z][A-Z0-9_]{0,31}")
PLACEMENTS = ("tab", "beside", "below")
# What an agent prints when its turn was cut short. idle looks the same as finished, so the screen is the only signal.
INTERRUPTED_MARKERS = {"claude": "Interrupted \u00b7"}
INTERRUPT_CHECK_LINES = 40
_agent_count = 0


class WorkflowError(RuntimeError):
    pass


@dataclass
class Result:
    stdout: str
    stderr: str
    code: int


@dataclass
class AgentResult:
    response: str
    pane_id: str
    name: str
    verdict: str = ""


def request(method, params=None, timeout=10.0):
    """One herdr API call. Returns the result object; raises WorkflowError on an error reply."""
    path = os.environ.get("HERDR_SOCKET_PATH") or os.path.expanduser("~/.config/herdr/herdr.sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(path)
        s.sendall((json.dumps({"id": "wf", "method": method, "params": params or {}}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(1 << 16)
            if not chunk:
                break
            buf += chunk
    if not buf:
        raise WorkflowError("%s: connection closed without a reply" % method)
    reply = json.loads(buf)
    if "error" in reply:
        raise WorkflowError("%s: %s" % (method, reply["error"]))
    return reply.get("result") or {}


def context():
    """Where the workflow started: workspace_id, tab_id, pane_id, cwd, plus run_id and run_dir."""
    env = os.environ
    return {
        "workspace_id": env.get("HWF_WORKSPACE_ID", ""),
        "tab_id": env.get("HWF_TAB_ID", ""),
        "pane_id": env.get("HWF_PANE_ID", ""),
        "cwd": env.get("HWF_CWD") or os.getcwd(),
        "run_id": env.get("HWF_RUN_ID", ""),
        "run_dir": env.get("HWF_RUN_DIR", ""),
        "workflow": env.get("HWF_WORKFLOW", ""),
    }


def input(name, default=None):
    """An --input name=value given to `hwf run`. Raises when missing and no default."""
    value = os.environ.get("HWF_INPUT_" + name)
    if value is not None:
        return value
    if default is None:
        raise WorkflowError("missing input %r: pass --input %s=<value>" % (name, name))
    return default


def event():
    """The herdr event that triggered this run, or None for a manual run."""
    raw = os.environ.get("HWF_EVENT_JSON")
    return json.loads(raw) if raw else None


def sh(*argv, check=True, cwd=None, env=None, stdin=None):
    """Run a local command and capture its output."""
    proc = subprocess.run(argv, cwd=cwd or context()["cwd"], env={**os.environ, **(env or {})}, input=stdin, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise WorkflowError("%s exited %d: %s" % (" ".join(argv), proc.returncode, proc.stderr.strip()))
    return Result(proc.stdout, proc.stderr, proc.returncode)


def notify(title, body=None, sound="none"):
    """herdr toast. sound is none, done or request."""
    request("notification.show", {"title": title, "body": body, "sound": sound})


def find_workspace(label_part):
    """Id of the first open workspace whose label contains label_part. For runs that start without a pane (schedules)."""
    matches = [w for w in request("workspace.list")["workspaces"] if label_part in w["label"]]
    if not matches:
        raise WorkflowError("no workspace labelled like %r: open it in herdr first" % label_part)
    return matches[0]["workspace_id"]


def open_pane(where="tab", cwd=None, label=None, focus=False, workspace_id=None):
    """New pane for a command or agent. Returns {"pane_id", "tab_id"}; tab_id is set only for where="tab".

    workspace_id defaults to the workspace the run started in.
    """
    ctx = context()
    cwd = cwd or ctx["cwd"]
    if where == "tab":
        reply = request("tab.create", {"workspace_id": workspace_id or ctx["workspace_id"], "cwd": cwd, "label": (label or "workflow")[:TAB_LABEL_CHARS], "focus": focus})
        return {"pane_id": reply["root_pane"]["pane_id"], "tab_id": reply["tab"]["tab_id"]}
    if where in ("beside", "below"):
        direction = "right" if where == "beside" else "down"
        reply = request("pane.split", {"direction": direction, "target_pane_id": ctx["pane_id"], "cwd": cwd, "focus": focus})
        return {"pane_id": reply["pane"]["pane_id"], "tab_id": None}
    raise WorkflowError("where must be one of %s, got %r" % (", ".join(PLACEMENTS), where))


def close(pane):
    if pane["tab_id"]:
        request("tab.close", {"tab_id": pane["tab_id"]})
    else:
        request("pane.close", {"pane_id": pane["pane_id"]})


def terminal(command, where="beside", cwd=None, label=None, focus=False, workspace_id=None):
    """Run a shell command in a new pane and leave it open. Returns the pane id."""
    pane = open_pane(where, cwd, label or command, focus, workspace_id)
    request("pane.send_input", {"pane_id": pane["pane_id"], "text": command, "keys": ["enter"]})
    return pane["pane_id"]


def parse_verdict(response, tokens):
    """The final non-empty line of the response, which must be one of tokens."""
    lines = [line.strip() for line in response.splitlines() if line.strip()]
    last = lines[-1] if lines else ""
    if last not in tokens:
        raise WorkflowError("agent verdict %r is not one of %s" % (last, ", ".join(tokens)))
    return last


def build_prompt(prompt, result_file, verdict=None):
    parts = [prompt, "When you are completely done, write your full final answer as Markdown to %s and then stop." % result_file]
    if verdict:
        parts.append("End the answer with exactly one of these tokens alone on the final line: %s." % ", ".join(verdict))
    return "\n\n".join(parts)


def agent(kind, prompt, *, where="tab", cwd=None, label=None, verdict=None, close_on_success=False, target=None, timeout=DEFAULT_AGENT_TIMEOUT_SECS, focus=False, workspace_id=None, raw=False):
    """Run one prompt in a coding agent and return its answer.

    Starts a new `kind` agent in a new pane, or prompts `target` (an idle agent's name or pane id).
    verdict: tokens; the answer must end with one of them. A blocked agent keeps the call waiting
    until you answer it in its pane or the timeout passes.
    raw: send the prompt unchanged, for slash commands. No answer file is requested, so the
    response is read from the screen, and verdict cannot be combined with it.
    """
    global _agent_count
    if raw and verdict:
        raise WorkflowError("verdict needs the answer instructions that raw=True leaves out")
    for token in verdict or []:
        if not VERDICT_TOKEN.fullmatch(token):
            raise WorkflowError("verdict token %r must match %s" % (token, VERDICT_TOKEN.pattern))
    _agent_count += 1
    ctx = context()
    result_file = os.path.join(ctx["run_dir"] or "/tmp", "agent-%d.md" % _agent_count)
    pane = None
    name = target
    if not target:
        name = "wf-%s-%d" % (ctx["run_id"].rsplit("-", 1)[-1] or "x", _agent_count)
        pane = open_pane(where, cwd, label or kind, focus, workspace_id)
        request("agent.start", {"name": name, "kind": kind, "pane_id": pane["pane_id"]}, timeout=STARTUP_TIMEOUT_SECS + 10)
        mark_as_workflow(pane["pane_id"], ctx["workflow"])
        # agent.start can return while the agent is still booting; prompts are rejected until it is idle.
        request("agent.wait", {"target": name, "until": ["idle"], "timeout_ms": STARTUP_TIMEOUT_SECS * 1000}, timeout=STARTUP_TIMEOUT_SECS + 10)
    request(
        "agent.prompt",
        {"target": name, "text": prompt if raw else build_prompt(prompt, result_file, verdict), "wait": {"until": ["idle", "done"], "timeout_ms": timeout * 1000}},
        timeout=timeout + 30,
    )
    check_not_interrupted(kind, name)
    response = read_response(name, result_file)
    result = AgentResult(response, pane["pane_id"] if pane else target, name, parse_verdict(response, verdict) if verdict else "")
    if close_on_success and pane:
        close(pane)
    return result


def age_text(seconds):
    if seconds < 60:
        return "just now"
    for unit, size in (("d", 86400), ("h", 3600)):
        if seconds >= size:
            return "%d%s ago" % (seconds // size, unit)
    return "%dmin ago" % (seconds // 60)


def workflow_label(workflow, started_at, now):
    """Sidebar text for an agent a workflow started, e.g. "<icon> morning (10min ago)"."""
    return "%s %s (%s)" % (WORKFLOW_ICON, workflow, age_text(int(now - started_at)))


def report_label(pane_id, label):
    request("pane.report_metadata", {"pane_id": pane_id, "source": SIDEBAR_SOURCE, "tokens": {"workflow": label}, "ttl_ms": LABEL_TTL_MS})


def mark_as_workflow(pane_id, workflow):
    """Sidebar token `$workflow` on the agent's pane; triggerd refreshes its age. Cosmetic: a failure is logged, not fatal."""
    if not workflow:
        return
    now = time.time()
    started_at = float(os.environ.get("HWF_RUN_STARTED") or now)
    try:
        report_label(pane_id, workflow_label(workflow, started_at, now))
    except (OSError, WorkflowError) as e:
        log("could not label %s as workflow %s: %s" % (pane_id, workflow, e))
        return
    record_pane(pane_id)


def record_pane(pane_id):
    run_dir = context()["run_dir"]
    if not run_dir:
        return
    path = os.path.join(run_dir, PANES_FILE)
    try:
        with open(path) as f:
            panes = json.load(f)
    except (OSError, ValueError):
        panes = []
    with open(path, "w") as f:
        json.dump(panes + [pane_id], f)


def check_not_interrupted(kind, name):
    marker = INTERRUPTED_MARKERS.get(kind)
    if not marker:
        return
    screen = request("agent.read", {"target": name, "source": "recent_unwrapped", "lines": INTERRUPT_CHECK_LINES})["read"]["text"]
    if marker in screen:
        raise WorkflowError("agent %s was interrupted before it finished; its pane is still open" % name)


def read_response(name, result_file):
    if os.path.exists(result_file):
        with open(result_file) as f:
            return f.read()
    # The agent did not write the file: fall back to what is on its screen.
    return request("agent.read", {"target": name, "source": "recent_unwrapped", "lines": RESPONSE_LINES})["read"]["text"]


def log(message):
    print(message, file=sys.stderr, flush=True)
