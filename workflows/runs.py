"""Run history on disk: one directory per run holding run.json and output.log."""

import json
import os
import secrets
import time

RETENTION = 200


def state_dir():
    socket_path = os.environ.get("HERDR_SOCKET_PATH") or os.path.expanduser("~/.config/herdr/herdr.sock")
    path = os.path.join(os.path.dirname(socket_path), "workflows-state", "runs")
    os.makedirs(path, exist_ok=True)
    return path


def run_dir(run_id):
    return os.path.join(state_dir(), run_id)


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def save(record):
    path = os.path.join(run_dir(record["id"]), "run.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(record, f, indent=2)
    os.replace(tmp, path)


def start(workflow, cwd, inputs, trigger=None):
    run_id = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
    os.makedirs(run_dir(run_id))
    record = {
        "id": run_id,
        "workflow": workflow.name,
        "path": workflow.path,
        "cwd": cwd,
        "inputs": inputs,
        "trigger": trigger,
        "status": "running",
        "pid": os.getpid(),
        "started_at": time.time(),
        "ended_at": None,
        "exit_code": None,
    }
    save(record)
    prune()
    return record


def finish(record, exit_code):
    record["status"] = "succeeded" if exit_code == 0 else "failed"
    record["exit_code"] = exit_code
    record["ended_at"] = time.time()
    save(record)


def load(run_id):
    with open(os.path.join(run_dir(run_id), "run.json")) as f:
        record = json.load(f)
    # A runner that died without finishing leaves "running" behind.
    if record["status"] == "running" and not pid_alive(record["pid"]):
        record["status"] = "interrupted"
    return record


def list_runs(limit=50):
    ids = sorted((d for d in os.listdir(state_dir()) if os.path.isfile(os.path.join(run_dir(d), "run.json"))), reverse=True)
    return [load(run_id) for run_id in ids[:limit]]


def prune():
    ids = sorted(d for d in os.listdir(state_dir()) if os.path.isdir(run_dir(d)))
    for run_id in ids[:-RETENTION]:
        for name in os.listdir(run_dir(run_id)):
            os.remove(os.path.join(run_dir(run_id), name))
        os.rmdir(run_dir(run_id))
