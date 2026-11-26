"""Configuration. The system file wins: an agent can't loosen the guard from its environment or home directory.

Resolution order: an explicit --config (only registrations and tests pass one), then /etc/agent-guard/config.json,
then AGENT_GUARD_CONFIG (honoured only when no system file exists, for development), then the user file.
The file is validated on load; an invalid setting falls back to the strict default and is reported."""
from __future__ import annotations

import datetime as _dt
import fnmatch
import json
import os

HOME = os.path.expanduser("~")
SYSTEM_CONFIG = "/etc/agent-guard/config.json"
USER_CONFIG = os.path.join(HOME, ".config", "agent-guard", "config.json")
ROLES = ("builder",)

DEFAULTS = {
    "mode": "enforce",          # "enforce" refuses; "warn" only logs
    "enforce_paths": [],        # empty: every directory; else only under these prefixes
    "exclude_paths": [],
    "roles": [],                # [{"role": "builder", "paths": [glob, ...]}]: assigned by the operator, by path
    "disabled_rules": [],
    "allow": [],                # [{"rule", "path" (glob), "until" (YYYY-MM-DD), "reason", "approved_by"}], all required
    "guard_paths": [],          # more files agents may not change
    "rule_packs": ["/etc/agent-guard/rules.d"],  # extra rule packs (files or directories); they can only add rules
    "log": "~/.local/state/agent-guard/events.jsonl",
    "state_dir": "~/.local/state/agent-guard",
    "stop_blocks": 2,           # refusals of one unbacked claim per session before it is let through and recorded
    "check_wrappers": [],       # programs that run another check, e.g. a lock or queue script: "run-check pnpm test"
}


def config_path(explicit: str | None = None) -> str:
    if explicit:
        return os.path.expanduser(explicit)
    if os.path.exists(SYSTEM_CONFIG):
        return SYSTEM_CONFIG
    if os.environ.get("AGENT_GUARD_CONFIG"):
        return os.path.expanduser(os.environ["AGENT_GUARD_CONFIG"])
    return USER_CONFIG


def _valid_allow(a) -> str | None:
    if not isinstance(a, dict):
        return "not an object"
    for key in ("rule", "path", "until", "reason", "approved_by"):
        if not isinstance(a.get(key), str) or not a[key].strip():
            return f"missing {key}"
    try:
        _dt.date.fromisoformat(a["until"])
    except ValueError:
        return f"until is not a calendar date: {a['until']!r}"
    return None


def validate(cfg: dict) -> list[str]:
    """Fix up an invalid configuration in place, choosing the strict option, and return what was wrong."""
    errors = []
    if cfg.get("mode") not in ("enforce", "warn"):
        errors.append(f"mode {cfg.get('mode')!r} is not enforce or warn; using enforce")
        cfg["mode"] = "enforce"
    kept = []
    for a in cfg.get("allow") or []:
        why = _valid_allow(a)
        if why:
            errors.append(f"allow entry ignored ({why}): {json.dumps(a)[:160]}")
        else:
            kept.append(a)
    cfg["allow"] = kept
    roles = []
    for r in cfg.get("roles") or []:
        if isinstance(r, dict) and r.get("role") in ROLES and isinstance(r.get("paths"), list) and r["paths"]:
            roles.append(r)
        else:
            errors.append(f"role entry ignored: {json.dumps(r)[:160]}")
    cfg["roles"] = roles
    for key in ("enforce_paths", "exclude_paths", "disabled_rules", "guard_paths", "rule_packs", "check_wrappers"):
        if not isinstance(cfg.get(key), list):
            errors.append(f"{key} must be a list; using the default")
            cfg[key] = list(DEFAULTS[key])
    if not isinstance(cfg.get("stop_blocks"), int) or cfg["stop_blocks"] < 1:
        errors.append("stop_blocks must be a positive integer; using 2")
        cfg["stop_blocks"] = 2
    return errors


def load(explicit: str | None = None) -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    path = config_path(explicit)
    errors = []
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                cfg.update(data)
            else:
                errors.append(f"{path}: not a JSON object; using defaults")
        except (OSError, ValueError) as e:
            errors.append(f"{path}: {e}; using defaults")
    errors += validate(cfg)
    cfg["_path"] = path
    cfg["_errors"] = errors
    return cfg


def role_for(path: str, cfg: dict) -> str | None:
    """The operator assigns roles by path in the config. AGENT_GUARD_ROLE can only tighten (set builder), never loosen."""
    real = os.path.realpath(os.path.expanduser(path)) if path else ""
    for r in cfg.get("roles") or []:
        if any(fnmatch.fnmatch(real, os.path.expanduser(g)) or fnmatch.fnmatch(path, os.path.expanduser(g)) for g in r["paths"]):
            return r["role"]
    if os.environ.get("AGENT_GUARD_ROLE") in ROLES:
        return os.environ["AGENT_GUARD_ROLE"]
    return cfg.get("_role_override")
