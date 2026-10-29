#!/usr/bin/env python3
"""Automation daemon: starts workflows when herdr events match their TRIGGERS.

A workflow declares triggers as a literal list at module level:

    TRIGGERS = [{"event": "pane.agent_status_changed", "where": {"agent_status": "done"}, "cooldown": 30}]

`where` keys are dotted paths into the event data and must equal the given values.
Supported events: the subscribable herdr events plus pane.agent_status_changed (derived from pane.updated;
data has pane_id, agent, agent_status, previous_status). Schedules use cron fields in local time and only apply to global workflows:

    TRIGGERS = [{"schedule": "30 9 * * 1-5", "catch_up_until": "13:00"}]

With catch_up_until, a slot missed because the Mac slept or herdr was closed still runs after wake-up
until that time of day. A workflow never starts while a previous run of it is still running, and `cooldown`
(seconds, default 5) spaces out starts so a workflow cannot retrigger itself in a tight loop.

Usage: triggerd.py            run the daemon (exits if one is already running)
       triggerd.py --ensure   start the daemon detached if it is not running
       triggerd.py --restart  stop the running daemon and start a fresh one
"""

import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime

import catalog
import hwf
import runs
import schedule
import wf

# pane.updated is consumed only to derive pane.agent_status_changed: herdr cannot subscribe to that
# event globally, and raw pane.updated is too frequent to offer as a trigger.
SUBSCRIBABLE = [
    "pane.updated",
    "workspace.created", "workspace.closed", "workspace.focused",
    "worktree.created", "worktree.opened", "worktree.removed",
    "tab.created", "tab.closed", "tab.focused",
    "pane.created", "pane.closed", "pane.exited", "pane.focused", "pane.agent_detected",
]
DEFAULT_COOLDOWN_SECS = 5
SCHEDULE_TICK_SECS = 30


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, file=sys.stderr, flush=True)


def state_dir():
    path = os.path.join(os.path.dirname(runs.state_dir()), "triggerd")
    os.makedirs(path, exist_ok=True)
    return path


def lookup(data, dotted):
    for key in dotted.split("."):
        if not isinstance(data, dict) or key not in data:
            return None
        data = data[key]
    return data


def matches(trigger, event):
    """Events arrive as {"event": "tab_created", ...} while triggers use the dotted subscription name."""
    if trigger.get("event", "").replace(".", "_") != event["event"]:
        return False
    return all(lookup(event["data"], path) == value for path, value in trigger.get("where", {}).items())


def event_context(data):
    pane = data.get("pane") or {}
    return {
        "workspace_id": data.get("workspace_id") or pane.get("workspace_id") or "",
        "tab_id": data.get("tab_id") or pane.get("tab_id") or "",
        "pane_id": data.get("pane_id") or pane.get("pane_id") or "",
    }


def event_cwd(data):
    pane = data.get("pane") or {}
    if pane.get("cwd"):
        return pane["cwd"]
    if data.get("pane_id"):
        try:
            return wf.request("pane.get", {"pane_id": data["pane_id"]})["pane"]["cwd"]
        except (OSError, wf.WorkflowError):
            pass  # the pane is gone (pane.closed); only global workflows apply
    return os.path.expanduser("~")


class StatusTracker:
    """Turns pane.updated snapshots into pane_agent_status_changed events."""

    def __init__(self):
        self.status = {}

    def feed(self, event):
        """Events to dispatch for one stream event. A pane's first snapshot only records its status."""
        data = event["data"]
        if event["event"] == "pane_closed":
            self.status.pop(data.get("pane_id"), None)
            return [event]
        if event["event"] != "pane_updated":
            return [event]
        pane = data["pane"]
        previous = self.status.get(pane["pane_id"])
        self.status[pane["pane_id"]] = pane["agent_status"]
        if previous is None or previous == pane["agent_status"]:
            return []
        changed = {
            "pane_id": pane["pane_id"], "tab_id": pane["tab_id"], "workspace_id": pane["workspace_id"],
            "agent": pane.get("agent"), "agent_status": pane["agent_status"], "previous_status": previous, "pane": pane,
        }
        return [{"event": "pane_agent_status_changed", "data": changed}]


class Dispatcher:
    def __init__(self, start=None, clock=time.monotonic, running=None):
        self.start = start or hwf.spawn_detached
        self.clock = clock
        self.running = running or (lambda name: {r["workflow"] for r in runs.list_runs(50) if r["status"] == "running"})
        self.last_start = {}

    def handle(self, event):
        data = event["data"]
        cwd = event_cwd(data)
        ctx = {**event_context(data), "cwd": cwd}
        running = None
        for workflow in catalog.discover(cwd):
            for trigger in workflow.triggers:
                if not matches(trigger, event):
                    continue
                running = self.running(None) if running is None else running
                cooldown = trigger.get("cooldown", DEFAULT_COOLDOWN_SECS)
                if workflow.name in running or self.clock() - self.last_start.get(workflow.name, -cooldown) < cooldown:
                    log("skip %s: still running or cooling down" % workflow.name)
                    break
                self.last_start[workflow.name] = self.clock()
                log("start %s on %s" % (workflow.name, event["event"]))
                self.start(workflow.name, ctx, {}, event)
                break


