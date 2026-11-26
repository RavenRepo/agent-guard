"""Harness hooks. PreToolUse refuses an edit or command before it runs; Stop refuses an unbacked "done".
A refusal exits 2 with the reason on stderr, which Claude Code and Codex both feed back to the agent.
Any internal error fails open (exit 0) and is logged, so a guard bug never stops every agent."""
from __future__ import annotations

import datetime as _dt
import fnmatch
import hashlib
import json
import os
import sys

from . import config as C
from . import files as F
from . import rules as R
from .claims import JS_CALL_RE, claim_findings, js_string_at, last_assistant_text, transcript_events
from .shell import configure as configure_shell, patch_findings, shell_findings

SHELL_TOOLS = {"Bash", "shell", "exec_command", "local_shell", "unified_exec", "container.exec"}
FILE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def collect_strings(obj, key=None, out=None):
    out = [] if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            collect_strings(v, k, out)
    elif isinstance(obj, list):
        if obj and all(isinstance(x, str) for x in obj) and key in ("command", "cmd", "argv"):
            out.append((key, obj[-1] if len(obj) >= 3 and obj[0].endswith(("sh", "bash", "zsh")) else " ".join(obj)))
        else:
            for v in obj:
                collect_strings(v, key, out)
    elif isinstance(obj, str):
        out.append((key, obj))
    return out


def pre_findings(event: dict, cfg: dict, rules: list[R.Rule]) -> list[R.Finding]:
    tool = str(event.get("tool_name") or "")
    ti = event.get("tool_input") or {}
    cwd = event.get("cwd") or os.getcwd()
    out: list[R.Finding] = []
    if not isinstance(ti, dict):
        ti = {"input": ti}

    # Claude Code file tools.
    if tool in FILE_TOOLS or ("file_path" in ti and ("content" in ti or "new_string" in ti or "edits" in ti)):
        path = F.resolve(ti.get("file_path") or ti.get("notebook_path") or "", cwd)
        disk = F.read_file(path)
        if "content" in ti:
            old, new = disk, ti.get("content") or ""
            out += R.file_change_findings(path, old, new, F.added_lines(old, new), cfg, rules)
        elif "edits" in ti:
            for e in ti.get("edits") or []:
                old, new = e.get("old_string", ""), e.get("new_string", "")
                out += R.file_change_findings(path, old, new, F.added_lines(old, new), cfg, rules)
        elif "new_source" in ti:
            new = ti.get("new_source") or ""
            out += R.file_change_findings(path, "", new, F.added_lines("", new), cfg, rules)
        else:
            old, new = ti.get("old_string", ""), ti.get("new_string", "")
            base = disk.count("\n", 0, disk.find(old)) if old and old in disk else 0
            added = [(ln + base if ln else ln, t) for ln, t in F.added_lines(old, new)]
            out += R.file_change_findings(path, old, new, added, cfg, rules)
        return out

    for key, s in collect_strings(ti):
        if not isinstance(s, str) or not s.strip():
            continue
        if JS_CALL_RE.search(s) and ("tools." in s or tool in ("exec", "code_mode")):
            # Codex code mode: JavaScript that calls exec_command({cmd: ...}) and apply_patch("...").
            for m in JS_CALL_RE.finditer(s):
                lit = js_string_at(s, m.end())
                if lit is None:
                    continue
                out += shell_findings(lit, cwd, cfg, rules) if m.group(1) == "exec_command" else patch_findings(lit, cwd, cfg, rules)
            continue
        if "*** Begin Patch" in s or (key in ("patch", "input") and s.lstrip().startswith(("--- ", "diff --git"))):
            out += patch_findings(s, cwd, cfg, rules)
            continue
        if key in ("command", "cmd", "argv") or (key in (None, "input") and tool in SHELL_TOOLS):
            out += shell_findings(s, cwd, cfg, rules)
    return out


def in_scope(paths: list[str], cfg: dict) -> bool:
    enf = [os.path.realpath(os.path.expanduser(p)) for p in cfg.get("enforce_paths") or []]
    exc = [os.path.realpath(os.path.expanduser(p)) for p in cfg.get("exclude_paths") or []]
    real = [os.path.realpath(p) for p in paths if p]
    if any(r == e or r.startswith(e + os.sep) for r in real for e in exc):
        return False
    if not enf:
        return True
    return any(r == e or r.startswith(e + os.sep) for r in real for e in enf)


def allowed(f: R.Finding, cfg: dict) -> bool:
    if f.rule == "guard-tamper":
        return False
    if f.rule in (cfg.get("disabled_rules") or []):
        return True
    today = _dt.date.today().isoformat()
    for a in cfg.get("allow") or []:
        if a.get("rule") not in (f.rule, "*"):
            continue
        if a.get("until") and str(a["until"]) < today:
            continue
        if fnmatch.fnmatch(f.path or "", a.get("path", "*")):
            return True
    return False


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def log_event(cfg: dict, record: dict) -> None:
    path = os.path.expanduser(cfg.get("log") or C.DEFAULTS["log"])
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError as exc:
        # The decision stands without the log line; say so where the operator can see it.
        sys.stderr.write(f"agent-guard: could not write {path}: {exc}\n")


