"""Success claims at the end of a turn, judged against what the transcript shows actually ran."""
from __future__ import annotations

import json
import os
import re

from . import files as F
from .rules import Finding, Rule
from .shell import check_types, masked_exit, strip_heredocs, strip_quoted

NEGATION_RE = re.compile(r"\b(?:not|never|no longer|isn't|aren't|nothing is|nor)\b[^.\n]{0,40}$", re.I)
EXIT_FAIL_RE = re.compile(r"\bExit code:?\s*[1-9]\d*|\"exit_code\"\s*:\s*[1-9]|Process exited with code [1-9]|\bexited with (?:code|status) [1-9]")
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"}
WRITE_CMD_RE = re.compile(r"\*\*\* Begin Patch|(?:^|[\s;&|])(?:sed\s+-[a-zA-Z]*i|perl\s+-[a-zA-Z]*i)\b|>{1,2}\s*[\w./-]+\.(?:[cm]?[jt]sx?|py|go|rs|sql)\b")
# Which passing checks back which claim. The project's whole check backs every claim.
SATISFIES = {"tests": {"tests", "full"}, "typecheck": {"typecheck", "full"}, "lint": {"lint", "full"},
             "build": {"build", "full"}, "full": {"full"}, "generic": {"tests", "full"}}


def claim_kind(phrase: str) -> str:
    p = phrase.lower()
    if "type" in p or "tsc" in p:
        return "typecheck"
    if "lint" in p:
        return "lint"
    if "build" in p:
        return "build"
    if "full check" in p or "the check" in p or "checks" in p or re.search(r"\bci\b", p):
        return "full"
    if "test" in p:
        return "tests"
    return "generic"
JS_CALL_RE = re.compile(r"\b(exec_command|apply_patch)\s*\(\s*(\{\s*\"?cmd\"?\s*:\s*)?")


def js_string_at(src: str, i: int) -> str | None:
    """Decode the JavaScript string literal that starts at src[i]."""
    if i >= len(src) or src[i] not in "\"'`":
        return None
    q, j, buf = src[i], i + 1, []
    while j < len(src):
        c = src[j]
        if c == "\\" and j + 1 < len(src):
            n = src[j + 1]
            if n == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", src[j + 2:j + 6]):
                buf.append(chr(int(src[j + 2:j + 6], 16)))
                j += 6
                continue
            buf.append({"n": "\n", "t": "\t", "r": "\r", "0": "\0"}.get(n, n))
            j += 2
            continue
        if c == q:
            return "".join(buf)
        buf.append(c)
        j += 1
    return None


def product_edit(path: str) -> bool:
    """An edit that can invalidate earlier check results: code, tests or gate files, not notes or evidence."""
    if not path or not (F.ext_of(path) in F.CODE_EXT or F.is_gate_config(path)):
        return False
    if os.path.isdir(os.path.dirname(path)):
        return F.repo_root(path) is not None
    return not path.startswith("/tmp/")


def transcript_events(path: str) -> list[tuple[str, dict]]:
    """Normalise Claude Code and Codex transcripts into (kind, data) events in order."""
    events: list[tuple[str, dict]] = []
    calls: dict[str, str] = {}
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return events
    with fh:
        for line in fh:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            first = len(events)
            _transcript_line(o, events, calls)
            for _, d in events[first:]:
                d["ts"] = o.get("timestamp", "")
    return events


