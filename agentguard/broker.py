"""Guarded builds. The operator writes a contract (what the change must do, which files may change,
behaviour cases, a base witness and a planted bad implementation). A builder can only submit edits;
the broker accepts a batch only when every case passes in a sandbox and the planted failure is caught."""
from __future__ import annotations

import hashlib
import fnmatch
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time
import uuid

from . import config as C
from . import files as F
from . import rules as R


class Denied(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise Denied(reason)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def strict_json(raw):
    def pairs(items):
        obj = dict(items)
        require(len(obj) == len(items), "duplicate JSON key")
        return obj

    value = json.loads(raw, object_pairs_hook=pairs)
    canonical(value)  # reject NaN and infinities
    return value


def safe_path(path):
    require(isinstance(path, str) and len(path) <= 500 and all(
        re.fullmatch(r"[A-Za-z0-9_@.-]+", part) and part not in (".", "..", ".git")
        for part in path.split("/")
    ), "invalid project path")
    return path


def check_write_grant(path):
    parts = path.lower().split("/")
    protected_dirs = {".github", ".gitlab", ".circleci", ".hooks", ".husky", "test", "tests",
                      "__tests__", "__snapshots__", ".claude", ".codex", "adr"}
    protected_files = ("*.spec.*", "*.test.*", "test_*.py", "*_test.py", "*_test.go", "conftest.py",
                       "*.snap", "package.json", "*lock.json", "*lock.yaml", "*.lock", "go.mod", "go.sum",
                       "pyproject.toml", "cargo.toml", "tsconfig*.json", "jsconfig*.json", "*.config.*",
                       ".eslint*", ".git*", "*ignore", ".prettier*", "lefthook*", "turbo.json", "agents.md")
    require(protected_dirs.isdisjoint(parts) and
            not any(fnmatch.fnmatchcase(parts[-1], pattern) for pattern in protected_files),
            f"protected build input: {path}")


def git(repo, *args, data=None, extra=None):
    env = {"PATH": os.defpath, "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_NO_REPLACE_OBJECTS": "1",
           "GIT_OPTIONAL_LOCKS": "0", **(extra or {})}
    return subprocess.run(["git", "-C", str(repo), *args], input=data, env=env,
                          capture_output=True, check=True, timeout=30).stdout


def screen(path, old, new, rules):
    """The shared content rules over the lines this submission adds. Any blocking finding denies the batch."""
    for f in R.content_findings(path, F.added_lines(old, new), rules):
        if f.mode == "block":
            raise Denied(f"{f.rule} at {path}:{f.line}: {f.why}")


def pinned_drivers(manifest, spec):
    drivers = {}
    for name, expected in spec.get("drivers", {}).items():
        require("/" not in safe_path(name), "driver must be a sibling of the contract")
        content = (Path(manifest).parent / name).read_bytes()
        require(digest(content) == expected, f"driver digest mismatch: {name}")
        drivers[name] = content
    return drivers


USERS = ("dev", "runner", "build", "app", "ci", "ubuntu")
WORKDIRS = ("app", "repo", "project", "work", "service", "src")


def sandbox_layout():
    """A fresh, ordinary-looking layout per contract, so code can't recognise the checks by fixed paths or a bare PATH."""
    user = secrets.choice(USERS)
    return {"user": user, "home": f"/home/{user}",
            "src": f"/home/{user}/{secrets.choice(WORKDIRS)}-{secrets.token_hex(3)}",
            "checks": f"/opt/{secrets.choice(('tools', 'support', 'harness', 'scripts'))}-{secrets.token_hex(3)}",
            "node": "/usr/local/bin/node"}


class Contract:
    FIELDS = {"task", "base", "editable", "context", "cases", "base_witness", "plant"}

    def __init__(self, repo, manifest, node=None, cfg=None):
        self.repo = Path(repo).resolve(strict=True)
        raw = Path(manifest).read_bytes()
        self.policy_hash = digest(raw)
        spec = strict_json(raw)
        require(set(spec) - {"drivers", "format"} == self.FIELDS, "invalid contract fields")
        require(spec.get("format", 2) == 2, "contract format 2 is required (argv uses {node}, {src}, {checks})")
        self.rules, _ = R.load(cfg or C.load())
        self.drivers = pinned_drivers(manifest, spec)
        self.task, self.base = spec["task"], spec["base"]
        require(isinstance(self.task, str) and self.task.strip(), "empty task")
        require(re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", self.base) and
                git(self.repo, "cat-file", "-t", self.base).strip() == b"commit", "pin a full base commit")
        self.editable = spec["editable"]
        context = spec["context"]
        require(isinstance(self.editable, list) and self.editable and isinstance(context, list), "empty scope")
        self.paths = [safe_path(path) for path in self.editable + context]
        require(len(set(self.paths)) == len(self.paths), "duplicate path")
        for path in self.editable:
            check_write_grant(path)
        self.sources, self.modes = self.read_tree(self.base)
        require(not any(str(parent) in self.sources for path in self.sources for parent in Path(path).parents),
                "overlapping source paths")
        self.cases = {}
        for case in spec["cases"]:
            require(set(case) == {"name", "argv", "input", "expect"}, "invalid case")
            name, argv = case["name"], case["argv"]
            require(isinstance(name, str) and name.strip() and name not in self.cases, "duplicate or empty case")
            require(isinstance(argv, list) and argv and all(isinstance(arg, str) for arg in argv), "invalid argv")
            self.cases[name] = case
        require(len({canonical(c["expect"]) for c in self.cases.values()}) >= 2,
                "provide contrasting observable outcomes")
        self.witness = spec["base_witness"]
        self.plant = spec["plant"]
        require(set(self.witness) == {"case", "observed"} and set(self.plant) ==
                {"path", "content", "case", "observed"}, "invalid controls")
        for control in (self.witness, self.plant):
            require(control["case"] in self.cases and canonical(control["observed"]) !=
                    canonical(self.cases[control["case"]]["expect"]), "control must show a specific wrong result")
        require(self.plant["path"] in self.editable and isinstance(self.plant["content"], str), "invalid plant")
        self.node = Path(node).resolve(strict=True) if node else None
        self.layout = sandbox_layout()

    def read_tree(self, revision):
        sources, modes = {}, {}
        for path in self.paths:
            entry = git(self.repo, "ls-tree", revision, "--", path).decode().rstrip("\n")
            if entry:
                meta, name = entry.split("\t")
                mode, kind, oid = meta.split()
                require(name == path and kind == "blob" and mode in ("100644", "100755"), "regular files only")
                require(int(git(self.repo, "cat-file", "-s", oid)) <= 262144, "source exceeds size limit")
                sources[path], modes[path] = git(self.repo, "cat-file", "blob", oid).decode(), mode
            else:
                require(path in self.editable, "missing context file")
                for parent in Path(path).parents:
                    if str(parent) != ".":
                        entry = git(self.repo, "ls-tree", revision, "--", str(parent))
                        require(not entry or entry.startswith(b"040000 tree "), "invalid parent")
                sources[path], modes[path] = None, "100644"
        return sources, modes

    def edits(self, changes):
        require(isinstance(changes, list) and 0 < len(changes) <= 100, "supply 1–100 edits")
        sources = dict(self.sources)
        for edit in changes:
            require(isinstance(edit, dict) and set(edit) == {"path", "before", "after"}, "invalid edit fields")
            path, old, new = edit["path"], edit["before"], edit["after"]
            require(isinstance(path, str) and path in self.editable, "path outside the write grant")
            require(isinstance(new, str) and "\x00" not in new, "expected UTF-8 source")
            if sources[path] is None:
                require(old is None, "creation requires before=null")
                sources[path] = new
            else:
                require(isinstance(old, str) and (old or sources[path] == "") and
                        sources[path].count(old) == 1, "before must match exactly once")
                sources[path] = sources[path].replace(old, new, 1)
            require(len(sources[path].encode()) <= 262144, "source exceeds size limit")
        require(sources != self.sources, "empty change")
        for path in self.editable:
            if self.sources[path] != sources[path]:
                screen(path, self.sources[path] or "", sources[path], self.rules)
        return sources

    def candidate(self, sources):
        with tempfile.TemporaryDirectory() as tmp:
            objects = Path(tmp, "objects")
            objects.mkdir()
            existing = git(self.repo, "rev-parse", "--path-format=absolute", "--git-path", "objects").decode().strip()
            env = {"GIT_INDEX_FILE": str(Path(tmp, "index")), "GIT_OBJECT_DIRECTORY": str(objects),
                   "GIT_ALTERNATE_OBJECT_DIRECTORIES": existing}
            git(self.repo, "read-tree", self.base, extra=env)
            for path in self.editable:
                if sources[path] != self.sources[path]:
                    oid = git(self.repo, "hash-object", "-w", "--stdin", data=sources[path].encode(), extra=env)
                    git(self.repo, "update-index", "--add", "--cacheinfo", self.modes[path],
                        oid.decode().strip(), path, extra=env)
            tree = git(self.repo, "write-tree", extra=env).decode().strip()
            patch = git(self.repo, "diff", "--cached", "--binary", "--full-index", "--no-renames",
                        "--no-ext-diff", "--no-textconv", "--src-prefix=a/", "--dst-prefix=b/", self.base, extra=env)
            return tree, patch

    def argv(self, case):
        lay = self.layout
        return [a.replace("{node}", lay["node"]).replace("{src}", lay["src"]).replace("{checks}", lay["checks"])
                for a in case["argv"]]

    def observe(self, sources, case):
        lay = self.layout
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as drivers:
            for name, content in self.drivers.items():
                Path(drivers, name).write_bytes(content)
            for path, content in sources.items():
                if content is not None:
                    file = Path(tmp, path)
                    file.parent.mkdir(parents=True, exist_ok=True)
                    file.write_bytes(content.encode())
                    file.chmod(int(self.modes[path][-3:], 8))
            # The user service bounds the entire process tree, including grandchildren.
            cmd = ["systemd-run", "--user", "--wait", "--pipe", "--collect", "--quiet",
                   "-p", "TasksMax=40", "-p", "MemoryMax=384M", "-p", "MemorySwapMax=0",
                   "-p", "CPUQuota=100%", "-p", "RuntimeMaxSec=6", "-p", "TimeoutStopSec=1",
                   "-p", "LimitFSIZE=1048576", "-p", "NoNewPrivileges=yes", "--",
                   "/usr/bin/bwrap", "--unshare-user", "--unshare-all", "--disable-userns",
                   "--cap-drop", "ALL", "--die-with-parent", "--new-session", "--clearenv",
                   "--ro-bind", "/usr/bin", "/usr/bin", "--ro-bind", "/usr/lib", "/usr/lib",
                   "--ro-bind-try", "/usr/lib64", "/usr/lib64"]
            for path in ("bin", "lib", "lib64"):
                host = Path("/", path)
                if host.is_symlink():
                    cmd += ["--symlink", os.readlink(host), "/" + path]
                elif host.is_dir():
                    cmd += ["--ro-bind", str(host), str(host)]
            if self.node:
                cmd += ["--ro-bind", str(self.node), lay["node"]]
            cmd += ["--ro-bind", tmp, lay["src"], "--ro-bind", drivers, lay["checks"], "--proc", "/proc", "--dev", "/dev",
                    "--size", "8388608", "--tmpfs", "/tmp", "--chdir", lay["src"], "--remount-ro", "/",
                    "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin", "--setenv", "HOME", lay["home"],
                    "--setenv", "USER", lay["user"], "--setenv", "LANG", "C.UTF-8", "--setenv", "LC_ALL", "C",
                    "--", *self.argv(case)]
            start = time.monotonic()
            # Regular files keep untrusted stdout/stderr under the service file-size limit.
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                run = subprocess.run(cmd, input=canonical(case["input"]).encode(), stdout=stdout, stderr=stderr,
                                     timeout=15)
                stdout.seek(0)
                data = stdout.read(65537)
                require(run.returncode == 0 and 0 < len(data) <= 65536,
                        f"probe {case['name']} failed (exit {run.returncode}, {len(data)} output bytes)")
                try:
                    value = strict_json(data.decode("utf-8"))
                except (ValueError, RecursionError) as exc:
                    raise Denied(f"probe {case['name']} returned invalid JSON") from exc
            return value, {"case": case["name"], "output_sha256": digest(data),
                           "elapsed_ms": round((time.monotonic() - start) * 1000), "exit": run.returncode}

    def control(self, sources, control):
        case = self.cases[control["case"]]
        actual, evidence = self.observe(sources, case)
        require(canonical(actual) == canonical(control["observed"]), f"invalid failure control: {case['name']}")
        return evidence

    def check_candidate(self, sources):
        checks = []
        for name, case in self.cases.items():
            actual, record = self.observe(sources, case)
            require(canonical(actual) == canonical(case["expect"]), f"behavior mismatch: {name}")
            checks.append(record)
        broken = {**sources, self.plant["path"]: self.plant["content"]}
        return checks, self.control(broken, self.plant)


class Broker:
    def __init__(self, contract, output, run_id=None):
        self.contract = contract
        self.run_id = run_id or uuid.uuid4().hex
        self.output = Path(output)
        self.output.mkdir(mode=0o700, exist_ok=False)
        self.finished = False
        self.attempts = 0
        self.base_result = contract.control(contract.sources, contract.witness)

    def log(self, event):
        with (self.output / "events.jsonl").open("a") as log:
            print(canonical({"attempt": self.attempts, **event}), file=log)

    def submit(self, edits):
        require(not self.finished, "session already accepted")
        self.attempts += 1
        require(self.attempts <= 12, "attempt budget exhausted")
        contract = self.contract
        try:
            sources = contract.edits(edits)
            checks, plant_result = contract.check_candidate(sources)
            tree, patch = contract.candidate(sources)
            receipt = {"run_id": self.run_id, "base": contract.base, "tree": tree, "patch_sha256": digest(patch),
                       "contract_sha256": contract.policy_hash, "checks": checks,
                       "base_failure": self.base_result, "planted_failure": plant_result,
                       "attempt": self.attempts}
            # Publish both artifacts with one same-filesystem rename.
            with tempfile.TemporaryDirectory(dir=self.output) as temp:
                bundle = Path(temp, "bundle")
                bundle.mkdir()
                (bundle / "change.patch").write_bytes(patch)
                (bundle / "receipt.json").write_text(canonical(receipt) + "\n")
                bundle.rename(self.output / "accepted")
            self.finished = True
            self.log({"decision": "accept", "tree": tree})
            return {"decision": "accepted", "tree": tree}
        except Denied as exc:
            self.log({"decision": "deny", "reason": str(exc)})
            raise
