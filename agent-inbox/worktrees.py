"""Linked git worktrees: find the one a session runs in, check it is safe to delete, delete it and bring it back."""

import os
import subprocess


def git(path, *args):
    return subprocess.run(["git", "-C", path, *args], capture_output=True, text=True)


def git_output(path, *args):
    result = git(path, *args)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "git %s failed" % args[0])
    return result.stdout.strip()


def linked_worktree(cwd):
    """The linked worktree containing `cwd` as {path, git_dir, branch, head}, or None for the main checkout and non-repos.
    `branch` is empty for a detached HEAD."""
    if not os.path.isdir(cwd):
        return None
    result = git(cwd, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir", "--show-toplevel")
    if result.returncode:
        return None
    git_dir, common_dir, top = (os.path.realpath(line) for line in result.stdout.split("\n")[:3])
    if git_dir == common_dir:
        return None
    return {"path": top, "git_dir": common_dir, "branch": git_output(top, "branch", "--show-current"), "head": git_output(top, "rev-parse", "HEAD")}


def inside(path, cwd):
    cwd = os.path.realpath(cwd)
    return cwd == path or cwd.startswith(path + os.sep)


def blocker(info, other_cwds):
    """Why deleting the worktree would lose work or break something, or None when it is safe."""
    if any(inside(info["path"], cwd) for cwd in other_cwds):
        return "another pane is open in it"
    if git_output(info["path"], "status", "--porcelain"):
        return "it has uncommitted changes"
    if git_output(info["path"], "log", "--oneline", "HEAD", "--not", "--remotes"):
        return "it has commits that are not pushed"
    return None


def remove(info):
    """Delete the worktree, and its branch when that is already merged into the default branch."""
    git_output(info["git_dir"], "worktree", "remove", info["path"])
    if info["branch"] and merged_into_default(info):
        git_output(info["git_dir"], "branch", "-D", info["branch"])


def merged_into_default(info):
    default = git(info["git_dir"], "symbolic-ref", "-q", "refs/remotes/origin/HEAD").stdout.strip() or "HEAD"
    return git(info["git_dir"], "merge-base", "--is-ancestor", info["head"], default).returncode == 0


def restore(info):
    """Check the worktree out again at its old path, recreating the branch at its old commit if it was deleted."""
    if not info["branch"]:
        args = ["--detach", info["path"], info["head"]]
    elif git(info["git_dir"], "rev-parse", "--verify", "-q", "refs/heads/" + info["branch"]).returncode == 0:
        args = [info["path"], info["branch"]]
    else:
        args = ["-b", info["branch"], info["path"], info["head"]]
    git_output(info["git_dir"], "worktree", "add", *args)
