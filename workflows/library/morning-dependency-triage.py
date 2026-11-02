"""Weekday 09:30: triage frontend-monorepo Renovate PRs, smoke test each one, then work through fixable Snyk findings."""

import wf

TITLE = "Morning dependency triage"
TRIGGERS = [{"schedule": "30 9 * * 1-5", "catch_up_until": "13:00"}]

WORKSPACE_LABEL = "pos-launchpad"
POS_LAUNCHPAD = "/Users/marlom.girardi/Projects/rvvup/pos-launchpad"
REPO = "rvvup-internal/frontend-monorepo"
TRIAGE_TIMEOUT_SECS = 2 * 60 * 60
SMOKE_TIMEOUT_SECS = 3 * 60 * 60
SNYK_TIMEOUT_SECS = 3 * 60 * 60

SMOKE_PROMPT = """\
If the Renovate triage above was interrupted, is still waiting on one of my answers, or stopped before its summary, stop here
and say so. Do not continue.

Otherwise the triage is done. Now smoke test every open Renovate PR of %(repo)s that the triage looked at, including the ones it
called quick wins, blocked, or needing review. Do not skip a PR because its changelog or CI looked clean: this step is
mandatory, and a PR without a smoke result is not cleared.

For each PR:
1. Check out the PR branch in its own git worktree of workspaces/frontend-monorepo. Never edit the main checkout, and never push.
2. Install, then run the checks CI runs: typecheck, lint, unit tests, and a production build of every app or package the changed
   dependencies reach (use the triage's call-site list to pick them; when unsure, build all).
3. Run the e2e walkthroughs (see the frontend-monorepo `e2e-walkthroughs` skill) for every app that imports a changed package,
   directly or through a workspace package. The apps are normally already running locally. If an app needed for a walkthrough is not
   running, do not start it: mark that PR "not run" and name the app. If no walkthrough covers an affected flow, say so; do not
   click through by hand and call it a pass.
4. Judge the result:
   - passed: every step above ran and passed.
   - failed: any step failed. Quote the deciding error line, name the package or the call site it points to, and say whether it is
     a breaking change from this bump or a pre-existing failure (check by running the same command on the default branch).
   - not run: say which step could not run and why.

Do not comment on, approve, rebase, push, or merge any PR for this part. Do not weaken, skip, or delete a test to get a pass.
Remove each worktree when done with it.

Finish with a table: PR, packages, apps reached, typecheck/lint/unit/build/e2e (passed / failed / not run), and verdict. Then list
every PR whose verdict changed the triage outcome (a quick win that failed or was not run), so it can be reopened.
""" % {"repo": REPO}

SNYK_PROMPT = """\
If the Renovate triage or the smoke test above was interrupted, is still waiting on one of my answers, or stopped before its
summary, stop here and say so. Do not continue.

Otherwise both are done. Now check Snyk for vulnerabilities in %(repo)s that we can fix.

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
smoke = wf.agent("claude", SMOKE_PROMPT, target=session.name, timeout=SMOKE_TIMEOUT_SECS)
wf.log(smoke.response)
snyk = wf.agent("claude", SNYK_PROMPT, target=session.name, timeout=SNYK_TIMEOUT_SECS)
wf.log(snyk.response)
wf.notify("dependency triage done", "frontend-monorepo: Renovate PRs triaged and smoke tested, Snyk findings reviewed", sound="done")
