"""Configuration validation, repository detection, and parity between the hook and the runner scan."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
BIN = ROOT / "bin" / "agent-guard"

from agentguard import config as C
from agentguard import files as F


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_GUARD_")}
    env.update(extra)
    return env


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=T", "-c", "user.email=t@invalid", *args],
                          check=True, capture_output=True, text=True).stdout


class Config(unittest.TestCase):
    def test_incomplete_exceptions_are_ignored(self):
        cfg = dict(C.DEFAULTS, mode="loose", allow=[
            {"rule": "type-escape", "path": "*", "until": "2999-01-01", "reason": "r", "approved_by": "maintainer"},
            {"rule": "type-escape", "path": "*", "until": "2999-01-01", "reason": "r"},
            {"rule": "type-escape", "path": "*", "reason": "r", "approved_by": "maintainer"},
            {"rule": "type-escape", "path": "*", "until": "2026-02-30", "reason": "r", "approved_by": "maintainer"},
            {"rule": "type-escape", "path": "*", "until": "2999-01-01", "reason": " ", "approved_by": "maintainer"}])
        errors = C.validate(cfg)
        self.assertEqual(cfg["mode"], "enforce")
        self.assertEqual(len(cfg["allow"]), 1)
        self.assertEqual(len(errors), 5)

    def test_roles_are_assigned_by_path_and_the_environment_cannot_loosen_them(self):
        cfg = dict(C.DEFAULTS, roles=[{"role": "builder", "paths": ["/work/builders/*"]}])
        C.validate(cfg)
        old = os.environ.pop("AGENT_GUARD_ROLE", None)
        try:
            self.assertEqual(C.role_for("/work/builders/t-1/src/a.spec.ts", cfg), "builder")
            self.assertIsNone(C.role_for("/work/writers/t-2/src/a.spec.ts", cfg))
            os.environ["AGENT_GUARD_ROLE"] = "writer"
            self.assertEqual(C.role_for("/work/builders/t-1/src/a.spec.ts", cfg), "builder")
            os.environ["AGENT_GUARD_ROLE"] = "builder"
            self.assertEqual(C.role_for("/work/writers/t-2/src/a.spec.ts", cfg), "builder")
        finally:
            os.environ.pop("AGENT_GUARD_ROLE", None)
            if old is not None:
                os.environ["AGENT_GUARD_ROLE"] = old

    def test_an_explicit_config_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            pinned, other = Path(tmp, "pinned.json"), Path(tmp, "other.json")
            pinned.write_text(json.dumps({"mode": "enforce", "rule_packs": [], "log": str(Path(tmp, "log"))}))
            other.write_text(json.dumps({"mode": "warn", "rule_packs": []}))
            repo = Path(tmp, "repo")
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            event = {"tool_name": "Write", "cwd": str(repo), "tool_input": {"file_path": str(repo / "a.ts"), "content": "export const x = y as any;\n"}}
            p = subprocess.run([sys.executable, str(BIN), "hook", "pre", "--config", str(pinned)], input=json.dumps(event),
                               capture_output=True, text=True, env=clean_env(AGENT_GUARD_CONFIG=str(other)))
            self.assertEqual(p.returncode, 2, p.stderr)


class Repositories(unittest.TestCase):
    def test_a_stray_git_marker_is_not_a_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, ".git").mkdir()
            Path(tmp, "scratch").mkdir()
            F._REPO_CACHE.clear()
            self.assertIsNone(F.repo_root(str(Path(tmp, "scratch", "probe.mjs"))))
            subprocess.run(["git", "init", "-q", str(Path(tmp, "real"))], check=True)
            self.assertEqual(F.repo_root(str(Path(tmp, "real", "a.ts"))), str(Path(tmp, "real")))
            subprocess.run(["git", "-C", str(Path(tmp, "real")), "worktree", "add", "-q", "--orphan", "-b", "w", str(Path(tmp, "wt"))],
                           check=False, capture_output=True)
            if Path(tmp, "wt", ".git").is_file():
                self.assertEqual(F.repo_root(str(Path(tmp, "wt", "a.ts"))), str(Path(tmp, "wt")))


class ScanParity(unittest.TestCase):
    """What the hook refuses as an edit, the runner scan must report as a committed change."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.repo = self.tmp / "repo"
        (self.repo / "src" / "__tests__").mkdir(parents=True)
        (self.repo / "src" / "__tests__" / "a.spec.ts").write_text("it('a', () => {\n  expect(a()).toBe(1);\n});\n")
        (self.repo / "src" / "__tests__" / "b.spec.ts").write_text("it('b', () => {\n  expect(b()).toBe(1);\n});\n")
        (self.repo / "src" / "keep.ts").write_text("export const k = 1;\n")
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        self.base = git(self.repo, "rev-parse", "HEAD").strip()
        self.cfg = self.tmp / "cfg.json"
        self.cfg.write_text(json.dumps({"rule_packs": [], "log": str(self.tmp / "log")}))

    def hook(self, tool_input, tool="Write", role=None):
        env = clean_env(AGENT_GUARD_CONFIG=str(self.cfg), **({"AGENT_GUARD_ROLE": role} if role else {}))
        p = subprocess.run([sys.executable, str(BIN), "hook", "pre"], input=json.dumps({"tool_name": tool, "cwd": str(self.repo),
                           "tool_input": tool_input}), capture_output=True, text=True, env=env)
        return {line.split("]")[0].split("[")[1] for line in p.stderr.splitlines() if line.startswith("- [")}

    def scan(self, role=None):
        args = [sys.executable, str(BIN), "scan", self.base, "--repo", str(self.repo)] + (["--role", role] if role else [])
        p = subprocess.run(args, capture_output=True, text=True, env=clean_env(AGENT_GUARD_CONFIG=str(self.cfg)))
        return {json.loads(line)["rule"] for line in p.stdout.splitlines() if line.strip()}

    def test_builder_test_edit(self):
        # The reviewer's case: a harmless extra assertion in a builder's test file.
        spec = self.repo / "src" / "__tests__" / "a.spec.ts"
        new = "it('a', () => {\n  expect(a()).toBe(1);\n  expect(a2()).toBe(2);\n});\n"
        self.assertIn("test-lock", self.hook({"file_path": str(spec), "content": new}, role="builder"))
        spec.write_text(new)
        git(self.repo, "commit", "-qam", "edit test")
        self.assertIn("test-lock", self.scan(role="builder"))
        self.assertNotIn("test-lock", self.scan())

    def test_additions_deletions_renames_and_guards(self):
        self.assertIn("gate-config", self.hook({"file_path": str(self.repo / "tsconfig.json"), "content": "{}\n"}))
        self.assertIn("guard-tamper", self.hook({"file_path": str(self.repo / ".claude" / "settings.json"), "content": "{}\n"}))
        (self.repo / "tsconfig.json").write_text("{}\n")
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude" / "settings.json").write_text("{}\n")
        git(self.repo, "rm", "-q", "src/__tests__/b.spec.ts")
        git(self.repo, "mv", "src/__tests__/a.spec.ts", "src/a-notes.ts")
        (self.repo / "src" / "keep.ts").write_text("export const k = 1;\nexport const x = y as any;\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "changes")
        found = self.scan()
        for rule in ("gate-config", "guard-tamper", "test-removed", "type-escape"):
            self.assertIn(rule, found)


if __name__ == "__main__":
    unittest.main(verbosity=2)
