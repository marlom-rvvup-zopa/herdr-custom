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
    out = subprocess.run(["ps", "-ax", "-o", "pid=,ppid=,command="], check=True, capture_output=True, text=True).stdout
    return parents_with_shells(out)
