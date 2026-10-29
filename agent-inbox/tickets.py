"""Which Jira tickets a Claude session is about, and why.

A key in the branch name, in the title of the session's PR, or in the first prompt means the session
owns the ticket ("primary"). A key the agent changed through the Jira tools is "touched". A key that only
shows up in later prompts is "mentioned": a link someone shared, not necessarily the work.
"""

import json
import re
from dataclasses import dataclass, field

# Jira keys such as PAY-123; the lookahead skips CVE-2024-1234 style ids.
TICKET_PATTERN = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b(?!-\d)")
NOT_TICKETS = {"UTF", "SHA", "ISO", "HTTP", "TLS", "AES", "RSA", "MD"}
# Branch names are lowercase and free-form: only a leading "KEY-123" or "type/KEY-123" counts, and words
# that look like a project prefix ("release-2024") are ignored.
BRANCH_PATTERN = re.compile(r"(?:^|/)([A-Za-z]{2,6}-\d{1,6})(?=$|[-_/])")
NOT_BRANCH_PREFIXES = NOT_TICKETS | {"RELEASE", "HOTFIX", "BUGFIX", "FEATURE", "FIX", "ISSUE", "VERSION", "REV", "RC"}
COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)
# Jira tool calls that change a ticket; reads and searches only mention one.
JIRA_WRITE_TOOLS = ("edit", "transition", "addcomment", "addworklog", "createissuelink")
ROLE_ORDER = ("primary", "touched", "mentioned")


@dataclass
class Scan:
    """What one pass over a session transcript found."""

    prompts: list = field(default_factory=list)
    branch: str = ""
    touched: list = field(default_factory=list)


def find_tickets(texts):
    """Unique Jira-style keys in the given texts, in order of appearance."""
    found = []
    for text in texts:
        for key in TICKET_PATTERN.findall(text):
            if key.rsplit("-", 1)[0] not in NOT_TICKETS and key not in found:
                found.append(key)
    return found


def branch_tickets(branch):
    keys = [m.upper() for m in BRANCH_PATTERN.findall(branch or "")]
    return [k for k in keys if k.rsplit("-", 1)[0] not in NOT_BRANCH_PREFIXES]


def prompt_text(content):
    """Text of a typed prompt. Content is a list of blocks when the prompt has pasted images."""
    if isinstance(content, list):
        content = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return content if isinstance(content, str) else None


def typed_text(entry):
    """What the user typed in a transcript line, or None. A slash command counts as its arguments."""
    if entry.get("type") != "user" or entry.get("isMeta"):
        return None
    text = prompt_text((entry.get("message") or {}).get("content"))
    if not text:
        return None
    if text.startswith("<"):
        args = COMMAND_ARGS.search(text)
        return args.group(1).strip() or None if args else None
    return " ".join(text.split())


def jira_writes(entry):
    """Ticket keys the agent changed through Jira tools in one transcript line."""
    content = (entry.get("message") or {}).get("content")
    if entry.get("type") != "assistant" or not isinstance(content, list):
        return []
    keys = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = (block.get("name") or "").lower()
        if "jira" in name and any(hint in name for hint in JIRA_WRITE_TOOLS):
            keys += find_tickets([json.dumps(block.get("input") or {})])
    return keys


def scan(path):
    result = Scan()
    with open(path) as f:
        for line in f:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            result.branch = entry.get("gitBranch") or result.branch
            text = typed_text(entry)
            if text:
                result.prompts.append(text)
            result.touched += jira_writes(entry)
    return result


def classify(found, pr_titles):
    """[{"key", "role", "sources"}] ordered primary, touched, mentioned."""
    sources = {}

    def add(keys, source):
        for key in keys:
            sources.setdefault(key, [])
            if source not in sources[key]:
                sources[key].append(source)

    add(branch_tickets(found.branch), "branch")
    add(find_tickets([t for t in pr_titles if t]), "pr")
    add(find_tickets(found.prompts[:1]), "first-prompt")
    add(found.touched, "jira-tool")
    add(find_tickets(found.prompts), "mention")
    owner = {"branch", "pr", "first-prompt"}
    rows = []
    for key, why in sources.items():
        role = "primary" if owner & set(why) else "touched" if "jira-tool" in why else "mentioned"
        rows.append({"key": key, "role": role, "sources": why})
    return sorted(rows, key=lambda row: ROLE_ORDER.index(row["role"]))


def display(rows):
    """(keys to show, number of mentioned keys left out). Mentions alone are shown when there is nothing better."""
    owned = [r["key"] for r in rows if r["role"] != "mentioned"]
    mentioned = [r["key"] for r in rows if r["role"] == "mentioned"]
    if owned:
        return owned, len(mentioned)
    return mentioned, 0
