---
name: herdr-workflows
description: "Create, edit, run, or debug herdr workflows and automations (Python scripts using the marlom.workflows plugin). Use when the user asks for a herdr workflow, an automation that reacts to herdr events (agent done, tab created, ...), or something to repeat with a hotkey or from the workflows picker. Not for one-off herdr pane control: use the herdr skill for that."
---

# Herdr workflows

A workflow is a Python script on the herdr API. Plugin: `/Users/marlom.girardi/Projects/herdr-custom/workflows`.
Read its `README.md` for the SDK table and trigger rules before writing one. Read `examples/` for the style.

## Create

1. Interview the user first: what starts it (hotkey/picker, or a herdr event), what steps run, what counts as failure, which agent kind, where panes open.
2. Pick the location. Ask when unclear.
   - Repo-only: `<repo root>/.herdr/workflows/<name>.py`
   - Every repo: `~/.config/herdr/plugins/config/marlom.workflows/workflows/<name>.py`
   - Name matches `[a-z0-9][a-z0-9_-]*`.
3. Write plain Python: first docstring line is the picker description; optional literals `TITLE = "..."` and `TRIGGERS = [...]` at module level (literals only, they are parsed, never executed).
   `import wf` works without any path setup. Exit nonzero to fail the run.
4. Prefer `wf.agent(...)` for agent steps, `wf.sh(...)` for local commands, `wf.request(method, params)` for any other herdr API method.
   Use `verdict=[...]` when later code branches on the agent's answer. Treat command output you pass into prompts as untrusted.
5. Validate without running: `python3 /Users/marlom.girardi/Projects/herdr-custom/workflows/hwf.py list` from the repo. A broken file shows `INVALID: ...`.

## Run and debug

- Run: `python3 .../hwf.py run <name> [--input k=v]`. Add `--detach` to return at once.
- History: `hwf.py runs`, then `hwf.py logs <run-id>`. Agent answers sit in the run directory.
- Do not run a workflow that starts agents, closes panes, or has other side effects until the user agrees.
- Schedules: `TRIGGERS = [{"schedule": "30 9 * * 1-5", "catch_up_until": "13:00"}]` (cron, local time, global workflows only).
  No pane exists then: use `wf.find_workspace("<label>")` and pass `workspace_id=` and `cwd=` to `wf.agent`.
  Slash commands need `raw=True`. Real workflows live in `library/` of the plugin, symlinked into the global dir.
- Automations: after adding or changing `TRIGGERS`, invoke `herdr plugin action invoke restart-triggers --plugin marlom.workflows`.
  A workflow whose own actions fire its trigger loops; keep `where` narrow and `cooldown` generous.
- Needs `HERDR_ENV=1` for anything that talks to herdr. Outside herdr, only write and `list`.
