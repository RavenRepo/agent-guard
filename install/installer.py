#!/usr/bin/env python3
"""Install, prove, inspect and undo agent-guard on one host.

  sudo python3 install/installer.py install --agent-user dev --enforce-paths /home/dev/work:/home/dev/repos
  sudo python3 install/installer.py status
  sudo python3 install/installer.py undo [--purge]

Install is a transaction:
- it stages the package in a root-owned directory and installs from there;
- it records each registration file's original content, or its absence, the first time it touches it (a reinstall
  never overwrites that record);
- it writes every file atomically;
- it registers the hooks as system hooks (Claude Code managed settings and /etc/codex/hooks.json, which Codex trusts
  by policy).
It then runs the activation proof as the agent user with the real clients. If the proof fails, the registrations
are rolled back and the command exits non-zero. Undo removes only agent-guard's own entries, and deletes a
registration file it created when nothing else is left in it.

--root PREFIX puts every system path under PREFIX and skips the root check and the proof (for tests)."""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import pwd
import shutil
import subprocess
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent
MARK = "agent-guard"
HOOK_EVENTS = {
    "claude": [("PreToolUse", "Edit|Write|MultiEdit|NotebookEdit|Bash", "pre"), ("Stop", None, "stop"), ("SubagentStop", None, "stop")],
    "codex": [("PreToolUse", "*", "pre"), ("Stop", None, "stop")],
}


class Paths:
    def __init__(self, root: str):
        r = Path(root)
        self.root = r
        self.lib = r / "usr/local/lib/agent-guard"
        self.etc = r / "etc/agent-guard"
        self.config = self.etc / "config.json"
        self.rules_d = self.etc / "rules.d"
        self.state = r / "var/lib/agent-guard"
        self.state_file = self.state / "state.json"
        self.logs = r / "var/log/agent-guard"
        self.registrations = {"claude": r / "etc/claude-code/managed-settings.json", "codex": r / "etc/codex/hooks.json"}

    def system(self, p: Path) -> str:
        """The path as the hooks will see it on the host (without the test prefix)."""
        return "/" + str(p.relative_to(self.root)) if self.root != Path("/") else str(p)


class Log:
    def __init__(self, folder: Path):
        folder.mkdir(parents=True, exist_ok=True)
        os.chmod(folder, 0o750)
        self.path = folder / f"install-{_dt.datetime.now(_dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.log"
        self.fh = open(self.path, "a", encoding="utf-8")

    def __call__(self, *parts):
        line = " ".join(str(p) for p in parts)
        print(line)
        self.fh.write(line + "\n")
        self.fh.flush()


