"""Activation proof: make the real Claude Code and Codex clients deliver events to the guard, and check the result.

Each client runs in a fresh profile against a scripted model on loopback (no account, no network). The script
makes the client propose one shortcut and then claim "All tests pass", so both the PreToolUse and the Stop hook
must fire.

  --installed   uses the system registrations (/etc/claude-code/managed-settings.json, /etc/codex/hooks.json) and
                the system policy. It asserts that each hook logged, and that Codex lists the hooks as managed and
                enabled (managed hooks are trusted by policy; user hooks wait for review and are skipped).
  --disposable  registers the hooks only for this run, with a throwaway policy in the given mode, so enforcement can
                be tested without touching the host. In enforce mode it asserts the shortcut did not happen."""
from __future__ import annotations

import http.server
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import config as C
from . import self_command

PROBE_FILE = "probe.ts"
PROBE_TEXT = "// TODO: agent-guard activation probe\nexport const probe = 1;\n"
PROBE_CMD = "pnpm test || true"
CLAIM = "All tests pass."


class _Model(http.server.ThreadingHTTPServer):
    def __init__(self, handler, script):
        super().__init__(("127.0.0.1", 0), handler)
        self.script, self.requests = script, []

    def __enter__(self):
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.shutdown()
        self.server_close()


class _Messages(http.server.BaseHTTPRequestHandler):
    """Scripted Anthropic Messages API: one tool call per request, then a final text."""

    def log_message(self, *args):
        return None

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        if not self.path.split("?")[0].endswith("/messages"):
            self.send_error(404)
            return
        self.server.requests.append(body)
        index = sum(1 for r in self.server.requests if r.get("tools"))
        step = self.server.script[index - 1] if 0 < index <= len(self.server.script) else None
        if step:
            block, reason = {"type": "tool_use", "id": f"toolu_{index}", "name": step[0], "input": step[1]}, "tool_use"
        else:
            block, reason = {"type": "text", "text": CLAIM}, "end_turn"
        message = {"type": "message", "id": f"msg_{len(self.server.requests)}", "role": "assistant", "model": body.get("model", "m"),
                   "content": [block], "stop_reason": reason, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}
        if body.get("stream"):
            start = {**block, **({"input": {}} if reason == "tool_use" else {"text": ""})}
            delta = ({"type": "input_json_delta", "partial_json": json.dumps(block["input"])} if reason == "tool_use"
                     else {"type": "text_delta", "text": block["text"]})
            events = [("message_start", {"message": {**message, "content": [], "stop_reason": None}}),
                      ("content_block_start", {"index": 0, "content_block": start}),
                      ("content_block_delta", {"index": 0, "delta": delta}), ("content_block_stop", {"index": 0}),
                      ("message_delta", {"delta": {"stop_reason": reason, "stop_sequence": None}, "usage": {"output_tokens": 1}}),
                      ("message_stop", {})]
            payload = "".join(f"event: {n}\ndata: {json.dumps({'type': n, **f})}\n\n" for n, f in events).encode()
            ctype = "text/event-stream"
        else:
            payload, ctype = json.dumps(message).encode(), "application/json"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _Responses(http.server.BaseHTTPRequestHandler):
    """Scripted OpenAI Responses API: one exec_command call, then a final message."""

    def log_message(self, *args):
        return None

    def do_GET(self):
        self.send_error(404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        self.server.requests.append(body)
        n = len(self.server.requests)
        if n <= len(self.server.script):
            item = {"type": "function_call", "id": f"fc_{n}", "call_id": f"call_{n}", "name": "exec_command",
                    "arguments": json.dumps({"cmd": self.server.script[n - 1]})}
        else:
            item = {"type": "message", "id": f"msg_{n}", "role": "assistant",
                    "content": [{"type": "output_text", "text": CLAIM, "annotations": []}]}
        usage = {"input_tokens": 1, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 1,
                 "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 2}
        events = [("response.created", {"response": {"id": f"resp_{n}"}}), ("response.output_item.done", {"item": item}),
                  ("response.completed", {"response": {"id": f"resp_{n}", "usage": usage}})]
        payload = "".join(f"event: {e}\ndata: {json.dumps({'type': e, **p})}\n\n" for e, p in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _log_records(log: str, since: int, cwd: str) -> list[dict]:
    if not os.path.isfile(log):
        return []
    with open(log, encoding="utf-8") as fh:
        lines = fh.readlines()[since:]
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if os.path.realpath(rec.get("cwd") or "") == os.path.realpath(cwd):
            out.append(rec)
    return out


def _count(log: str) -> int:
    if not os.path.isfile(log):
        return 0
    with open(log, encoding="utf-8") as fh:
        return sum(1 for _ in fh)


def _rules(records: list[dict], kind: str) -> set[str]:
    return {f["rule"] for r in records if r.get("kind") == kind for f in r.get("findings", [])}


def _hook_cmd(config_file: str | None, event: str) -> str:
    pinned = f" --config {config_file}" if config_file else ""
    return " ".join(shlex.quote(part) for part in self_command()) + f" hook {event}{pinned}"


def _isolated_env(home: Path, extra: dict) -> dict:
    keep = {k: os.environ[k] for k in ("PATH", "LANG", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "TERM") if k in os.environ}
    return {**keep, "HOME": str(home), "NO_PROXY": "127.0.0.1,localhost", "HTTPS_PROXY": "http://127.0.0.1:1",
            "HTTP_PROXY": "http://127.0.0.1:1", "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", **extra}


def prove_claude(probe: Path, cfg: dict, disposable: dict | None) -> dict:
    cli = shutil.which("claude")
    if not cli:
        return {"client": "claude", "status": "missing"}
    log = os.path.expanduser(cfg["log"])
    before = _count(log)
    with tempfile.TemporaryDirectory() as home, _Model(_Messages, [("Write", {"file_path": str(probe / PROBE_FILE), "content": PROBE_TEXT})]) as model:
        args = [cli, "-p", "Create the probe file.", "--permission-mode", "acceptEdits", "--no-session-persistence"]
        if disposable:
            settings = Path(home, "settings.json")
            settings.write_text(json.dumps({"hooks": {
                "PreToolUse": [{"matcher": "Edit|Write|MultiEdit|NotebookEdit|Bash", "hooks": [{"type": "command", "command": _hook_cmd(disposable["config"], "pre"), "timeout": 10}]}],
                "Stop": [{"hooks": [{"type": "command", "command": _hook_cmd(disposable["config"], "stop"), "timeout": 10}]}]}}))
            args += ["--settings", str(settings)]
        env = _isolated_env(Path(home), {"ANTHROPIC_API_KEY": "probe-not-a-key", "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{model.server_port}"})
        run = subprocess.run(args, cwd=probe, env=env, capture_output=True, text=True, timeout=180)
    time.sleep(0.2)
    records = _log_records(log, before, str(probe))
    pre, stop = _rules(records, "pre"), _rules(records, "stop")
    result = {"client": "claude", "exit": run.returncode, "pre_rules": sorted(pre), "stop_rules": sorted(stop),
              "file_written": (probe / PROBE_FILE).exists(), "model_requests": len(model.requests)}
    ok = "placeholder" in pre and "unbacked-claim" in stop
    if cfg.get("mode") == "enforce":
        ok = ok and not result["file_written"]
    result["status"] = "ok" if ok else "failed"
    (probe / PROBE_FILE).unlink(missing_ok=True)
    return result


def _codex_hooks_list(cli: str, codex_home: str, cwd: Path, env: dict) -> list[dict]:
    lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "agent-guard-prove", "version": "1"}}},
             {"jsonrpc": "2.0", "method": "initialized"}, {"jsonrpc": "2.0", "id": 2, "method": "hooks/list", "params": {}}]
    proc = subprocess.Popen([cli, "app-server"], cwd=cwd, env={**env, "CODEX_HOME": codex_home}, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        for line in lines:
            proc.stdin.write(json.dumps(line) + "\n")
        proc.stdin.flush()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            msg = json.loads(line)
            if msg.get("id") == 2:
                return [h for group in msg.get("result", {}).get("data", []) for h in group.get("hooks", [])]
        return []
    finally:
        proc.kill()
        proc.wait()


def prove_codex(probe: Path, cfg: dict, disposable: dict | None) -> dict:
    cli = shutil.which("codex")
    if not cli:
        return {"client": "codex", "status": "missing"}
    log = os.path.expanduser(cfg["log"])
    before = _count(log)
    with tempfile.TemporaryDirectory() as home, _Model(_Responses, [PROBE_CMD]) as model:
        codex_home = Path(home, "codex")
        codex_home.mkdir()
        env = _isolated_env(Path(home), {"CODEX_HOME": str(codex_home), "PROBE_KEY": "probe-not-a-key"})
        extra = []
        if disposable:
            (codex_home / "hooks.json").write_text(json.dumps({"hooks": {
                "PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": _hook_cmd(disposable["config"], "pre"), "timeout": 10}]}],
                "Stop": [{"hooks": [{"type": "command", "command": _hook_cmd(disposable["config"], "stop"), "timeout": 10}]}]}}))
            extra = ["--dangerously-bypass-hook-trust"]  # disposable registrations are user hooks, never trusted
        listed = [h for h in _codex_hooks_list(cli, str(codex_home), probe, env) if "agent-guard" in (h.get("command") or "")]
        provider = f'model_providers.probe={{name="probe",base_url="http://127.0.0.1:{model.server_port}/v1",wire_api="responses",env_key="PROBE_KEY"}}'
        run = subprocess.run([cli, "exec", "--skip-git-repo-check", "--ephemeral", *extra, "-c", "model_provider=probe", "-c", provider,
                              "-c", "model=probe-model", "Run the tests."], cwd=probe, env=env, capture_output=True, text=True, timeout=180)
        outputs = [i for r in model.requests for i in r.get("input", []) if i.get("type") == "function_call_output"]
    time.sleep(0.2)
    records = _log_records(log, before, str(probe))
    pre, stop = _rules(records, "pre"), _rules(records, "stop")
    refused = any("agent-guard refused" in json.dumps(o) for o in outputs)
    result = {"client": "codex", "exit": run.returncode, "pre_rules": sorted(pre), "stop_rules": sorted(stop),
              "command_refused": refused, "hooks_listed": [{k: h.get(k) for k in ("eventName", "source", "isManaged", "enabled", "trustStatus")} for h in listed]}
    events = {h.get("eventName") for h in listed}
    activation = {"preToolUse", "stop"} <= events and all(h.get("enabled") for h in listed)
    if not disposable:
        # Installed hooks must be managed: user hooks are skipped until someone trusts them by hand.
        activation = activation and all(h.get("isManaged") and h.get("trustStatus") != "untrusted" for h in listed)
    ok = activation and "masked-exit" in pre and "unbacked-claim" in stop
    if cfg.get("mode") == "enforce":
        ok = ok and refused
    result["status"] = "ok" if ok else "failed"
    return result


