"""Tab title rules: what a tab is called and whether the plugin may rename it."""

import re

TITLE_CHARS = 40
# Labels herdr or the previous auto-title plugin wrote: the bare position ("3") or "3 · name".
MANAGED_LABEL = re.compile(r"\d+( · .*)?$")


def tab_label(position, ticket, title):
    """e.g. "3 · PAY-1226: Browser SDK event caching"; the position doubles as the switch key."""
    return "%d · %s" % (position, "%s: %s" % (ticket, title) if ticket else title)


def may_rename(label, last_set):
    """True unless the user gave the tab a name of their own."""
    return label == last_set or bool(MANAGED_LABEL.match(label))


def parse_title_and_summary(output):
    """Split the two-line model output into (title, summary); None when it is not in that shape."""
    lines = [" ".join(line.split()).strip("\"'") for line in output.splitlines() if line.strip()]
    if len(lines) != 2:
        return None
    title = lines[0]
    if len(title) > TITLE_CHARS:
        title = title[:TITLE_CHARS].rsplit(" ", 1)[0]
    return title, lines[1]