class Scheduler:
    """Starts global workflows whose TRIGGERS have a `schedule`. Last fired slots survive restarts."""

    def __init__(self, start=None, clock=datetime.now, running=None, state_path=None):
        self.start = start or hwf.spawn_detached
        self.clock = clock
        self.running = running or (lambda: {r["workflow"] for r in runs.list_runs(50) if r["status"] == "running"})
        self.state_path = state_path or os.path.join(state_dir(), "schedule.json")

    def load(self):
        try:
            with open(self.state_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save(self, fired):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(fired, f)
        os.replace(tmp, self.state_path)

    def tick(self):
        now = self.clock()
        fired = self.load()
        running = None
        for workflow in catalog.discover_global():
            for index, trigger in enumerate(workflow.triggers):
                if "schedule" not in trigger:
                    continue
                key = "%s#%d" % (workflow.name, index)
                last = datetime.fromisoformat(fired[key]) if key in fired else None
                slot = schedule.due_slot(trigger, now, last)
                if slot is None:
                    continue
                running = self.running() if running is None else running
                if workflow.name in running:
                    log("defer %s: previous run still going" % workflow.name)
                    continue
                fired[key] = slot.isoformat()
                self.save(fired)
                log("start %s for slot %s" % (workflow.name, slot.isoformat()))
                ctx = {"workspace_id": "", "tab_id": "", "pane_id": "", "cwd": os.path.expanduser("~")}
                self.start(workflow.name, ctx, {}, {"event": "schedule", "data": {"slot": slot.isoformat(), "schedule": trigger["schedule"]}})


class LabelRefresher:
    """Keeps the "10min ago" in the sidebar label of agents that recent runs started."""

    def __init__(self, report=None, clock=time.time):
        self.report = report or wf.report_label
        self.clock = clock
        self.sent = {}
        self.gone = set()

    def run_panes(self, run_id):
        try:
            with open(os.path.join(runs.run_dir(run_id), wf.PANES_FILE)) as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def tick(self):
        now = self.clock()
        for record in runs.list_runs(50):
            if now - record["started_at"] > wf.LABEL_TTL_MS / 1000:
                continue
            label = wf.workflow_label(record["workflow"], record["started_at"], now)
            for pane_id in self.run_panes(record["id"]):
                if pane_id in self.gone or self.sent.get(pane_id) == label:
                    continue
                try:
                    self.report(pane_id, label)
                except wf.WorkflowError as e:
                    if "pane_not_found" not in str(e):
                        log("could not refresh label of %s: %s" % (pane_id, e))
                        continue
                    self.gone.add(pane_id)
                    continue
                self.sent[pane_id] = label


def schedule_loop(scheduler, refresher):
    while True:
        for job in (scheduler.tick, refresher.tick):
            try:
                job()
            except (OSError, ValueError, KeyError) as e:
                log("%s failed: %s" % (job.__qualname__, e))
        time.sleep(SCHEDULE_TICK_SECS)


def acquire_lock():
    f = open(os.path.join(state_dir(), "daemon.lock"), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def spawn_detached():
    log_file = open(os.path.join(state_dir(), "daemon.log"), "a")
    subprocess.Popen([sys.executable, os.path.abspath(__file__)], stdin=subprocess.DEVNULL, stdout=log_file, stderr=log_file, start_new_session=True)


def stop_running():
    try:
        with open(os.path.join(state_dir(), "daemon.pid")) as f:
            os.kill(int(f.read()), signal.SIGTERM)
    except (OSError, ValueError):
        return
    for _ in range(50):
        lock = acquire_lock()
        if lock:
            lock.close()
            return
        time.sleep(0.1)


def events(path):
    """Yield events from one subscription connection until it closes."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(path)
        subscriptions = [{"type": name} for name in SUBSCRIBABLE]
        s.sendall((json.dumps({"id": "triggerd", "method": "events.subscribe", "params": {"subscriptions": subscriptions}}) + "\n").encode())
        with s.makefile("r") as lines:
            for line in lines:
                message = json.loads(line)
                if "event" in message:
                    yield message
    raise OSError("event stream closed")


def serve():
    path = os.environ.get("HERDR_SOCKET_PATH") or os.path.expanduser("~/.config/herdr/herdr.sock")
    threading.Thread(target=schedule_loop, args=(Scheduler(), LabelRefresher()), daemon=True).start()
    dispatcher = Dispatcher()
    tracker = StatusTracker()
    backoff = 1.0
    while True:
        try:
            for event in events(path):
                backoff = 1.0
                for derived in tracker.feed(event):
                    try:
                        dispatcher.handle(derived)
                    except (OSError, wf.WorkflowError, ValueError, KeyError) as e:
                        log("dispatch failed for %s: %s" % (derived["event"], e))
        except OSError as e:
            log("event stream down (%s); retrying in %.0fs" % (e, backoff))
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if mode == "--restart":
        stop_running()
        spawn_detached()
        return
    if mode == "--ensure":
        lock = acquire_lock()
        if lock:
            lock.close()
            spawn_detached()
        return
    lock = acquire_lock()
    if not lock:
        return
    with open(os.path.join(state_dir(), "daemon.pid"), "w") as f:
        f.write(str(os.getpid()))
    log("triggerd started (pid %d)" % os.getpid())
    serve()


if __name__ == "__main__":
    main()
