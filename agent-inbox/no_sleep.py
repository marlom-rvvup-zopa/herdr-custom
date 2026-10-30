#!/usr/bin/env python3
"""Keep the Mac awake with caffeinate and show "no sleep" in the terminal window title.

Usage: no_sleep.py toggle   start or stop
       no_sleep.py hold     the long-running holder, started detached by toggle
"""

import fcntl
import os
import signal
import subprocess
import sys
import time

from lib import herdr_request, notify, state_dir

TITLE = "\u2615"
# -d keeps the display on, and with it the screen saver and the lock that follow it; -i keeps the system awake while idle,
# -m the disk spinning, -s the system awake on AC power, which also covers clamshell mode.
# A forced sleep, or unplugging the displays in clamshell mode, still sleeps the Mac.
CAFFEINATE_FLAGS = ["-d", "-i", "-m", "-s"]
# A restarted herdr, or a client that attaches while it is on, gets the title within this long.
REPORT_SECS = 5


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, file=sys.stderr, flush=True)


def lock_path():
    return os.path.join(state_dir(), "no-sleep.lock")


def pid_path():
    return os.path.join(state_dir(), "no-sleep.pid")


def acquire_lock():
    f = open(lock_path(), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def show_title():
    herdr_request("client.window_title.set", {"title": TITLE})


def clear_title():
    herdr_request("client.window_title.clear", {})


def hold():
    lock = acquire_lock()
    if not lock:
        return
    with open(pid_path(), "w") as f:
        f.write(str(os.getpid()))
    # -w ends caffeinate with the holder, even if the holder is killed.
    caffeinate = subprocess.Popen(["caffeinate", *CAFFEINATE_FLAGS, "-w", str(os.getpid())])
    signal.signal(signal.SIGTERM, lambda *_: caffeinate.terminate())
    while caffeinate.poll() is None:
        try:
            show_title()
        except (OSError, RuntimeError) as e:
            log("setting the title failed: %s" % e)
        try:
            caffeinate.wait(REPORT_SECS)
        except subprocess.TimeoutExpired:
            pass
    try:
        clear_title()
    except (OSError, RuntimeError) as e:
        log("clearing the title failed: %s" % e)


def toggle():
    lock = acquire_lock()
    if lock:
        lock.close()
        log_file = open(os.path.join(state_dir(), "no-sleep.log"), "a")
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "hold"],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )
        notify("No sleep", "On. The Mac stays awake until you toggle it off.")
        return 0
    with open(pid_path()) as f:
        os.kill(int(f.read()), signal.SIGTERM)
    notify("No sleep", "Off. The Mac may sleep again.")
    return 0


MODES = {"toggle": toggle, "hold": hold}

if __name__ == "__main__":
    mode = MODES.get(sys.argv[1] if len(sys.argv) > 1 else "")
    if not mode:
        sys.exit("usage: no_sleep.py toggle|hold")
    sys.exit(mode())