def report(findings: list[R.Finding]) -> str:
    lines = ["agent-guard refused this action:"]
    seen = set()
    for f in findings:
        key = (f.rule, f.path, f.line, f.text)
        if key in seen:
            continue
        seen.add(key)
        where = f"{f.path}:{f.line}" if f.path and f.line else (f.path or "")
        lines.append(f"- [{f.rule}] {where} {f.text!r}".rstrip())
        lines.append(f"  {f.why}")
    lines.append("These rules apply inside repositories only: put probes and scratch scripts in your scratch folder outside the repository.")
    lines.append("Do the real work instead. Don't work around this check through another tool or command: that is refused too, "
                 "and it is logged. If you believe this is a false positive, stop and report the rule and the line to the coordinator.")
    return "\n".join(lines)


def record(findings: list[R.Finding], cfg: dict, event: dict, kind: str) -> list[R.Finding]:
    """Log the findings that no exception covers, and return them."""
    live = [f for f in findings if not allowed(f, cfg)]
    if live:
        log_event(cfg, {"ts": now(), "kind": kind, "mode": cfg.get("mode"), "tool": event.get("tool_name"),
                        "cwd": event.get("cwd"), "session": event.get("session_id"), "host": os.uname().nodename,
                        "findings": [{"rule": f.rule, "rule_mode": f.mode, "path": f.path, "line": f.line,
                                      "sha": hashlib.sha256(f.text.encode()).hexdigest()[:12]} for f in live]})
    return live


def decide(findings: list[R.Finding], cfg: dict, event: dict, kind: str) -> int:
    live = record(findings, cfg, event, kind)
    blocking = [f for f in live if f.mode == "block"]
    if cfg.get("mode") == "warn" or not blocking:
        return 0
    sys.stderr.write(report(blocking) + "\n")
    return 2


def _session_file(cfg: dict, folder: str, session: str) -> str:
    safe = "".join(ch for ch in str(session) if ch.isalnum() or ch in "-_")[:80] or "unknown"
    return os.path.join(os.path.expanduser(cfg.get("state_dir") or C.DEFAULTS["state_dir"]), folder, safe + ".json")


def _read_json(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else {}


def _write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def stop(event: dict, cfg: dict, rules: list[R.Rule]) -> int:
    """Refuse an unbacked claim up to stop_blocks times per session, then let the turn end but record an unresolved
    verdict the controller can read (agent-guard verdicts) and tell the user. The verdict, not the reply, decides
    whether work is complete."""
    cwd = event.get("cwd") or os.getcwd()
    session = event.get("session_id") or ""
    events = transcript_events(event.get("transcript_path") or "")
    findings = [f for f in claim_findings(last_assistant_text(event, events), events, rules) if not allowed(f, cfg)]
    verdict_path = _session_file(cfg, "verdicts", session) if session else ""
    counter_path = _session_file(cfg, "stops", session) if session else ""
    if not findings:
        if verdict_path and _read_json(verdict_path).get("status") == "unresolved":
            _write_json(verdict_path, {**_read_json(verdict_path), "status": "resolved", "resolved_at": now()})
        if counter_path and os.path.exists(counter_path):
            os.remove(counter_path)  # a clean stop ends the episode; a later claim gets its own refusals
        print("{}")
        return 0
    if counter_path:
        blocks = _read_json(counter_path).get("blocks", 0)
    else:  # no session id to count with: refuse once, never loop
        blocks = cfg.get("stop_blocks", 2) if event.get("stop_hook_active") else 0
    if cfg.get("mode") != "warn" and blocks < cfg.get("stop_blocks", 2):
        if counter_path:
            _write_json(counter_path, {"blocks": blocks + 1, "ts": now()})
        return decide(findings, cfg, event, "stop")
    record(findings, cfg, event, "stop")  # warn mode, or the refusal limit was reached
    verdict = {"status": "unresolved", "session": session, "cwd": cwd, "host": os.uname().nodename, "ts": now(),
               "mode": cfg.get("mode"), "findings": [{"rule": f.rule, "text": f.text[:200]} for f in findings]}
    if verdict_path:
        _write_json(verdict_path, verdict)
    log_event(cfg, {"ts": now(), "kind": "stop-persisted", **{k: verdict[k] for k in ("session", "cwd", "host", "findings")}})
    print(json.dumps({"systemMessage": "agent-guard: the final reply claims success that no passing check backs ("
                      + "; ".join(f.text[:120] for f in findings) + "). Recorded as unresolved; don't treat this work as complete."}))
    return 0


def run(kind: str, config_file: str | None = None) -> int:
    cfg = C.load(config_file)
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return 0
    try:
        configure_shell(cfg)
        rules, errors = R.load(cfg)
        if errors or cfg.get("_errors"):
            log_event(cfg, {"ts": now(), "kind": "config-error", "errors": (cfg.get("_errors", []) + errors)[:20]})
        cwd = event.get("cwd") or os.getcwd()
        if kind == "pre":
            findings = pre_findings(event, cfg, rules)
            if not in_scope([cwd] + [f.path for f in findings if f.path], cfg):
                return 0
            return decide(findings, cfg, event, kind)
        if kind == "stop":
            if not in_scope([cwd], cfg):
                print("{}")
                return 0
            return stop(event, cfg, rules)
    except Exception as exc:  # fail open, but leave a trace
        log_event(cfg, {"ts": now(), "kind": "error", "error": f"{type(exc).__name__}: {exc}"[:300], "tool": event.get("tool_name")})
        if kind == "stop":
            print("{}")
        return 0
    return 0
