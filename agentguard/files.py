"""What kind of file a path is, and where repositories begin."""
from __future__ import annotations

import os
import re
import subprocess

from .config import HOME, SYSTEM_CONFIG, config_path

CODE_EXT = {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".py", ".go", ".rs",
            ".java", ".kt", ".swift", ".rb", ".php", ".cs", ".sql", ".sh", ".bash"}
JS_EXT = {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"}
TS_EXT = {".ts", ".tsx", ".mts", ".cts"}
HASH_COMMENT_EXT = {".py", ".rb", ".sh", ".bash"}
C_LIKE_EXT = JS_EXT | {".go", ".rs", ".java", ".kt", ".swift", ".cs", ".php"}
LANGUAGES = {"js": JS_EXT, "ts": TS_EXT, "py": {".py"}, "sh": {".sh", ".bash"}, "go": {".go"},
             "rs": {".rs"}, "sql": {".sql"}, "code": CODE_EXT}

TEST_RE = re.compile(r"(^|/)(__tests__|__mocks__|tests?|spec|e2e|fixtures?)/|\.(test|spec|e2e)\.[cm]?[jt]sx?$"
                     r"|(^|/)test_[^/]*\.py$|_test\.(py|go)$|(^|/)conftest\.py$")
GENERATED_RE = re.compile(r"\.gen\.|(^|/)(node_modules|dist|build|vendor|generated|__generated__|\.turbo)/"
                          r"|\.min\.js$|(^|/)(pnpm-lock\.yaml|package-lock\.json|yarn\.lock)$")
GATE_CONFIG_RES = [re.compile(p) for p in (
    r"(^|/)tsconfig[^/]*\.json$", r"(^|/)\.eslintrc[^/]*$", r"(^|/)eslint\.config\.[cm]?[jt]s$",
    r"(^|/)biome\.jsonc?$", r"(^|/)\.(eslint|prettier|stylelint)ignore$",
    r"(^|/)(vitest|jest|playwright|vite)\.(config|workspace)\.[cm]?[jt]s$",
    r"(^|/)\.dependency-cruiser\.[cm]?js$", r"(^|/)lefthook(-local)?\.ya?ml$", r"(^|/)\.(husky|githooks)/",
    r"(^|/)\.github/workflows/", r"(^|/)turbo\.json$", r"(^|/)sgconfig\.ya?ml$", r"(^|/)tools/ast-grep/",
    r"(^|/)knip\.(json|jsonc|[cm]?[jt]s)$", r"(^|/)stryker\.[^/]*$", r"(^|/)(codecov\.ya?ml|\.c8rc[^/]*|\.nycrc[^/]*)$",
    r"(^|/)(pytest\.ini|setup\.cfg|tox\.ini|\.coveragerc|mypy\.ini|\.?ruff\.toml|pyproject\.toml)$",
    r"(^|/)package\.json$", r"(^|/)pnpm-(lock|workspace)\.yaml$", r"(^|/)\.npmrc$", r"(^|/)renovate\.json$",
    r"(^|/)commitlint\.config\.[cm]?js$",
)]
PROJECT_GUARD_RE = re.compile(r"(^|/)\.claude/settings[^/]*\.json$|(^|/)\.codex/(hooks\.json|config\.toml)$"
                              r"|(^|/)\.config/agent-guard(/|$)|(^|/)agent-guard\.json$")
PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def ext_of(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def norm(path: str) -> str:
    return path.replace("\\", "/")


def is_test(path: str) -> bool:
    return bool(TEST_RE.search(norm(path)))


def is_generated(path: str) -> bool:
    return bool(GENERATED_RE.search(norm(path)))


def is_gate_config(path: str) -> bool:
    p = norm(path)
    return not GENERATED_RE.search(p) and any(r.search(p) for r in GATE_CONFIG_RES)


def guard_paths(cfg: dict) -> list[str]:
    paths = [PACKAGE_DIR, config_path(), os.path.dirname(SYSTEM_CONFIG), os.path.join(HOME, ".config", "agent-guard"),
             os.path.join(HOME, ".claude", "settings.json"), os.path.join(HOME, ".claude", "settings.local.json"),
             os.path.join(HOME, ".codex", "hooks.json"), os.path.join(HOME, ".codex", "config.toml"),
             "/etc/claude-code", "/etc/codex"]
    paths += [os.path.expanduser(p) for p in cfg.get("guard_paths", [])]
    return [os.path.realpath(p) for p in paths]


def is_guard_path(path: str, cfg: dict) -> bool:
    if not path:
        return False
    rp = os.path.realpath(os.path.expanduser(path))
    if PROJECT_GUARD_RE.search(norm(rp)):
        return True
    return any(rp == g or rp.startswith(g + os.sep) for g in guard_paths(cfg))


_REPO_CACHE: dict[str, str | None] = {}


def is_git_marker(dot_git: str) -> bool:
    """A real work tree marker: a .git directory with a HEAD, or a .git file pointing at a git dir.
    An empty or stray .git (as in /tmp/.git) does not make its directory a repository."""
    if os.path.isdir(dot_git):
        return os.path.isfile(os.path.join(dot_git, "HEAD"))
    if os.path.isfile(dot_git):
        try:
            with open(dot_git, encoding="utf-8", errors="replace") as fh:
                return fh.read(8) == "gitdir: "
        except OSError:
            return False
    return False


def repo_root(path: str) -> str | None:
    """The work tree holding path (a directory with .git), or None for scratch files outside any repository."""
    d = os.path.abspath(path) if os.path.isdir(path) else os.path.dirname(os.path.abspath(path))
    seen = []
    while True:
        if d in _REPO_CACHE:
            root = _REPO_CACHE[d]
            break
        seen.append(d)
        if is_git_marker(os.path.join(d, ".git")):
            root = d
            break
        parent = os.path.dirname(d)
        if parent == d:
            root = None
            break
        d = parent
    for x in seen:
        _REPO_CACHE[x] = root
    return root


def is_tracked(path: str) -> bool:
    root = repo_root(path)
    if not root or not os.path.exists(path):
        return False
    try:
        return subprocess.run(["git", "-C", root, "ls-files", "--error-unmatch", "--", os.path.relpath(path, root)],
                              capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return True


def resolve(path: str, cwd: str) -> str:
    path = os.path.expanduser(path)
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(cwd or os.getcwd(), path))


def read_file(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def added_lines(old: str, new: str) -> list[tuple[int | None, str]]:
    """Lines in new that old lacks (multiset difference), with new's line numbers."""
    pool: dict[str, int] = {}
    for ln in old.splitlines():
        pool[ln] = pool.get(ln, 0) + 1
    out = []
    for i, ln in enumerate(new.splitlines(), 1):
        if pool.get(ln):
            pool[ln] -= 1
        else:
            out.append((i, ln))
    return out


def split_comment(line: str, ext: str) -> tuple[str, str | None]:
    """Return (code part, comment text or None) for one line, skipping comment markers inside quotes."""
    stripped = line.lstrip()
    if ext in C_LIKE_EXT and stripped.startswith(("/*", "*")):
        return "", stripped.lstrip("/*").rstrip("*/")
    if ext in HASH_COMMENT_EXT and stripped.startswith("#!"):
        return line, None
    marker = "#" if ext in HASH_COMMENT_EXT else "--" if ext == ".sql" else "//" if ext in C_LIKE_EXT else None
    if marker is None:
        return line, None
    quote, i = None, 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
        elif line.startswith(marker, i) and not (marker == "//" and i > 0 and line[i - 1] == ":"):
            return line[:i], line[i + len(marker):]
        i += 1
    return line, None


def parse_patch(text: str) -> list[dict]:
    """Patch text (apply_patch format or unified diff) as [{path, old, new, deleting}]."""
    files: list[dict] = []
    cur = None
    for raw in text.splitlines():
        m = re.match(r"\*\*\* (Add|Update|Delete) File: (.+)$", raw)
        if m:
            cur = {"path": m.group(2).strip(), "old": [], "new": [], "deleting": m.group(1) == "Delete"}
            files.append(cur)
            continue
        m = re.match(r"\+\+\+ (?:b/)?(.+)$", raw)
        if m and not raw.startswith("++++"):
            cur = {"path": m.group(1).strip(), "old": [], "new": [], "deleting": m.group(1).strip() == "/dev/null"}
            files.append(cur)
            continue
        if cur is None or raw.startswith(("*** ", "--- ", "@@")):
            continue
        if raw.startswith("+"):
            cur["new"].append(raw[1:])
        elif raw.startswith("-"):
            cur["old"].append(raw[1:])
    return [{"path": f["path"], "old": "\n".join(f["old"]), "new": "\n".join(f["new"]), "deleting": f["deleting"]}
            for f in files]
