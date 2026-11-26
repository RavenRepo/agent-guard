"""Guarded builds against a self-contained fixture repository (examples/classifier)."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples" / "classifier"))

from agentguard.broker import Broker, Contract, Denied, git
import make_fixture

NODE = shutil.which("node") and subprocess.check_output(["node", "-p", "process.execPath"], text=True).strip()
FIXED = (ROOT / "examples" / "classifier" / "fixed-classify.txt").read_text().rstrip("\n")
CFG = {"rule_packs": []}


def sandbox_available():
    if not (NODE and shutil.which("bwrap") and shutil.which("systemd-run")):
        return False
    return subprocess.run(["systemd-run", "--user", "--wait", "--quiet", "--collect", "true"], capture_output=True).returncode == 0


@unittest.skipUnless(sandbox_available(), "needs node, bubblewrap and a systemd user manager")
class BrokerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.repo, self.policy, self.base = make_fixture.build(self.tmp / "fixture")
        self.out = self.tmp / "run"
        src = git(self.repo, "show", f"{self.base}:lib/classify.mjs").decode()
        start = src.index("export function classify(")
        self.before = src[start:src.index("\n}", start) + 2]
        self.good = {"path": "lib/classify.mjs", "before": self.before, "after": FIXED}

    def contract(self):
        return Contract(self.repo, self.policy, NODE, CFG)

    def reject(self, broker, edits, reason):
        with self.assertRaises(Denied) as ctx:
            broker.submit(edits)
        self.assertIn(reason, str(ctx.exception))
        self.assertFalse((self.out / "accepted").exists())

    def test_shortcuts_are_refused_and_the_real_fix_lands(self):
        broker = Broker(self.contract(), self.out)
        for path in ("lib/classify.test.mjs", ".github/workflows/ci.yml", "bin/report.mjs"):
            self.reject(broker, [self.good, {**self.good, "path": path}], "outside the write grant")
        for marker, rule in [("// TODO: fill this in later", "placeholder"), ("// @ts-ignore", "suppress-checker"),
                             ("test.skip('branch')", "skipped-test")]:
            self.reject(broker, [{**self.good, "after": marker + "\n" + FIXED}], rule)
        # A comment that claims the fix, a disabled branch, a constant answer, a replacement that breaks the command.
        self.reject(broker, [{**self.good, "after": "// Only the first line determines the result.\n" + self.before}], "stack-frame")
        self.reject(broker, [{**self.good, "after": FIXED.replace("  if (/timed?", "  if (false && /timed?")}], "mixed-errors")
        self.reject(broker, [{**self.good, "after": "export function classify() { return 'assertion'; }"}], "word-boundaries")
        whole = git(self.repo, "show", f"{self.base}:lib/classify.mjs").decode()
        self.reject(broker, [{"path": "lib/classify.mjs", "before": whole, "after": FIXED + "\n"}], "cli-")
        self.assertEqual(broker.submit([self.good])["decision"], "accepted")
        with self.assertRaisesRegex(Denied, "already accepted"):
            broker.submit([self.good])
        receipt = json.loads((self.out / "accepted/receipt.json").read_bytes())
        self.assertEqual(len(receipt["checks"]), 5)
        self.assertEqual(receipt["base_failure"]["case"], "stack-frame")
        self.assertEqual(receipt["planted_failure"]["case"], "mixed-errors")

        # Land the patch on a commit and verify it there.
        landing = self.tmp / "landing"
        subprocess.run(["git", "clone", "-q", "--shared", str(self.repo), str(landing)], check=True)
        subprocess.run(["git", "-C", str(landing), "apply", str(self.out / "accepted/change.patch")], check=True)
        subprocess.run(["git", "-C", str(landing), "-c", "user.name=T", "-c", "user.email=t@invalid", "commit", "-qam", "land"], check=True)
        head = git(landing, "rev-parse", "HEAD").decode().strip()

        def verify(commit):
            env = dict(os.environ, AGENT_GUARD_CONFIG="/nonexistent/agent-guard.json")
            return subprocess.run([sys.executable, str(ROOT / "bin" / "agent-guard"), "verify", "--repo", str(landing),
                                   "--contract", str(self.policy), "--node", NODE, "--bundle", str(self.out / "accepted"),
                                   "--commit", commit], capture_output=True, text=True, env=env)

        ok = verify(head)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout)["verified_commit"], head)
        self.assertIn("differs", verify(self.base).stderr)

    def test_sandbox_aware_code_is_refused(self):
        """Review finding F1: code that applies the fix only inside the checks must not be accepted."""
        broker = Broker(self.contract(), self.out)
        body = FIXED.split("\n", 2)[2]  # the rules after the firstLine line
        for probe in ("process.env.PATH === '/runtime:/usr/bin'", "existsSync('/checks')", "process.cwd() === '/src'"):
            cheat = (f"export function classify(message) {{\n  const probe = {probe};\n"
                     f"  const firstLine = probe ? String(message).split('\\n', 1)[0] : message;\n{body}")
            self.reject(broker, [{**self.good, "after": cheat}], "sandbox-probe")

    def test_the_layout_is_not_fixed(self):
        a, b = self.contract().layout, self.contract().layout
        self.assertNotEqual((a["src"], a["checks"]), (b["src"], b["checks"]))
        case = {"name": "where", "argv": ["/usr/bin/python3", "-I", "-c",
                "import os, json; print(json.dumps([os.getcwd().startswith('/home/'), os.environ.get('PATH'), "
                "os.path.exists('/checks'), os.path.exists('/runtime'), os.path.exists('/src')]))"], "input": None}
        observed, _ = self.contract().observe(self.contract().sources, case)
        self.assertEqual(observed, [True, "/usr/local/bin:/usr/bin:/bin", False, False, False])

    def test_isolation(self):
        contract = self.contract()
        case = {"name": "isolation", "argv": ["/usr/bin/python3", "-I", "-c",
                "import os, json, glob; from pathlib import Path; "
                "src = os.getcwd(); "
                "print(json.dumps([os.access(src + '/lib/classify.mjs', os.W_OK), Path(src, '.git').exists(), "
                "os.path.exists('/run/user'), dict(os.environ).get('GITHUB_TOKEN'), "
                "len(Path('/proc/net/route').read_text().splitlines())]))"], "input": None}
        observed, _ = contract.observe(contract.sources, case)
        self.assertEqual(observed, [False, False, False, None, 1])

    def test_controls_fail_closed(self):
        contract = self.contract()
        contract.witness = dict(contract.witness, observed=["unexpected"])
        with self.assertRaisesRegex(Denied, "invalid failure control"):
            Broker(contract, self.out)
        contract = self.contract()
        contract.plant["content"] = FIXED + "\nexport const REPORT_VERSION = 2;\n"
        broker = Broker(contract, self.tmp / "run2")
        with self.assertRaisesRegex(Denied, "invalid failure control"):
            broker.submit([self.good])
        with self.assertRaisesRegex(Denied, "probe stack-frame failed"):
            Broker(Contract(self.repo, self.policy, None, CFG), self.tmp / "run3")

    def test_contract_validation(self):
        spec = json.loads(self.policy.read_text())
        bad = self.tmp / "fixture" / "bad.json"
        for change, reason in [({"cases": []}, "contrasting"), ({"format": 1}, "format 2"),
                               ({"editable": ["lib/classify.mjs", "tsconfig.json"]}, "protected build input")]:
            bad.write_text(json.dumps({**spec, **change}))
            with self.assertRaisesRegex(Denied, reason):
                Contract(self.repo, bad, NODE, CFG)
        (self.tmp / "fixture" / "report_cli.py").write_text("print('pass')")
        with self.assertRaisesRegex(Denied, "driver digest mismatch"):
            Contract(self.repo, self.policy, NODE, CFG)


if __name__ == "__main__":
    unittest.main(verbosity=2)
