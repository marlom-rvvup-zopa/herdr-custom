# Agent Inbox

Herdr plugin (`marlom.agent-inbox`) that sorts agents in the sidebar by state and last prompt, shows Jira tickets, PR status and sub agents on each agent's row, and keeps a settled list you can resume from. Python, standard library only.

## Install

```
herdr plugin link /Users/marlom.girardi/Projects/herdr-custom/agent-inbox
```

The skill `herdr-sub-agents` lives in `skills/` and is symlinked into `~/.claude/skills/`.

## Herdr config (`~/.config/herdr/config.toml`)

Run `herdr server reload-config` after editing the sidebar rows: they do not show until then.

### Key bindings

```toml
[[keys.command]]
key = "prefix+m"
type = "shell"
description = "settle agent (close pane, keep it in the settled list)"
command = "herdr plugin action invoke settle --plugin marlom.agent-inbox"

[[keys.command]]
key = "prefix+i"
type = "shell"
description = "open the agent inbox"
command = "herdr plugin pane open --plugin marlom.agent-inbox --entrypoint inbox"

[[keys.command]]
key = "super+i"
type = "shell"
description = "open the agent inbox"
command = "herdr plugin pane open --plugin marlom.agent-inbox --entrypoint inbox"

[[keys.command]]
key = "prefix+t"
type = "shell"
description = "open the agent's Jira ticket(s) in the browser"
command = "herdr plugin action invoke open-ticket --plugin marlom.agent-inbox"

[[keys.command]]
key = "prefix+u"
type = "shell"
description = "open the agent's pull request(s) in the browser"
command = "herdr plugin action invoke open-pr --plugin marlom.agent-inbox"

[[keys.command]]
key = "prefix+f"
type = "shell"
description = "collapse or expand the focused agent's sub agents"
command = "herdr plugin action invoke toggle-subs --plugin marlom.agent-inbox"

[[keys.command]]
key = "prefix+shift+i"
type = "popup"
description = "quick actions for the focused agent (c close/settle, s snooze, n new claude tab, p new claude pane to the right)"
command = "python3 /Users/marlom.girardi/Projects/herdr-custom/agent-inbox/quick_tui.py"
width = 46
height = 13

[[keys.command]]
key = "prefix+shift+z"
type = "shell"
description = "toggle no sleep (caffeinate)"
command = "herdr plugin action invoke toggle-no-sleep --plugin marlom.agent-inbox"
```

`quick_tui.py` is not a plugin pane: the popup binding runs it directly and herdr passes the focused pane in `HERDR_ACTIVE_PANE_ID`.

### Sidebar

The daemon reports tokens per agent pane, and the sidebar draws only the tokens listed in `[ui.sidebar.agents]` `rows`. A token missing from the rows is never shown. Each row is a list of `{ token = "$name", fg = "#hex", bold = bool, dim = bool }`.

| Tokens | Meaning |
| --- | --- |
| `$st_working`, `$st_waiting`, `$st_blocked`, `$st_done`, `$st_idle` | State label. Only one is set per agent. `waiting` = waiting on a sub agent or a script |
| `$shell_mark`, `$snooze_mark` | Shell running in the agent, agent woken from a snooze |
| `$last_reply` | Age of the last answer |
| `$title` | Session title |
| `$hint` | Short summary of the session |
| `$ticket_line`, `$ticket_line2`, `$ticket_line3`, `$ticket_line4` | Jira tickets, one sidebar row each. The row also holds the PR tokens of the same number |
| `$pr_*`, `$prl_*`, `$pr3_*`, `$pr4_*` | PR links for ticket rows 1 to 4. Suffixes: `pass`, `pending`, `fail`, `open`, `merged`, `closed`. One token per PR state so each gets its own color |
| `$native_1` to `$native_4` | Claude built-in named sub agents running under the agent, one row each |
| `$workflow` | Set by the workflows plugin, not by this one |

Current row layout (colors in the real config): state row, title, `$workflow`, `$hint`, then one row per ticket/PR group (`$ticket_line` + `$pr_*`, `$ticket_line2` + `$prl_*`, ...), then one row per `$native_N`. Rows listed as `[]` are spacers.

The daemon also sets `$sort_key` and `$hidden` for ordering and filtering. Do not put them in a row.

Other `[ui]` settings the real config relies on: `agent_panel_sort = "priority"`, `status_indicators = "symbols"`, `show_agent_labels_on_pane_borders = true`, `sidebar_min_width = 50`, `sidebar_max_width = 50`. The ticket and PR rows are cut to 46 cells (`LINK_ROW_WIDTH` in `daemon.py`), so keep the sidebar at least that wide.

## Behavior

- The daemon starts with herdr and on `pane.agent_detected`. Restart it with `herdr plugin action invoke restart-daemon --plugin marlom.agent-inbox`.
- Jira status uses `acli`. If the login expired, the `jira-login` pane opens.
- Session tickets are links: any text matching a Jira key opens through the `jira-ticket` link handler.
- Tabs and panes holding an agent are renamed from the first ticket and the session summary.
- No sleep starts on with herdr and holds `caffeinate`.

## Sub agents

`spawn_sub.py "<label>" "<prompt>"` starts a Claude session in a new herdr tab, nested under the calling agent in the sidebar. See `skills/herdr-sub-agents/SKILL.md`.

`block_builtin_agents.py` is a Claude Code `PreToolUse` hook for the `Agent` tool that redirects to `spawn_sub.py` inside herdr. It is not configured in Claude settings by default.

## Tests

```
python3 -I -m unittest discover -s . -t .
```
