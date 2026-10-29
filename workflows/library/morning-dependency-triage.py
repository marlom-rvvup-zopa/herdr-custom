"""Weekday 09:30: triage frontend-monorepo Renovate PRs, then work through fixable Snyk findings."""

import wf

TITLE = "Morning dependency triage"
TRIGGERS = [{"schedule": "30 9 * * 1-5", "catch_up_until": "13:00"}]

WORKSPACE_LABEL = "pos-launchpad"
POS_LAUNCHPAD = "/Users/marlom.girardi/Projects/rvvup/pos-launchpad"
REPO = "rvvup-internal/frontend-monorepo"
TRIAGE_TIMEOUT_SECS = 2 * 60 * 60
SNYK_TIMEOUT_SECS = 3 * 60 * 60

SNYK_PROMPT = """\
If the Renovate triage above was interrupted, is still waiting on one of my answers, or stopped before its summary, stop here
and say so. Do not continue.

Otherwise the triage is done. Now check Snyk for vulnerabilities in %(repo)s that we can fix.

1. Use the snyk-api skill (REST API, token from 1Password) to list open, fixable issues for the Snyk projects of %(repo)s,
   most severe first. Keep the package, current version, fixed-in version, severity, and Snyk project.
2. For each vulnerable package, first check whether an open Renovate PR already updates it (use this session's triage results).
3. Minor and patch updates, whether through a Renovate PR or a direct bump: work them through.
   - Prepare the change in a git worktree of workspaces/frontend-monorepo. Never edit the main checkout.
   - Get CI-equivalent checks passing (install, typecheck, lint, unit tests) in a way that meets the repo standards:
     follow the frontend-monorepo `update-dependency` skill and its CLAUDE.md. Fix the cause; never skip, weaken, or delete a test.
   - Make sure no feature or flow breaks. Run the repo's e2e workflow when one exists. The apps are normally already running locally.
     If an app needed for e2e is not running, do not start it: report which one, so we can adjust.
4. Major updates and issues with no fix available: change nothing; list them with the reason.
5. Jira: log vulnerabilities and package updates under the parent PAY-825, one Task per package. Look for an existing ticket
   first (the triage state in reports/renovate-triage/%(name)s/processed.json and a Jira search) and reuse it.
6. Do not push, merge, or comment on PRs for this part. Leave each prepared fix as a local branch in its worktree and show the diff
   so it can be approved, as the renovate-pr-triage skill requires for CI fixes.

Finish with a short summary table: package, severity, covered by Renovate PR or not, outcome (fixed locally / needs major /
no fix / blocked), ticket, and e2e status (passed / failed / not run, with the reason).
""" % {"repo": REPO, "name": REPO.split("/")[1]}

workspace_id = wf.find_workspace(WORKSPACE_LABEL)
session = wf.agent(
    "claude", "/renovate-pr-triage " + REPO, raw=True,
    workspace_id=workspace_id, cwd=POS_LAUNCHPAD, label="renovate-triage", timeout=TRIAGE_TIMEOUT_SECS,
)
snyk = wf.agent("claude", SNYK_PROMPT, target=session.name, timeout=SNYK_TIMEOUT_SECS)
wf.log(snyk.response)
wf.notify("dependency triage done", "frontend-monorepo: Renovate PRs triaged, Snyk findings reviewed", sound="done")
