"""When is a session finished enough to settle: its PRs merged and its tickets done, and nobody working in it."""

from dataclasses import dataclass

QUIET_MINUTES = 30
SETTLE_MODES = ("off", "suggest", "auto")
DEFAULT_MODE = "suggest"


@dataclass
class Verdict:
    ready: bool
    reason: str


def evaluate(rec, unseen_done, last_active, now, in_family, rows, prs, ticket_done, quiet_secs=QUIET_MINUTES * 60):
    """Ready only when all of these hold; otherwise the reason names the first one that does not.

    - a Claude session that can be resumed, outside any sub agent family
    - idle, in nobody's focus, with its last result already seen, and quiet for quiet_secs
    - at least one PR, and every PR merged (a PR closed without merging blocks)
    - at least one primary ticket, and every one of them Done in Jira
    ticket_done maps a key to True or False once Jira was asked, and has no entry before that.
    """
    session = rec.get("agent_session") or {}
    if rec["agent"] != "claude" or session.get("kind") != "id":
        return Verdict(False, "not a Claude session that can be resumed")
    if in_family:
        return Verdict(False, "part of a sub agent family")
    if rec["agent_status"] != "idle" or unseen_done:
        return Verdict(False, "agent is %s" % ("showing an unseen result" if unseen_done else rec["agent_status"]))
    if rec.get("focused"):
        return Verdict(False, "you are looking at it")
    quiet = now - last_active
    if quiet < quiet_secs:
        return Verdict(False, "active %d min ago, needs %d quiet minutes" % (quiet // 60, quiet_secs // 60))
    return work_verdict(prs, rows, ticket_done)


def work_verdict(prs, rows, ticket_done, closed_unmerged_counts=False):
    """Ready when there is at least one PR and every one is merged (or closed too, if closed_unmerged_counts),
    and at least one primary ticket and every one is done."""
    if not prs:
        return Verdict(False, "no PR known")
    finished = ("MERGED", "CLOSED") if closed_unmerged_counts else ("MERGED",)
    for pr in prs:
        if pr["state"] not in finished:
            return Verdict(False, "PR #%d is %s" % (pr["number"], "closed without merging" if pr["state"] == "CLOSED" else "open"))
    primary = [r["key"] for r in rows if r["role"] == "primary"]
    if not primary:
        return Verdict(False, "no ticket owned by the session")
    for key in primary:
        if key not in ticket_done:
            return Verdict(False, "%s status not looked up yet" % key)
        if not ticket_done[key]:
            return Verdict(False, "%s is not done in Jira" % key)
    outcome = "merged" if all(p["state"] == "MERGED" for p in prs) else "closed"
    return Verdict(True, "%s %s, %s done" % (", ".join("#%d" % p["number"] for p in prs), outcome, ", ".join(primary)))
