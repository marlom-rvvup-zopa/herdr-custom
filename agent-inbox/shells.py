"""Claude Code runs each shell command, foreground or background, as a child process of the claude process:
/bin/zsh -c source ~/.claude/shell-snapshots/<snapshot>.sh ... && eval '<command>'. A claude process with such a child has a shell running."""

import subprocess

SNAPSHOT_DIR = "/.claude/shell-snapshots/"


def parents_with_shells(ps_output):
    """Pids of the processes that have a Claude shell child, from `ps -ax -o pid=,ppid=,command=` output."""
    parents = set()
    for line in ps_output.splitlines():
        fields = line.split(None, 2)
        if len(fields) == 3 and SNAPSHOT_DIR in fields[2]:
            parents.add(int(fields[1]))
    return parents


def running_parents():
    return parents_with_shells(process_table())


def process_table():
    return subprocess.run(["ps", "-ax", "-o", "pid=,ppid=,command="], check=True, capture_output=True, text=True).stdout


def listening_pids():
    """Pids holding a listening TCP socket. lsof exits 1 when there is none, so its exit code is not checked."""
    out = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fp"], capture_output=True, text=True).stdout
    return {int(line[1:]) for line in out.splitlines() if line.startswith("p")}


def parents_with_waiting_shells(ps_output, listening):
    """Pids of the processes with a Claude shell child whose process tree listens on no port. A shell that serves
    something, like a dev server, is not one the agent waits on; one that listens on nothing is a script that will end."""
    children = {}
    shells_by_parent = {}
    for line in ps_output.splitlines():
        fields = line.split(None, 2)
        if len(fields) < 3:
            continue
        pid, ppid = int(fields[0]), int(fields[1])
        children.setdefault(ppid, []).append(pid)
        if SNAPSHOT_DIR in fields[2]:
            shells_by_parent.setdefault(ppid, []).append(pid)

    def serves(pid):
        return pid in listening or any(serves(child) for child in children.get(pid, []))

    return {parent for parent, found in shells_by_parent.items() if not all(serves(shell) for shell in found)}


def waiting_parents():
    return parents_with_waiting_shells(process_table(), listening_pids())
