#!/usr/bin/env python3
"""Workflows CLI.

Usage: hwf.py list [--json]
       hwf.py run <name> [--input k=v]... [--detach]
       hwf.py runs [--limit N] [--json]
       hwf.py logs <run-id>
       hwf.py edit <name> --with vscode|claude
       hwf.py open-picker

Workflows are Python scripts in <repo>/.herdr/workflows/ and the plugin config dir's workflows/.
"""

import argparse
import json
import os
import subprocess
import sys
import time

import catalog
import edit
import runs
import wf

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
HERDR = os.environ.get("HERDR_BIN_PATH", "herdr")


def invocation_context():
    """Where the command was invoked: herdr's injected action context, else the pane environment."""
    injected = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
    env = os.environ
    return {
        "workspace_id": env.get("HERDR_WORKSPACE_ID") or injected.get("workspace_id", ""),
        "tab_id": env.get("HERDR_TAB_ID") or injected.get("tab_id", ""),
        "pane_id": env.get("HERDR_PANE_ID") or injected.get("focused_pane_id", ""),
        "cwd": env.get("HWF_INVOKE_CWD") or injected.get("focused_pane_cwd") or injected.get("workspace_cwd") or os.getcwd(),
    }


def parse_inputs(pairs):
    inputs = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep or not name:
            raise SystemExit("--input expects name=value, got %r" % pair)
        inputs[name] = value
    return inputs


def run_env(record, ctx, trigger):
    env = {**os.environ, "PYTHONPATH": PLUGIN_DIR + os.pathsep + os.environ.get("PYTHONPATH", "")}
    env.update({
        "HWF_WORKFLOW": record["workflow"],
        "HWF_RUN_ID": record["id"],
        "HWF_RUN_STARTED": str(record["started_at"]),
        "HWF_RUN_DIR": runs.run_dir(record["id"]),
        "HWF_WORKSPACE_ID": ctx["workspace_id"],
        "HWF_TAB_ID": ctx["tab_id"],
        "HWF_PANE_ID": ctx["pane_id"],
        "HWF_CWD": record["cwd"],
    })
    env.update({"HWF_INPUT_" + name: value for name, value in record["inputs"].items()})
    if trigger:
        env["HWF_EVENT_JSON"] = json.dumps(trigger)
    return env


def execute(workflow, ctx, inputs, trigger=None):
    """Run the script, tee its output to the run log, record the result, and notify. Returns the exit code."""
    record = runs.start(workflow, ctx["cwd"], inputs, trigger)
    log_path = os.path.join(runs.run_dir(record["id"]), "output.log")
    with open(log_path, "w") as log:
        with subprocess.Popen(
            [sys.executable, workflow.path], cwd=ctx["cwd"], env=run_env(record, ctx, trigger),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        ) as proc:
            for line in proc.stdout:
                log.write(line)
                log.flush()
                sys.stdout.write(line)
        code = proc.returncode
    runs.finish(record, code)
    announce(record)
    return code


def announce(record):
    elapsed = "%ds" % (record["ended_at"] - record["started_at"])
    if record["status"] == "succeeded":
        title, sound = "%s succeeded in %s" % (record["workflow"], elapsed), "done"
    else:
        title, sound = "%s failed after %s - %s" % (record["workflow"], elapsed, record["id"]), "none"
    try:
        wf.notify("workflows", title, sound)
    except (OSError, wf.WorkflowError) as e:
        print("could not show notification: %s" % e, file=sys.stderr)


def spawn_detached(name, ctx, inputs, trigger=None):
    """Start `hwf.py run` in its own session so it outlives the caller (picker, action, daemon)."""
    argv = ["run", name]
    for key, value in inputs.items():
        argv += ["--input", "%s=%s" % (key, value)]
    if trigger:
        argv += ["--event-json", json.dumps(trigger)]
    spawn_hwf(argv, ctx)


