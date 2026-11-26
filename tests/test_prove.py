"""The activation proof with the real clients against scripted models on loopback (no account or network)."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin" / "agent-guard"
CLIENTS = [c for c in ("claude", "codex") if shutil.which(c)]


def prove(*args, config=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_GUARD_")}
    if config:
        env["AGENT_GUARD_CONFIG"] = config
    with tempfile.TemporaryDirectory() as parent:
        p = subprocess.run([sys.executable, str(BIN), "prove", *args, "--probe-parent", parent] + sum((["--client", c] for c in CLIENTS), []),
                           capture_output=True, text=True, env=env, timeout=900)
    return p.returncode, json.loads(p.stdout)


@unittest.skipUnless(CLIENTS, "needs Claude Code or Codex installed")
class Prove(unittest.TestCase):
    def test_warn_mode_logs_both_hooks(self):
        code, out = prove("--disposable", "warn")
        self.assertEqual(code, 0, out)
        for r in out["results"]:
            self.assertEqual(r["status"], "ok", r)

    def test_enforce_mode_blocks_the_shortcut(self):
        code, out = prove("--disposable", "enforce")
        self.assertEqual(code, 0, out)
        for r in out["results"]:
            self.assertTrue(r.get("command_refused") or r.get("file_written") is False, r)

    def test_unregistered_hooks_fail_the_installed_proof(self):
        # No system registrations on this machine: the proof must fail rather than report success.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp, "policy.json")
            cfg.write_text(json.dumps({"mode": "warn", "rule_packs": [], "log": str(Path(tmp, "events.jsonl")),
                                       "state_dir": str(Path(tmp, "state"))}))
            if Path("/etc/claude-code/managed-settings.json").exists() or Path("/etc/codex/hooks.json").exists():
                self.skipTest("system registrations exist on this machine")
            code, out = prove("--installed", config=str(cfg))
            self.assertEqual(code, 1, out)
            self.assertTrue(all(r["status"] == "failed" for r in out["results"]), out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
