"""The rule engine and rule packs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, ROOT)
BIN = os.path.join(ROOT, "bin", "agent-guard")

from agentguard import rules as R


def cli(*args, cfg=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_GUARD_")}
    env["AGENT_GUARD_CONFIG"] = cfg or "/nonexistent/agent-guard.json"
    return subprocess.run([sys.executable, BIN, *args], capture_output=True, text=True, env=env)


class Engine(unittest.TestCase):
    def test_core_pack_selftest(self):
        p = cli("selftest")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("OK", p.stdout)

    def test_every_core_rule_has_examples_and_provenance(self):
        with open(R.CORE_PACK) as fh:
            data = json.load(fh)
        for raw in data["rules"]:
            self.assertEqual(R.validate(raw), [], raw["id"])
            self.assertTrue(raw.get("source"), f"{raw['id']} needs a source: where it was found")

    def test_a_rule_that_cannot_fail_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack = os.path.join(tmp, "p.json")
            Path(pack).write_text(json.dumps({"rules": [{"id": "never-fires", "kind": "line", "pattern": "zzz-not-there", "help": "h",
                                  "examples": {"refuse": ["const a = 1;"], "allow": ["const b = 2;"]}}]}))
            p = cli("rules", "check", pack)
            self.assertEqual(p.returncode, 1)
            self.assertIn("expected refuse", p.stdout)

    def test_a_rule_without_examples_is_rejected(self):
        errs = R.validate({"id": "bare", "kind": "line", "pattern": "x", "help": "h"})
        self.assertTrue(any("examples" in e for e in errs))

    def test_packs_cannot_replace_core_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack = os.path.join(tmp, "p.json")
            Path(pack).write_text(json.dumps({"rules": [{"id": "type-escape", "kind": "line", "pattern": "x", "help": "h",
                                  "examples": {"refuse": ["x"], "allow": ["y"]}}]}))
            rules, errors = R.load({"rule_packs": [pack]})
            self.assertTrue(any("duplicate id" in e for e in errors))
            self.assertEqual(sum(r.id == "type-escape" for r in rules), 1)

    def test_calibration_on_this_repository_is_clean(self):
        # The product's own code must pass its own rules.
        p = cli("files", os.path.join(ROOT, "agentguard"), os.path.join(ROOT, "examples"), os.path.join(ROOT, "install"))
        self.assertEqual(p.returncode, 0, p.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
