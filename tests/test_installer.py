"""The installer as a transaction, under a test prefix: clean install, reinstall, undo and a failure part-way."""
import getpass
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "install" / "installer.py"
USER = getpass.getuser()


class Installer(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.claude = self.root / "etc/claude-code/managed-settings.json"
        self.codex = self.root / "etc/codex/hooks.json"
        self.config = self.root / "etc/agent-guard/config.json"

    def run_installer(self, *args):
        return subprocess.run([sys.executable, str(INSTALLER), *args, "--root", str(self.root), "--agent-user", USER, "--skip-prove"],
                              capture_output=True, text=True)

    def ours(self, path):
        data = json.loads(path.read_text())
        return {event: sum("agent-guard" in json.dumps(g) for g in groups) for event, groups in data.get("hooks", {}).items()}

    def test_clean_install_reinstall_and_undo(self):
        p = self.run_installer("install")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("effective mode: warn", p.stdout)
        self.assertEqual(self.ours(self.claude), {"PreToolUse": 1, "Stop": 1, "SubagentStop": 1})
        self.assertEqual(self.ours(self.codex), {"PreToolUse": 1, "Stop": 1})
        hook = json.loads(self.codex.read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertEqual(hook, "/usr/bin/python3 -I -B /usr/local/lib/agent-guard/bin/agent-guard hook --config /etc/agent-guard/config.json pre")
        self.assertTrue((self.root / "usr/local/lib/agent-guard/agentguard/rulepacks/core.json").is_file())
        self.assertTrue(list((self.root / "var/log/agent-guard").glob("install-*.log")))
        state = json.loads((self.root / "var/lib/agent-guard/state.json").read_text())
        self.assertEqual(state["originals"]["codex"], {"existed": False, "content": None})
        # A reinstall replaces the registrations, never duplicates them, and keeps the first record of the originals.
        self.assertEqual(self.run_installer("install").returncode, 0)
        self.assertEqual(self.ours(self.claude), {"PreToolUse": 1, "Stop": 1, "SubagentStop": 1})
        self.assertEqual(json.loads((self.root / "var/lib/agent-guard/state.json").read_text())["originals"], state["originals"])
        p = self.run_installer("undo")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(self.claude.exists())
        self.assertFalse(self.codex.exists())

    def test_undo_keeps_other_hooks(self):
        other = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/usr/local/bin/notify-done"}]}]}, "theme": "dark"}
        self.claude.parent.mkdir(parents=True)
        self.claude.write_text(json.dumps(other))
        self.assertEqual(self.run_installer("install").returncode, 0)
        self.assertEqual(self.ours(self.claude), {"PreToolUse": 1, "Stop": 1, "SubagentStop": 1})
        self.assertEqual(self.run_installer("undo").returncode, 0)
        self.assertEqual(json.loads(self.claude.read_text()), other)

    def test_existing_mode_is_kept_and_reported(self):
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps({"mode": "enforce", "enforce_paths": ["/w"]}))
        p = self.run_installer("install")
        self.assertIn("effective mode: enforce", p.stdout)
        p = self.run_installer("install", "--mode", "warn")
        self.assertIn("effective mode: warn", p.stdout)
        self.assertEqual(json.loads(self.config.read_text())["mode"], "warn")

    def test_a_failure_part_way_leaves_nothing_registered(self):
        (self.root / "etc").mkdir(parents=True)
        (self.root / "etc/codex").write_text("not a directory")  # the second registration cannot be written
        p = self.run_installer("install")
        self.assertNotEqual(p.returncode, 0)
        self.assertFalse(self.claude.exists(), "the first registration must be rolled back")


if __name__ == "__main__":
    unittest.main(verbosity=2)
