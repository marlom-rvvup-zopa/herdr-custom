# Workflows

Workflows and automations for herdr, written as Python scripts on the herdr API instead of YAML.
Standard library only.

## Workflows

A workflow is a `.py` file in `<repo>/.herdr/workflows/` or in `<plugin config dir>/workflows/`
(`~/.config/herdr/plugins/config/marlom.workflows/workflows/`). A repo workflow shadows a global one
with the same name. Names match `[a-z0-9][a-z0-9_-]*`.

The picker and `hwf list` read `TITLE`, `TRIGGERS`, and the first docstring line by parsing the file.
They never run it.

```python
"""Review the working tree diff with an agent."""
import sys
import wf

TITLE = "Review diff"

diff = wf.sh("git", "diff", "HEAD").stdout
if not diff:
    sys.exit(0)
review = wf.agent("claude", "Review this diff:\n" + diff, where="beside", verdict=["APPROVE", "REJECT"])
sys.exit(1 if review.verdict == "REJECT" else 0)
```

## SDK (`import wf`)

| Call | Does |
| --- | --- |
| `wf.request(method, params)` | Any herdr socket API method. Returns the result object |
| `wf.agent(kind, prompt, where=, verdict=, target=, close_on_success=, timeout=)` | Start (or prompt `target`) an agent, wait for it, return `AgentResult(response, pane_id, name, verdict)` |
| `wf.find_workspace(label_part)` | Id of the first open workspace whose label contains the text (first wins when several match). Pass it as `workspace_id=` to `agent`/`open_pane`/`terminal` for runs that start without a pane (schedules) |
| `wf.terminal(command, where=)` | Run a shell command in a new pane. Returns the pane id |
| `wf.open_pane(where)` / `wf.close(pane)` | Raw pane placement: `tab`, `beside`, `below` |
| `wf.sh(*argv)` | Local command. Raises on nonzero exit unless `check=False` |
| `wf.notify(title, body, sound)` | herdr toast |
| `wf.input(name, default)` | Value of `--input name=value` |
| `wf.context()` | `workspace_id`, `tab_id`, `pane_id`, `cwd`, `run_id`, `run_dir` |
| `wf.event()` | The triggering event, or `None` |

`raw=True` sends the prompt unchanged, which slash commands such as `/renovate-pr-triage org/repo` need.
Without it the SDK appends answer-file and verdict instructions to the prompt.

A script fails when it exits nonzero or raises. Control flow, loops, and parallelism are plain Python.

## Automations

```python
TRIGGERS = [{"event": "pane.agent_status_changed", "where": {"agent_status": "done"}, "cooldown": 10}]
```

`triggerd.py` subscribes to herdr events and starts matching workflows detached, with the event in
`wf.event()`. `where` keys are dotted paths into the event data. A workflow never starts while its
previous run is still going (a schedule waits and retries), and `cooldown` (default 5s) spaces out starts.
Events: workspace/worktree/tab `created|closed|focused|...`, `pane.created|closed|exited|focused|agent_detected`,
and `pane.agent_status_changed` (`pane_id`, `agent`, `agent_status`, `previous_status`).
A trigger has either `event` or `schedule`:

```python
TRIGGERS = [{"schedule": "30 9 * * 1-5", "catch_up_until": "13:00"}]
```

`schedule` is a 5-field cron expression in local time and works for global workflows only. With
`catch_up_until`, a slot missed while the Mac slept or herdr was closed still runs on wake-up until that
time of day, once. The last fired slot is kept in `~/.config/herdr/workflows-state/triggerd/schedule.json`.
Scheduled runs have no pane, so use `wf.find_workspace()`.

Repo workflows trigger only for panes whose cwd is inside that repo. Run the action
`restart-triggers` after adding a trigger.

## CLI

```
python3 hwf.py list [--json]
python3 hwf.py run <name> [--input k=v]... [--detach]
python3 hwf.py runs [--limit N] [--json]
python3 hwf.py logs <run-id>
python3 hwf.py edit <name> --with vscode|claude
```

Runs are stored under `~/.config/herdr/workflows-state/runs/<id>/` (`run.json`, `output.log`, agent answers).
A script is code you chose to run: there is no sandbox.

## Sidebar

`wf.agent()` tags the agent it starts with the pane token `$workflow`, `"<icon> <name>"` (source `marlom.workflows`, kept up to
24h; change the icon with `wf.WORKFLOW_ICON`). It is one token because herdr draws a separator between tokens of a row.
Other agents have no such token, so the row stays hidden. Put `{ token = "$workflow", ... }` in a row of its own under
`[ui.sidebar.agents]` in `config.toml`. Agents you prompt with `target=` are not tagged.

## Picker keys

`enter` runs (after a y/n), `e` edits: `v` opens the real file in VS Code, `c` opens a Claude tab that is told the file,
its triggers, its last run log and the docs, and asks what to change before touching anything. `tab` switches to the runs table.

## Install

```
herdr plugin link /Users/marlom.girardi/Projects/herdr-custom/workflows
```

```toml
[[keys.command]]
key = "prefix+k"
type = "shell"
description = "workflows picker"
command = "herdr plugin action invoke open-picker --plugin marlom.workflows"
```

`library/` holds my real workflows; they are symlinked into the global workflows directory.

Tests: `python3 -I -m unittest discover -s . -t .`