def _transcript_line(o: dict, events: list, calls: dict) -> None:
    """Append the events one transcript line holds (Claude Code message lines, Codex response items)."""
    msg = o.get("message") if isinstance(o.get("message"), dict) else None
    if msg is not None:  # Claude Code
        role, content = msg.get("role"), msg.get("content")
        if isinstance(content, str):
            events.append(("user_text" if role == "user" else "assistant_text", {"text": content}))
            return
        for it in content or []:
            if not isinstance(it, dict):
                continue
            t = it.get("type")
            if t == "text":
                events.append(("user_text" if role == "user" else "assistant_text", {"text": it.get("text", "")}))
            elif t == "tool_use":
                name, inp = it.get("name", ""), it.get("input") or {}
                cmd = inp.get("command", "") if isinstance(inp, dict) else ""
                path = (inp.get("file_path") or inp.get("notebook_path") or "") if isinstance(inp, dict) else ""
                calls[it.get("id", "")] = cmd
                events.append(("call", {"id": it.get("id"), "name": name, "cmd": cmd, "path": path}))
            elif t == "tool_result":
                c = it.get("content")
                txt = c if isinstance(c, str) else json.dumps(c)
                events.append(("result", {"id": it.get("tool_use_id"),
                                          "error": bool(it.get("is_error")) or bool(EXIT_FAIL_RE.search(txt[:400])),
                                          "cmd": calls.get(it.get("tool_use_id", ""), ""), "text": txt[:2000] + txt[-2000:]}))
        return
    p = o.get("payload") if isinstance(o.get("payload"), dict) else {}
    pt = p.get("type")
    if pt == "message":
        text = " ".join(c.get("text", "") for c in p.get("content") or [] if isinstance(c, dict))
        if p.get("role") in ("user", "assistant"):
            events.append((p["role"] + "_text", {"text": text}))
    elif pt in ("function_call", "custom_tool_call"):
        raw = p.get("arguments") or p.get("input") or ""
        cmds = [js_string_at(raw, m.end()) or "" for m in JS_CALL_RE.finditer(raw) if m.group(1) == "exec_command"]
        patch = "*** Begin Patch" in raw
        cmd = " ; ".join(cmds) or _function_command(raw, p.get("name", ""))
        paths = [f["path"] for f in F.parse_patch(raw.replace("\\n", "\n"))] if patch else []
        calls[p.get("call_id", "")] = cmd
        events.append(("call", {"id": p.get("call_id"), "name": "apply_patch" if patch else p.get("name", ""), "cmd": cmd,
                                "path": next((x for x in paths if product_edit(x)), paths[0] if paths else "")}))
    elif pt in ("function_call_output", "custom_tool_call_output"):
        outp = p.get("output")
        txt = outp if isinstance(outp, str) else json.dumps(outp)
        events.append(("result", {"id": p.get("call_id"), "error": bool(EXIT_FAIL_RE.search(txt)),
                                  "cmd": calls.get(p.get("call_id", ""), ""), "text": txt[:2000] + txt[-2000:]}))


def _function_command(raw: str, name: str) -> str:
    """The shell command a Codex function call ran: JSON arguments with cmd or command (a string or an argv list)."""
    if name not in ("shell", "exec_command", "local_shell", "container.exec"):
        return ""
    try:
        args = json.loads(raw)
    except ValueError:
        return raw
    cmd = args.get("cmd") or args.get("command") if isinstance(args, dict) else None
    if isinstance(cmd, list):
        return cmd[-1] if len(cmd) >= 3 and str(cmd[0]).endswith(("sh", "bash", "zsh")) else " ".join(map(str, cmd))
    return cmd if isinstance(cmd, str) else ""


def evidence_state(events: list[tuple[str, dict]], kinds: set[str]) -> tuple[int, int, int]:
    """Indexes of the last product edit, and of the last passing and failing run of a check of one of these kinds.
    Only an exit status the harness observed counts. Log text a command printed does not, because any command can print
    "rc=0"; a check run in the background is cited by the agent and judged by the controller, not claimed here.
    A check piped through a filter without pipefail is not evidence either way: its exit status is the filter's."""
    last_edit = last_ok = last_fail = -1
    for i, (kind, d) in enumerate(events):
        cmd = d.get("cmd") or ""
        if kind == "call" and ((d.get("name") in EDIT_TOOLS and product_edit(d.get("path") or "")) or WRITE_CMD_RE.search(cmd)):
            last_edit = i
        elif kind == "result" and check_types(cmd) & kinds and not masked_exit(strip_quoted(strip_heredocs(cmd))):
            if d.get("error"):
                last_fail = i
            else:
                last_ok = i
    return last_edit, last_ok, last_fail


def claim_findings(text: str, events: list[tuple[str, dict]], rules: list[Rule], builtins: bool = True) -> list[Finding]:
    out: list[Finding] = []
    for r in (r for r in rules if r.kind == "claim"):
        hits = [m for m in r.pattern.finditer(text) if not NEGATION_RE.search(text[max(0, m.start() - 60):m.start()])]
        if not hits:
            continue
        if not r.needs_evidence:
            out.append(Finding(r.id, "", None, hits[0].group(0), r.help, r.mode))
            continue
        for hit in hits:
            kind = claim_kind(hit.group(0))
            last_edit, last_ok, last_fail = evidence_state(events, SATISFIES[kind])
            if last_ok <= last_edit or last_ok < last_fail:
                need = " or ".join(sorted(SATISFIES[kind]))
                detail = (f"no {need} check passed after the last code edit" if last_ok <= last_edit
                          else f"the latest {need} check failed")
                out.append(Finding(r.id, "", None, f"{hit.group(0)!r}: {detail}", r.help, r.mode))
                break
    return out


def last_assistant_text(event: dict, events: list[tuple[str, dict]]) -> str:
    for key in ("last_assistant_message", "last-assistant-message", "assistant_message"):
        if isinstance(event.get(key), str):
            return event[key]
    texts = []
    for kind, d in reversed(events):
        if kind == "assistant_text":
            texts.append(d.get("text", ""))
        elif kind in ("call", "result", "user_text") and texts:
            break
    return "\n".join(reversed(texts))
