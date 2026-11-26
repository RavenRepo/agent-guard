#!/usr/bin/env python3
"""Build the classifier demo: a small Git repository with a known bug, and a guarded-build contract for it.
Usage: python3 make_fixture.py DIR   (prints the repository and contract paths)"""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
EVAL = ("import { readFileSync } from 'node:fs';\n"
        "import { classify } from './lib/classify.mjs';\n"
        "const messages = JSON.parse(readFileSync(0, 'utf8'));\n"
        "console.log(JSON.stringify(messages.map(classify)));")
FUNCTION = ["{node}", "--input-type=module", "--eval", EVAL]
CLI = ["/usr/bin/python3", "-I", "{checks}/report_cli.py", "{node}", "{src}"]


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def build(target: Path) -> tuple[Path, Path, str]:
    repo, contract = target / "repo", target / "contract.json"
    shutil.copytree(HERE / "repo-base", repo)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@invalid", "commit", "-q", "-m", "Classifier with the stack-frame bug")
    base = git(repo, "rev-parse", "HEAD")
    shutil.copy(HERE / "report_cli.py", target / "report_cli.py")
    spec = {
        "format": 2,
        "task": "Fix classify in lib/classify.mjs. Classify only the first line of a message. Recognise OOM as a whole word. "
                "Keep timeout, connection, out-of-memory and ordinary assertion results, and keep the report command working.",
        "base": base, "editable": ["lib/classify.mjs"], "context": ["bin/report.mjs"],
        "drivers": {"report_cli.py": hashlib.sha256((HERE / "report_cli.py").read_bytes()).hexdigest()},
        "cases": [
            {"name": "stack-frame", "argv": FUNCTION, "input": ["assertion failed\n  at waitForTimeout()"], "expect": ["assertion"]},
            {"name": "word-boundaries", "argv": FUNCTION, "input": ["module init boom", "OOM", "out of memory"],
             "expect": ["assertion", "oom", "oom"]},
            {"name": "mixed-errors", "argv": FUNCTION, "input": ["timed out", "ECONNREFUSED", "expected 2, got 1", "connection closed\nTimeout"],
             "expect": ["timeout", "connection", "assertion", "connection"]},
            {"name": "cli-regression", "argv": CLI, "input": ["expected 2\n at waitForTimeout()"],
             "expect": {"exit": 1, "classes": ["assertion"], "version": 2}},
            {"name": "cli-clean", "argv": CLI, "input": ["timed out"], "expect": {"exit": 0, "classes": ["timeout"], "version": 2}},
        ],
        "base_witness": {"case": "stack-frame", "observed": ["timeout"]},
        "plant": {"path": "lib/classify.mjs", "content": "export function classify() { return 'assertion'; }\n",
                  "case": "mixed-errors", "observed": ["assertion", "assertion", "assertion", "assertion"]},
    }
    contract.write_text(json.dumps(spec, indent=1) + "\n")
    return repo, contract, base


if __name__ == "__main__":
    repo, contract, base = build(Path(sys.argv[1]).resolve())
    print(json.dumps({"repo": str(repo), "contract": str(contract), "base": base}))