def spawn_hwf(args, ctx):
    argv = [sys.executable, os.path.join(PLUGIN_DIR, "hwf.py")] + args
    env = {**os.environ, "HERDR_WORKSPACE_ID": ctx["workspace_id"], "HERDR_TAB_ID": ctx["tab_id"], "HERDR_PANE_ID": ctx["pane_id"]}
    subprocess.Popen(argv, cwd=ctx["cwd"], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def cmd_list(args):
    workflows = catalog.discover(invocation_context()["cwd"])
    if args.json:
        print(json.dumps([w.__dict__ for w in workflows], indent=2))
    for w in workflows:
        if not args.json:
            print("%-24s %-6s %s" % (w.name, w.scope, w.error and "INVALID: " + w.error or w.description))
    return 0


def cmd_run(args):
    ctx = invocation_context()
    workflow = catalog.find(ctx["cwd"], args.name)
    if workflow.error:
        raise SystemExit("%s is invalid: %s" % (workflow.name, workflow.error))
    inputs = parse_inputs(args.input)
    trigger = json.loads(args.event_json) if args.event_json else None
    if args.detach:
        spawn_detached(workflow.name, ctx, inputs, trigger)
        return 0
    return execute(workflow, ctx, inputs, trigger)


def cmd_edit(args):
    ctx = invocation_context()
    workflow = catalog.find(ctx["cwd"], args.name)
    try:
        if args.editor == "vscode":
            edit.edit_in_vscode(workflow)
        else:
            edit.edit_with_claude(workflow, ctx["workspace_id"])
    except (OSError, subprocess.CalledProcessError, wf.WorkflowError) as e:
        # Started detached from the picker, so a toast is the only place the user can see this.
        wf.notify("workflows", "could not open %s in %s: %s" % (workflow.name, args.editor, e), "request")
        return 1
    return 0


def cmd_runs(args):
    records = runs.list_runs(args.limit)
    if args.json:
        print(json.dumps(records, indent=2))
        return 0
    for r in records:
        print("%-22s %-12s %-20s %s" % (r["id"], r["status"], r["workflow"], time.strftime("%H:%M:%S", time.localtime(r["started_at"]))))
    return 0


def cmd_logs(args):
    with open(os.path.join(runs.run_dir(args.run_id), "output.log")) as f:
        sys.stdout.write(f.read())
    return 0


def cmd_open_picker(args):
    ctx = invocation_context()
    # No --cwd: it would move the popup out of the plugin directory, where picker.py lives.
    argv = [HERDR, "plugin", "pane", "open", "--plugin", catalog.PLUGIN_ID, "--entrypoint", "picker", "--env", "HWF_INVOKE_CWD=" + ctx["cwd"], "--focus"]
    for key, value in (("WORKSPACE", ctx["workspace_id"]), ("TAB", ctx["tab_id"]), ("PANE", ctx["pane_id"])):
        argv += ["--env", "HERDR_%s_ID=%s" % (key, value)]
    return subprocess.run(argv, check=False).returncode


def parser():
    p = argparse.ArgumentParser(prog="hwf.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(fn=cmd_list)
    run = sub.add_parser("run")
    run.add_argument("name")
    run.add_argument("--input", action="append", default=[])
    run.add_argument("--detach", action="store_true")
    run.add_argument("--event-json")
    run.set_defaults(fn=cmd_run)
    editor = sub.add_parser("edit")
    editor.add_argument("name")
    editor.add_argument("--with", dest="editor", choices=["vscode", "claude"], required=True)
    editor.set_defaults(fn=cmd_edit)
    history = sub.add_parser("runs")
    history.add_argument("--limit", type=int, default=20)
    history.add_argument("--json", action="store_true")
    history.set_defaults(fn=cmd_runs)
    logs = sub.add_parser("logs")
    logs.add_argument("run_id")
    logs.set_defaults(fn=cmd_logs)
    sub.add_parser("open-picker").set_defaults(fn=cmd_open_picker)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return args.fn(args)
    except LookupError as e:
        raise SystemExit(str(e))


if __name__ == "__main__":
    sys.exit(main())
