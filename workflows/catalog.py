"""Find workflow scripts and read their metadata without running them."""

import ast
import os
import re
import subprocess
from dataclasses import dataclass, field

import schedule

PLUGIN_ID = "marlom.workflows"
NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]*")
METADATA_CONSTANTS = ("TITLE", "TRIGGERS")


@dataclass
class Workflow:
    name: str
    path: str
    scope: str
    title: str
    description: str = ""
    triggers: list = field(default_factory=list)
    error: str = ""


def repo_root(cwd):
    if not os.path.isdir(cwd):
        return cwd
    result = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else cwd


def global_dir():
    config_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or os.path.expanduser("~/.config/herdr/plugins/config/" + PLUGIN_ID)
    return os.path.join(config_dir, "workflows")


def workflow_dirs(cwd):
    """Repo workflows shadow global ones of the same name."""
    return [("repo", os.path.join(repo_root(cwd), ".herdr", "workflows")), ("global", global_dir())]


def read_metadata(path):
    """Title, description, and triggers from literals in the file. Raises SyntaxError or ValueError."""
    with open(path) as f:
        tree = ast.parse(f.read(), path)
    doc = (ast.get_docstring(tree) or "").strip()
    constants = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id in METADATA_CONSTANTS:
                constants[node.targets[0].id] = ast.literal_eval(node.value)
    return constants.get("TITLE"), doc.splitlines()[0] if doc else "", constants.get("TRIGGERS", [])


def validate_triggers(triggers):
    """Each trigger names exactly one of event or schedule. Raises ValueError."""
    if not isinstance(triggers, list):
        raise ValueError("TRIGGERS must be a list")
    for trigger in triggers:
        if not isinstance(trigger, dict) or ("event" in trigger) == ("schedule" in trigger):
            raise ValueError("each trigger needs exactly one of 'event' or 'schedule': %r" % (trigger,))
        if "schedule" in trigger:
            schedule.parse(trigger["schedule"])
        cutoff = trigger.get("catch_up_until")
        if cutoff and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", cutoff):
            raise ValueError("catch_up_until must be HH:MM, got %r" % cutoff)


def load(name, path, scope):
    default_title = name.replace("-", " ").replace("_", " ").title()
    try:
        title, description, triggers = read_metadata(path)
        validate_triggers(triggers)
    except (SyntaxError, ValueError) as e:
        return Workflow(name, path, scope, default_title, error=str(e))
    return Workflow(name, path, scope, title or default_title, description, triggers)


def discover(cwd):
    return discover_in(workflow_dirs(cwd))


def discover_global():
    return discover_in([("global", global_dir())])


def discover_in(dirs):
    found = {}
    for scope, directory in dirs:
        if not os.path.isdir(directory):
            continue
        for filename in sorted(os.listdir(directory)):
            name, ext = os.path.splitext(filename)
            if ext == ".py" and NAME_PATTERN.fullmatch(name) and name not in found:
                found[name] = load(name, os.path.join(directory, filename), scope)
    return sorted(found.values(), key=lambda w: w.name)


def find(cwd, name):
    workflow = next((w for w in discover(cwd) if w.name == name), None)
    if workflow is None:
        raise LookupError("no workflow named %r in %s" % (name, " or ".join(d for _, d in workflow_dirs(cwd))))
    return workflow
