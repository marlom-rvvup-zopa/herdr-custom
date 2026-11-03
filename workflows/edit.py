"""Open a workflow for editing: in VS Code, or in a new Claude session that already knows the context."""

import os
import secrets
import shutil
import subprocess
import time

import catalog
import runs
import wf

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
VSCODE_BIN = "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"
AGENT_START_TIMEOUT_SECS = 60
TAB_LABEL = "edit "


def real_path(workflow):
    """Workflows can be symlinks (examples/ is linked into the global dir). Edit the file they point to."""
    return os.path.realpath(workflow.path)


def vscode_command(workflow):
    binary = VSCODE_BIN if os.path.exists(VSCODE_BIN) else shutil.which("code")
    target = real_path(workflow)
    if binary:
        return [binary, catalog.repo_root(os.path.dirname(target)), target]
    return ["open", "-a", "Visual Studio Code", target]


def edit_in_vscode(workflow):
    subprocess.run(vscode_command(workflow), check=True)


def last_run_note(workflow):
    latest = next((r for r in runs.list_runs(200) if r["workflow"] == workflow.name), None)
    if not latest:
        return "It has never run."
    log = os.path.join(runs.run_dir(latest["id"]), "output.log")
    return "Its last run %s finished as %s (exit code %s); the log is %s." % (latest["id"], latest["status"], latest["exit_code"], log)


def context_prompt(workflow):
    target = real_path(workflow)
    triggers = "TRIGGERS: %r." % (workflow.triggers,) if workflow.triggers else "It has no TRIGGERS, so it only runs when started by hand."
    symlink = "" if target == os.path.abspath(workflow.path) else " (%s is a symlink to it)" % workflow.path
    return "\n".join([
        "Use the herdr-workflows skill. I want to change the herdr workflow '%s'." % workflow.name,
        "File: %s%s. Scope: %s. %s" % (target, symlink, workflow.scope, triggers),
        last_run_note(workflow),
        "Plugin docs for the SDK and trigger rules: %s." % os.path.join(PLUGIN_DIR, "README.md"),
        "Read the workflow and the docs first. Do not edit or run anything yet: summarise what the workflow does in a few lines, then ask me what to change.",
        "After editing, validate with `python3 %s list`. If TRIGGERS changed, restart the trigger daemon." % os.path.join(PLUGIN_DIR, "hwf.py"),
    ])


def edit_with_claude(workflow, workspace_id):
    """New focused tab with a Claude session that has been given the context. Does not wait for it."""
    target = real_path(workflow)
    pane = wf.open_pane("tab", cwd=catalog.repo_root(os.path.dirname(target)), label=TAB_LABEL + workflow.name, focus=True, workspace_id=workspace_id)
    name = "edit-%s-%s" % (workflow.name[:20], secrets.token_hex(2))
    wf.request("agent.start", {"name": name, "kind": "claude", "pane_id": pane["pane_id"]}, timeout=AGENT_START_TIMEOUT_SECS + 10)
    # agent.start can return while the agent is still booting; prompts are rejected until it is idle.
    wf.request("agent.wait", {"target": name, "until": ["idle"], "timeout_ms": AGENT_START_TIMEOUT_SECS * 1000}, timeout=AGENT_START_TIMEOUT_SECS + 10)
    wf.request("agent.prompt", {"target": name, "text": context_prompt(workflow)})
    return name