def run(installed: bool, mode: str, clients: list[str], probe_parent: str | None, allow_missing: bool) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        disposable = None
        if installed:
            cfg = C.load()
        else:
            config_file = os.path.join(tmp, "policy.json")
            cfg = dict(C.DEFAULTS, mode=mode, log=os.path.join(tmp, "events.jsonl"), state_dir=os.path.join(tmp, "state"), rule_packs=[])
            with open(config_file, "w", encoding="utf-8") as fh:
                json.dump({k: v for k, v in cfg.items() if not k.startswith("_")}, fh)
            disposable = {"config": config_file}
        parent = probe_parent or (cfg.get("enforce_paths") or [None])[0] or tmp
        probe = Path(tempfile.mkdtemp(prefix=".agent-guard-probe-", dir=os.path.expanduser(parent)))
        try:
            subprocess.run(["git", "init", "-q", str(probe)], check=True)
            results = []
            for client in clients:
                results.append((prove_claude if client == "claude" else prove_codex)(probe, cfg, disposable))
        finally:
            shutil.rmtree(probe, ignore_errors=True)
    ok = all(r["status"] == "ok" or (allow_missing and r["status"] == "missing") for r in results)
    print(json.dumps({"mode": cfg.get("mode"), "installed": installed, "results": results, "passed": ok}, indent=1))
    return 0 if ok else 1