def atomic_write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.agent-guard-{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def read_text(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.is_file() else None


def load_state(paths: Paths) -> dict:
    text = read_text(paths.state_file)
    return json.loads(text) if text else {"originals": {}, "installs": []}


def strip_ours(data: dict) -> dict:
    hooks = data.get("hooks")
    if isinstance(hooks, dict):
        for event in list(hooks):
            groups = [g for g in hooks[event] if MARK not in json.dumps(g)]
            if groups:
                hooks[event] = groups
            else:
                del hooks[event]
        if not hooks:
            del data["hooks"]
    return data


def registration(current: str | None, client: str, hook: str) -> str:
    data = strip_ours(json.loads(current)) if current else {}
    hooks = data.setdefault("hooks", {})
    for event, matcher, arg in HOOK_EVENTS[client]:
        group = {"hooks": [{"type": "command", "command": f"{hook} {arg}", "timeout": 10}]}
        if matcher:
            group["matcher"] = matcher
        hooks.setdefault(event, []).append(group)
    return json.dumps(data, indent=2) + "\n"


def write_registrations(paths: Paths, plan: dict[str, str], log: Log) -> dict[str, str | None]:
    """Write every registration or none: on a failure, put back what was there before this run."""
    before = {c: read_text(paths.registrations[c]) for c in plan}
    written = []
    try:
        for client, text in plan.items():
            atomic_write(paths.registrations[client], text)
            written.append(client)
            log("registered", paths.registrations[client])
    except Exception:
        restore(paths, {c: before[c] for c in written}, log)
        raise
    return before


def restore(paths: Paths, before: dict[str, str | None], log: Log) -> None:
    for client, text in before.items():
        target = paths.registrations[client]
        if text is None:
            target.unlink(missing_ok=True)
            log("removed", target)
        else:
            atomic_write(target, text)
            log("restored", target)


def manifest(folder: Path) -> dict[str, str]:
    out = {}
    for p in sorted(folder.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            out[str(p.relative_to(folder))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def install_package(paths: Paths, stamp: str, log: Log) -> dict[str, str]:
    staging = paths.state / f"staging-{stamp}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    for part in ("agentguard", "bin"):
        shutil.copytree(SOURCE / part, staging / part, ignore=shutil.ignore_patterns("__pycache__"))
    staged = manifest(staging)
    fresh = paths.lib.with_name("agent-guard.new")
    shutil.rmtree(fresh, ignore_errors=True)
    shutil.copytree(staging, fresh)
    if manifest(fresh) != staged:
        raise RuntimeError("the installed copy differs from the staged copy")
    for p in fresh.rglob("*"):
        os.chmod(p, 0o755 if p.is_dir() or p.name == "agent-guard" else 0o644)
    old = paths.lib.with_name("agent-guard.old")
    shutil.rmtree(old, ignore_errors=True)
    if paths.lib.exists():
        os.replace(paths.lib, old)
    os.replace(fresh, paths.lib)
    shutil.rmtree(old, ignore_errors=True)
    shutil.rmtree(staging, ignore_errors=True)
    log("installed", paths.lib, f"({len(staged)} files)")
    return staged


def write_config(paths: Paths, agent_home: str, enforce_paths: list[str], mode: str | None, log: Log) -> dict:
    current = read_text(paths.config)
    if current is None:
        cfg = {"mode": mode or "warn", "enforce_paths": enforce_paths or [agent_home], "exclude_paths": [], "roles": [],
               "disabled_rules": [], "allow": [], "rule_packs": [paths.system(paths.rules_d)],
               "log": f"{agent_home}/.local/state/agent-guard/events.jsonl", "state_dir": f"{agent_home}/.local/state/agent-guard"}
        atomic_write(paths.config, json.dumps(cfg, indent=2) + "\n")
        log("wrote", paths.config)
    else:
        cfg = json.loads(current)
        changed = False
        if mode and cfg.get("mode") != mode:
            cfg["mode"] = mode
            changed = True
        if enforce_paths and cfg.get("enforce_paths") != enforce_paths:
            cfg["enforce_paths"] = enforce_paths
            changed = True
        if changed:
            atomic_write(paths.config, json.dumps(cfg, indent=2) + "\n")
            log("updated", paths.config)
        else:
            log("kept", paths.config)
    paths.rules_d.mkdir(parents=True, exist_ok=True)
    return cfg


def prove(paths: Paths, agent_user: str, log: Log) -> bool:
    entry = paths.lib / "bin" / "agent-guard"
    cmd = ["sudo", "-u", agent_user, "-H", "--", "bash", "-lc", f"/usr/bin/python3 -I -B {entry} prove --installed"]
    run = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    for line in (run.stdout + run.stderr).splitlines():
        log("  ", line)
    return run.returncode == 0


def cmd_install(args, paths: Paths, log: Log) -> int:
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    user = pwd.getpwnam(args.agent_user)
    enforce_paths = [p for p in (args.enforce_paths or "").split(":") if p]
    state = load_state(paths)
    for client, target in paths.registrations.items():
        if client not in state["originals"]:  # first touch only: a reinstall never overwrites the original
            state["originals"][client] = {"existed": target.is_file(), "content": read_text(target)}
    paths.state.mkdir(parents=True, exist_ok=True)
    atomic_write(paths.state_file, json.dumps(state, indent=2) + "\n", 0o600)
    staged = install_package(paths, stamp, log)
    cfg = write_config(paths, user.pw_dir, enforce_paths, args.mode, log)
    hook = f"/usr/bin/python3 -I -B {paths.system(paths.lib / 'bin' / 'agent-guard')} hook --config {paths.system(paths.config)}"
    plan = {c: registration(read_text(paths.registrations[c]), c, hook) for c in paths.registrations}
    before = write_registrations(paths, plan, log)
    proved = args.skip_prove or prove(paths, args.agent_user, log)
    if not proved:
        log("activation proof FAILED: rolling back the registrations")
        restore(paths, before, log)
        return 1
    state["installs"].append({"at": stamp, "version": (SOURCE / "agentguard/__init__.py").read_text().split('"')[1],
                              "files": len(staged), "mode": cfg.get("mode")})
    atomic_write(paths.state_file, json.dumps(state, indent=2) + "\n", 0o600)
    log(f"done. effective mode: {cfg.get('mode')}; enforce paths: {cfg.get('enforce_paths')}; proof: "
        + ("skipped (--skip-prove)" if args.skip_prove else "passed"))
    return 0


def cmd_undo(args, paths: Paths, log: Log) -> int:
    state = load_state(paths)
    for client, target in paths.registrations.items():
        text = read_text(target)
        if text is None:
            continue
        data = strip_ours(json.loads(text))
        original = state["originals"].get(client, {})
        if not data and not original.get("existed", True):
            target.unlink()
            log("removed", target, "(agent-guard created it)")
        else:
            atomic_write(target, json.dumps(data, indent=2) + "\n")
            log("cleaned", target)
    if args.purge:
        for p in (paths.lib, paths.etc, paths.state):
            shutil.rmtree(p, ignore_errors=True)
            log("purged", p)
    else:
        log("left", paths.lib, "and", paths.config, "in place (use --purge to remove them)")
    return 0


def cmd_status(args, paths: Paths, log: Log) -> int:
    cfg = json.loads(read_text(paths.config) or "{}")
    for client, target in paths.registrations.items():
        text = read_text(target)
        ours = text is not None and MARK in text
        log(f"{client}: {'registered' if ours else 'not registered'} in {target}")
    log(f"mode: {cfg.get('mode', 'not configured')}; enforce paths: {cfg.get('enforce_paths')}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", choices=["install", "undo", "status"])
    p.add_argument("--agent-user", default="dev")
    p.add_argument("--enforce-paths", help="colon-separated; default: the agent user's home (new config only)")
    p.add_argument("--mode", choices=["warn", "enforce"], help="set the policy mode (default: warn for a new config, else unchanged)")
    p.add_argument("--purge", action="store_true", help="undo: also remove the package, policy and state")
    p.add_argument("--root", default="/", help=argparse.SUPPRESS)
    p.add_argument("--skip-prove", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args()
    testing = args.root != "/"
    if not testing and os.geteuid() != 0:
        print("run as root (sudo)", file=sys.stderr)
        return 1
    if args.skip_prove and not testing:
        print("--skip-prove is for tests only", file=sys.stderr)
        return 1
    paths = Paths(args.root)
    log = Log(paths.logs)
    log(f"== agent-guard {args.action} on {os.uname().nodename}, log {log.path}")
    return {"install": cmd_install, "undo": cmd_undo, "status": cmd_status}[args.action](args, paths, log)


if __name__ == "__main__":
    sys.exit(main())
