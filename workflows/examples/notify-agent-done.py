"""Automation: toast when any agent finishes background work."""

import wf

TITLE = "Notify when agent is done"
TRIGGERS = [{"event": "pane.agent_status_changed", "where": {"agent_status": "done"}, "cooldown": 10}]

data = wf.event()["data"]
wf.notify("agent done", "%s in %s" % (data.get("agent"), data["pane_id"]), sound="done")
