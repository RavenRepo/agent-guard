"""Drive the installed Claude Code client against scripted Messages API responses on loopback.
Only a synthetic key and a fresh home reach the local fixture; no real model or account is used."""
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

from test_broker import FIXED, NODE, ROOT, git, make_fixture, sandbox_available


class ScriptedMessages(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if not self.path.split("?")[0].endswith("/messages"):
            self.send_error(404)
            return
        self.server.requests.append([t["name"] for t in body.get("tools", [])])
        index = len(self.server.requests) - 1
        if index < len(self.server.script):
            name, args = self.server.script[index]
            block, reason = {"type": "tool_use", "id": f"toolu_{index}", "name": name, "input": args}, "tool_use"
        else:
            block, reason = {"type": "text", "text": "Finished."}, "end_turn"
        message = {"type": "message", "id": f"msg_{index}", "role": "assistant", "model": body["model"],
                   "content": [block], "stop_reason": reason, "stop_sequence": None,
                   "usage": {"input_tokens": 10, "output_tokens": 10}}
        if body.get("stream"):
            start_block = {**block, **({"input": {}} if reason == "tool_use" else {"text": ""})}
            delta = ({"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
                     if reason == "tool_use" else {"type": "text_delta", "text": block["text"]})
            events = [("message_start", {"message": {**message, "content": [], "stop_reason": None}}),
                      ("content_block_start", {"index": 0, "content_block": start_block}),
                      ("content_block_delta", {"index": 0, "delta": delta}), ("content_block_stop", {"index": 0}),
                      ("message_delta", {"delta": {"stop_reason": reason, "stop_sequence": None},
                                         "usage": {"output_tokens": 10}}), ("message_stop", {})]
            payload = "".join("event: " + n + "\ndata: " + json.dumps({"type": n, **f}) + "\n\n" for n, f in events).encode()
            ctype = "text/event-stream"
        else:
            payload, ctype = json.dumps(message).encode(), "application/json"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def claude_binary():
    cli = os.environ.get("AGENT_GUARD_TEST_CLAUDE") or shutil.which("claude")
    if cli and "/shims/" in cli and shutil.which("mise"):
        cli = subprocess.check_output(["mise", "which", "claude"], text=True).strip()
    if not cli:
        return None
    helptext = subprocess.run([cli, "--help"], capture_output=True, text=True).stdout
    return cli if "--restricted" in helptext else None


@unittest.skipUnless(sandbox_available() and claude_binary(), "needs the sandbox and a Claude Code build with --restricted")
class LauncherTest(unittest.TestCase):
    def exercise(self, script, root, repo, policy):
        with http.server.ThreadingHTTPServer(("127.0.0.1", 0), ScriptedMessages) as server:
            server.script, server.requests = script, []
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                env = {k: os.environ[k] for k in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS") if k in os.environ}
                env.update(HOME=str(root), PATH=str(root / "bin") + ":" + os.defpath, ANTHROPIC_API_KEY="local-fixture",
                           ANTHROPIC_BASE_URL=f"http://127.0.0.1:{server.server_port}", NO_PROXY="127.0.0.1,localhost",
                           HTTPS_PROXY="http://127.0.0.1:1", HTTP_PROXY="http://127.0.0.1:1",
                           CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_AUTOUPDATER="1", PYTHONDONTWRITEBYTECODE="1",
                           AGENT_GUARD_CONFIG="/nonexistent/agent-guard.json")
                out = root / f"run-{len(script)}"
                process = subprocess.run([sys.executable, "-B", str(ROOT / "bin" / "agent-guard"), "run", "--repo", str(repo),
                                          "--contract", str(policy), "--node", NODE, "--out", str(out)],
                                         env=env, capture_output=True, text=True, timeout=120)
                return process, out, server.requests
            finally:
                server.shutdown()
                thread.join()

    def test_tool_surface_retry_and_empty_finish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bin").mkdir()
            (root / "bin/claude").symlink_to(Path(claude_binary()).resolve())
            repo, policy, base = make_fixture.build(root / "fixture")
            src = git(repo, "show", f"{base}:lib/classify.mjs").decode()
            start = src.index("export function classify(")
            good = {"path": "lib/classify.mjs", "before": src[start:src.index("\n}", start) + 2], "after": FIXED}
            script = [("mcp__guard__task", {}), ("mcp__guard__read", {"path": good["path"]}),
                      ("mcp__guard__submit", {"edits": [{**good, "path": ".github/workflows/ci.yml"}]}),
                      ("mcp__guard__submit", {"edits": [good]})]
            proc, out, requests = self.exercise(script, root, repo, policy)
            self.assertEqual(proc.returncode, 0, (proc.stdout, proc.stderr, requests))
            for names in requests:
                self.assertEqual(set(names), {"mcp__guard__task", "mcp__guard__read", "mcp__guard__submit"})
            events = [json.loads(line) for line in (out / "events.jsonl").read_text().splitlines()]
            self.assertEqual([e["decision"] for e in events], ["deny", "accept"])
            proc, out, _ = self.exercise([], root, repo, policy)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("without an accepted change", proc.stderr)
            self.assertFalse((out / "accepted").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
