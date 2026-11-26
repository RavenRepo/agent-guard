"""Runner-side scans. scan_diff applies the same path policy and content rules as the hooks to every change in
BASE...HEAD: additions, modifications, deletions and both sides of renames. Shell and completion checks stay in the
hooks, where their events exist."""
from __future__ import annotations

import os
import re
import subprocess

from . import files as F
from . import rules as R


def _git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=True).stdout


def changes(base: str, repo: str) -> list[tuple[str, str, str | None]]:
    """[(status, path, old_path)] for BASE...HEAD, renames detected."""
    out = []
    fields = _git(repo, "diff", "--name-status", "-z", "-M", "--no-ext-diff", f"{base}...HEAD").split("\0")
    i = 0
    while i < len(fields) and fields[i]:
        status = fields[i]
        if status[0] in "RC":
            out.append((status[0], fields[i + 2], fields[i + 1]))
            i += 3
        else:
            out.append((status[0], fields[i + 1], None))
            i += 2
    return out


def hunks(base: str, repo: str) -> dict[str, dict]:
    diff = _git(repo, "diff", "--unified=0", "--no-color", "--no-ext-diff", "-M", f"{base}...HEAD")
    files: dict[str, dict] = {}
    cur = None
    new_ln = 0
    for raw in diff.splitlines():
        if raw.startswith("diff --git"):
            cur = None
            continue
        if raw.startswith("+++ "):
            path = raw[4:].strip()
            path = path[2:] if path.startswith("b/") else path
            cur = files.setdefault(path, {"added": [], "removed": []})
            continue
        if raw.startswith("--- "):
            continue
        m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", raw)
        if m:
            new_ln = int(m.group(1))
            continue
        if cur is None:
            continue
        if raw.startswith("+"):
            cur["added"].append((new_ln, raw[1:]))
            new_ln += 1
        elif raw.startswith("-"):
            cur["removed"].append(raw[1:])
    return files


def scan_diff(base: str, cfg: dict, rules: list[R.Rule], repo: str = ".") -> list[R.Finding]:
    repo = os.path.abspath(repo)
    findings: list[R.Finding] = []
    content = hunks(base, repo)
    for status, path, old_path in changes(base, repo):
        absolute = os.path.join(repo, path)
        if status == "R":
            # Both sides: moving a test away is removing it; moving a file into a guarded place is adding one.
            findings += R.path_policy(os.path.join(repo, old_path), cfg, "D", label=old_path, host=False)
            findings += R.path_policy(absolute, cfg, "A", label=path, host=False)
            if F.is_test(old_path) and not F.is_test(path):
                findings.append(R.Finding("test-removed", old_path, None, f"renamed to {path}", R.BUILTIN["test-removed"]))
        else:
            findings += R.path_policy(absolute, cfg, status, label=path, host=False)
        d = content.get(path)
        if d and status != "D":
            findings += R.test_shrink_findings(path, "\n".join(d["removed"]), "\n".join(t for _, t in d["added"]))
            findings += R.content_findings(path, d["added"], rules)
    # The deletion pass in path_policy already reports removed tests once; drop exact duplicates.
    seen, unique = set(), []
    for f in findings:
        key = (f.rule, f.path, f.line, f.text)
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


def scan_files(paths: list[str], rules: list[R.Rule]) -> list[R.Finding]:
    out: list[R.Finding] = []
    for root in paths:
        if os.path.isfile(root):
            walk = [(os.path.dirname(root) or ".", [], [os.path.basename(root)])]
        else:
            walk = os.walk(root)
        for d, dirs, names in walk:
            dirs[:] = [x for x in dirs if x not in (".git", "node_modules", "dist", ".turbo")]
            for n in names:
                p = os.path.join(d, n)
                text = F.read_file(p)
                out += R.content_findings(p, list(enumerate(text.splitlines(), 1)), rules)
    return out
