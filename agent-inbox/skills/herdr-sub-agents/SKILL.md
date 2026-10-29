---
name: herdr-sub-agents
description: "Spawn a sub agent as its own Claude session in a herdr tab, shown nested under the current agent in the herdr sidebar. Use when running inside herdr (HERDR_ENV=1) and the user asks to spawn, delegate to, or fan out to sub agents they can watch, resume, or see in the sidebar. Not for hidden in-process helpers: use the built-in Agent tool for those, or when HERDR_ENV is not 1."
---

# Herdr sub agents

`spawn_sub.py` (agent-inbox plugin) opens a new herdr tab, starts a Claude session in it, sends it a prompt, and prints its answer. The sub agent is linked to the calling agent, so the sidebar draws it under its parent (`├─` / `└─`) and the family sorts and collapses together.

## Use

```bash
python3 /Users/marlom.girardi/Projects/herdr-custom/agent-inbox/spawn_sub.py "<label>" "<prompt>"
```

- `<label>`: short tab name, shown as `sub · <label>` (28 chars max).
- `<prompt>`: self-contained task. The sub agent has none of this conversation's context.
- The sub agent starts in the current directory.
- Output: `[sub agent <name>, herdr tab <id>]`, then the sub agent's final answer.

## Rules

- The command blocks until the sub agent is done (30 minute cap). Always run it with `run_in_background: true`, and launch several in one turn to fan out. Read each result when its completion notification arrives.
- Check `echo $HERDR_ENV` is `1` first. If not, the script exits with an error: use the built-in Agent tool.
- A sub agent can call this script itself; its children nest one level deeper (max depth 5).
- The directory must already be trusted by Claude Code. On an untrusted folder the sub agent stops at the trust prompt and the command waits. Never accept that prompt for the user: tell them to trust the folder.
- The sub agent is a full session: it stays open after answering, can be resumed from its tab, and counts as normal usage.

## Sidebar

- Sub agents from this script show as their own rows under the parent (`├─` / `└─`).
- Claude's built-in named sub agents (Agent tool with `name`) have no herdr pane. The daemon lists the running ones as extra rows under the parent session. A sub agent drops off the list when it finishes or ends its turn.
- After editing the sidebar rows in `~/.config/herdr/config.toml`, run `herdr server reload-config`; the rows do not show until then.
