"""GitHub PR lookup for agent sessions: the PR of the working directory's branch plus PRs the session created."""

import glob
import json
import os
import re
import subprocess

PR_URL_PATTERN = re.compile(r"https://github\.com/([\w.-]+)/[\w.-]+/pull/\d+")
# Commands that make a PR part of the session's work. `gh pr view` is left out: it also reads other people's PRs.
PR_WRITE_COMMAND = re.compile(r"\bgh pr (create|edit|ready|merge|comment|close|reopen)\b")
REMOTE_PATTERN = re.compile(r"github\.com[:/]([\w.-]+)/")
PR_FIELDS = "number,title,url,state,isDraft,statusCheckRollup,reviewDecision"
GH_TIMEOUT_SECS = 30
FAILED = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"}
# Display order in the sidebar row; one token per state so each gets its own color.
PR_TOKENS = {"passing": "pr_pass", "pending": "pr_pending", "failing": "pr_fail", "MERGED": "pr_merged", None: "pr_open", "CLOSED": "pr_closed"}
APPROVED_SYMBOL = "✓"

_tokens = {}


def run(argv, cwd=None, env=None):
    return subprocess.run(argv, capture_output=True, text=True, timeout=GH_TIMEOUT_SECS, cwd=cwd, env=env)


def token_for(suffix):
    if suffix not in _tokens:
        logins = run(["gh", "auth", "status", "--json", "hosts", "--jq", '.hosts["github.com"][].login']).stdout.split()
        login = next((l for l in logins if l.endswith(suffix)), None)
        _tokens[suffix] = run(["gh", "auth", "token", "--user", login]).stdout.strip() if login else None
    return _tokens[suffix]


def gh_pr_view(args, owner, cwd=None):
    """PR dict from `gh pr view`, None when there is no PR; raises RuntimeError on other failures.

    Tries the gh account that owns the org's repos first, then the other one: repos can be
    transferred, so the remote's owner does not always match the account with access.
    The active gh account is never switched.
    """
    suffixes = ["-rvvup-zopa", "_zopabank"] if owner in ("rvvup", "rvvup-internal") else ["_zopabank", "-rvvup-zopa"]
    error = "no gh account for %s" % owner
    for suffix in suffixes:
        token = token_for(suffix)
        if not token:
            continue
        result = run(["gh", "pr", "view", *args, "--json", PR_FIELDS], cwd=cwd, env={**os.environ, "GH_TOKEN": token})
        if result.returncode == 0:
            return json.loads(result.stdout)
        if "no pull requests found" in result.stderr:
            return None
        error = result.stderr.strip()[:200]
    raise RuntimeError(error)


def branch_pr(cwd):
    remote = run(["git", "remote", "get-url", "origin"], cwd=cwd).stdout
    match = REMOTE_PATTERN.search(remote)
    return gh_pr_view([], match.group(1), cwd=cwd) if match else None


def worked_pr_urls(session_id):
    """PR URLs printed by `gh pr` commands that create or change a PR in a Claude session."""
    if not re.fullmatch(r"[A-Za-z0-9-]+", session_id):
        return []
    paths = glob.glob(os.path.expanduser("~/.claude/projects/*/%s.jsonl" % session_id))
    if not paths:
        return []
    create_calls, urls = set(), []
    with open(paths[0]) as f:
        for line in f:
            try:
                content = (json.loads(line).get("message") or {}).get("content")
            except ValueError:
                continue
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_use" and PR_WRITE_COMMAND.search(str((block.get("input") or {}).get("command", ""))):
                    create_calls.add(block["id"])
                elif block.get("type") == "tool_result" and block.get("tool_use_id") in create_calls:
                    urls += [m.group(0) for m in PR_URL_PATTERN.finditer(str(block.get("content")))]
    return list(dict.fromkeys(urls))


def check_state(rollup):
    if any(c.get("conclusion") in FAILED or c.get("state") in FAILED for c in rollup):
        return "failing"
    if any(c.get("status", "COMPLETED") != "COMPLETED" or c.get("state") in ("PENDING", "EXPECTED") for c in rollup):
        return "pending"
    return "passing" if rollup else None


def pr_state(pr):
    return pr["state"] if pr["state"] in ("MERGED", "CLOSED") else check_state(pr["statusCheckRollup"])


def strike(text):
    return "".join(c + "\u0336" for c in text)


def session_prs(cwd, session_id):
    """Unique PR dicts for the cwd's branch and, for Claude sessions, the PRs the session created or changed."""
    prs = []
    branch = branch_pr(cwd) if cwd and os.path.isdir(cwd) else None
    if branch:
        prs.append(branch)
    for url in worked_pr_urls(session_id) if session_id else []:
        pr = gh_pr_view([url], PR_URL_PATTERN.match(url).group(1))
        if pr and all(pr["url"] != p["url"] for p in prs):
            prs.append(pr)
    return prs


def tokens(prs, icon):
    """Sidebar tokens, one per PR state so each can be colored. Merged and closed PRs are struck through,
    open PRs get a check once approved. The icon goes inside the first token so a single PR has no separator."""
    out = {name: None for name in PR_TOKENS.values()}
    for pr in prs:
        state = pr_state(pr)
        text = "#%d" % pr["number"]
        if state in ("MERGED", "CLOSED"):
            text = strike(text)
        elif pr.get("reviewDecision") == "APPROVED":
            text += APPROVED_SYMBOL
        name = PR_TOKENS[state]
        out[name] = "%s %s" % (out[name], text) if out[name] else text
    first = next((name for name in PR_TOKENS.values() if out[name]), None)
    if first:
        out[first] = "%s %s" % (icon, out[first])
    return out
