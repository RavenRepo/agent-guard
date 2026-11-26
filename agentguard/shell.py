"""Shell commands: pattern rules per command segment, plus built-in checks for masked exit codes,
shell writes to protected files, repository copies, and files written through heredocs or patches."""
from __future__ import annotations

import os
import re
import shlex

from . import files as F
from .rules import BUILTIN, Finding, Rule, file_change_findings

CHECK_WORD_RE = re.compile(r"^(?:test|check|lint|typecheck|type-check|build|vitest|tsc|e2e|verify|(?:test|check|lint|e2e|typecheck|build):[\w:-]+)$")
CHECK_PROGS = {"vitest", "jest", "tsc", "pytest", "eslint", "playwright", "mocha", "ruff", "mypy", "knip", "depcruise"}
WRAPPERS = {"sudo", "command", "env", "nohup", "time", "timeout", "nice", "stdbuf", "xargs"}
MASK_NEXT_RE = re.compile(r"^\s*(?:true|:|exit\s+0|echo\b|printf\b)")
HEREDOC_RE = re.compile(r"(?:cat|tee)(?:\s+-a)?\s*(?:>{1,2}\s*)?([^\s<>|;&]+)?\s*<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n(.*?)\n\s*\2\s*(?:\n|$)", re.S)
ANY_HEREDOC_RE = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?([^\n]*)\n.*?\n\s*\1\s*(?=\n|$)", re.S)
HEREDOC_TARGET_RE = re.compile(r">{1,2}\s*([^\s<>|;&]+)")
INLINE_WRITE_RE = re.compile(r"write_text|writeFile|\.write\(|open\([^)]*['\"][wa]")
ASSIGN_RE = re.compile(r"(?:^|[\s;&|(])([A-Za-z_]\w*)=([^\s;&|()'\"$`]+)")
MUTATORS = {"rm", "unlink", "shred", "truncate", "mv"}
WRITE_FLAGS_RE = re.compile(r"^-[a-zA-Z]*i")


def strip_quoted(cmd: str) -> str:
    return re.sub(r"'[^']*'|\"(?:\\.|[^\"\\])*\"", "''", cmd)


def strip_heredocs(cmd: str) -> str:
    """Keep each heredoc's opening line, drop its body (file content is checked separately)."""
    return ANY_HEREDOC_RE.sub(lambda m: "<<" + m.group(1) + m.group(2), cmd)


def split_ops(cmd: str) -> tuple[list[str], list[str]]:
    parts = re.split(r"(\|\||&&|;|\||\n)", cmd)
    return parts[0::2], parts[1::2]


def tokens(seg: str) -> list[str]:
    try:
        toks = shlex.split(seg, posix=True)
    except ValueError:
        toks = seg.split()
    while toks and re.match(r"^\w+=", toks[0]):
        toks = toks[1:]
    while toks and os.path.basename(toks[0]) in WRAPPERS:
        toks = toks[1:]
        while toks and (toks[0].startswith("-") or re.match(r"^\d+[smh]?$", toks[0]) or re.match(r"^\w+=", toks[0])):
            toks = toks[1:]
    return toks


PROG_TYPES = {"vitest": "tests", "jest": "tests", "mocha": "tests", "pytest": "tests", "playwright": "tests",
              "tsc": "typecheck", "mypy": "typecheck", "eslint": "lint", "ruff": "lint", "knip": "lint", "depcruise": "lint"}
WORD_TYPES = (("test", "tests"), ("e2e", "tests"), ("vitest", "tests"), ("jest", "tests"), ("typecheck", "typecheck"),
              ("type-check", "typecheck"), ("tsc", "typecheck"), ("lint", "lint"), ("build", "build"), ("check", "full"),
              ("verify", "full"))
CHECK_WRAPPERS: set[str] = set()  # programs that run another check (policy: check_wrappers)


def configure(cfg: dict) -> None:
    """Apply the policy's project-specific settings to the shell analysis."""
    CHECK_WRAPPERS.clear()
    CHECK_WRAPPERS.update(os.path.basename(w) for w in cfg.get("check_wrappers") or [] if isinstance(w, str))


def is_repo_source(arg: str, cwd: str) -> bool:
    """A copy source that is a repository: the working directory itself, or a directory with a real .git."""
    if arg in (".", "./", "..", "$PWD", "${PWD}"):
        return True
    path = F.resolve(arg, cwd)
    return os.path.isdir(path) and F.is_git_marker(os.path.join(path, ".git"))


def check_type(seg: str) -> str | None:
    """What kind of check one command segment runs: tests, typecheck, lint, build, full (the project's whole check), or None."""
    toks = tokens(seg)
    while toks and os.path.basename(toks[0]) in CHECK_WRAPPERS:
        toks = toks[1:]
        if not toks:
            return "full"
    if not toks or any(t in ("--help", "-h", "--version", "list", "--list") for t in toks[1:]):
        return None
    prog = os.path.basename(toks[0])
    if prog in PROG_TYPES:
        return PROG_TYPES[prog]
    if prog == "prettier":
        return "lint" if ("--check" in toks or "-c" in toks) else None
    if prog in ("pnpm", "npm", "yarn", "bun", "npx", "turbo", "make", "just"):
        if "prettier" in toks[1:8]:
            return "lint" if "--check" in toks else None
        for t in toks[1:8]:
            base = os.path.basename(t)
            if base in PROG_TYPES:
                return PROG_TYPES[base]
            if CHECK_WORD_RE.match(t):
                head = t.split(":", 1)[0]
                return next(kind for word, kind in WORD_TYPES if head == word)
        return None
    if prog in ("cargo", "go") and len(toks) > 1:
        return {"test": "tests", "clippy": "lint", "vet": "lint", "check": "typecheck", "build": "build"}.get(toks[1])
    if prog in ("bash", "sh") and len(toks) > 1:
        name = os.path.basename(toks[-1])
        if re.search(r"check|verify", name):
            return "full"
        if re.search(r"test", name):
            return "tests"
    return None


def is_check_segment(seg: str) -> bool:
    return check_type(seg) is not None


def check_types(cmd: str) -> set[str]:
    segs, _ = split_ops(strip_quoted(strip_heredocs(cmd)))
    return {t for t in (check_type(s) for s in segs) if t}


def is_check_command(cmd: str) -> bool:
    return bool(check_types(cmd))


def masked_exit(bare: str) -> bool:
    segs, ops = split_ops(bare)
    pipefail = "pipefail" in bare or "PIPESTATUS" in bare
    checks = [i for i, s in enumerate(segs) if is_check_segment(s)]
    if checks and re.search(r"\bset\s+\+e\b", bare):
        return True
    for i in checks:
        if i < len(ops):
            op, nxt = ops[i], segs[i + 1] if i + 1 < len(segs) else ""
            if op == "||" and MASK_NEXT_RE.match(nxt):
                return True
            if op == ";" and re.match(r"^\s*(?:true|exit\s+0)\s*$", nxt):
                return True
            if op == "|" and not pipefail:
                return True
    return False


def patch_findings(text: str, cwd: str, cfg: dict, rules: list[Rule]) -> list[Finding]:
    out: list[Finding] = []
    for f in F.parse_patch(text):
        p = F.resolve(f["path"], cwd)
        old = (f["old"] or F.read_file(p)) if f["deleting"] else f["old"]
        new = "" if f["deleting"] else f["new"]
        out += file_change_findings(p, old, new, F.added_lines(f["old"], f["new"]), cfg, rules, deleting=f["deleting"])
    return out


def path_findings(targets: list[str], seg: str, cwd: str, cfg: dict) -> list[Finding]:
    """Shell changes to guards (anywhere), gate files (in a repository) and committed tests.
    A test file the agent created itself and has not committed may be rewritten or removed."""
    out = []
    for t in targets:
        p = F.resolve(t, cwd)
        if F.is_guard_path(p, cfg):
            out.append(Finding("guard-tamper", p, None, seg.strip()[:200], BUILTIN["guard-tamper"]))
        elif not F.repo_root(p):
            continue
        elif F.is_gate_config(p) or (F.is_test(p) and F.is_tracked(p)):
            out.append(Finding("test-file-mutation", p, None, seg.strip()[:200], BUILTIN["test-file-mutation"]))
    return out


def shell_findings(cmd: str, cwd: str, cfg: dict, rules: list[Rule], builtins: bool = True) -> list[Finding]:
    out: list[Finding] = []
    shell_rules = [r for r in rules if r.kind == "shell"]

    def add(rule: str, text: str) -> None:
        out.append(Finding(rule, cwd, None, text.strip()[:200], BUILTIN[rule]))

    if builtins and "*** Begin Patch" in cmd:
        out += patch_findings(cmd[cmd.index("*** Begin Patch"):], cwd, cfg, rules)
        cmd = cmd[:cmd.index("*** Begin Patch")]

    env = dict(ASSIGN_RE.findall(strip_heredocs(cmd)))
    env.setdefault("HOME", F.HOME)

    def expand(t: str) -> str | None:
        t = re.sub(r"\$\{?([A-Za-z_]\w*)\}?", lambda m: env.get(m.group(1), "\0"), t)
        return None if "\0" in t or "$" in t else t

    if builtins:
        # Files written through heredocs get the same content rules as an edit.
        for m in HEREDOC_RE.finditer(cmd):
            target = m.group(1)
            if not target:
                t2 = HEREDOC_TARGET_RE.search(cmd[m.end(2):].split("\n", 1)[0])
                target = t2.group(1) if t2 else None
            target = expand(target) if target else None
            if target and target not in ("/dev/null", "-"):
                p = F.resolve(target, cwd)
                old, body = F.read_file(p), m.group(3)
                out += file_change_findings(p, old, body, F.added_lines(old, body), cfg, rules)

    shell = strip_heredocs(cmd)
    bare = strip_quoted(shell)
    bare_segs, _ = split_ops(bare)
    for r in shell_rules:
        hit = next((sg for sg in bare_segs if r.pattern.search(sg)), None)
        if hit:
            out.append(Finding(r.id, cwd, None, hit.strip()[:200], r.help, r.mode))
    if not builtins:
        return out
    if masked_exit(bare):
        add("masked-exit", shell)

    for seg in split_ops(shell)[0]:
        toks = [x for x in (expand(t) for t in tokens(seg)) if x is not None]
        if not toks:
            continue
        prog = os.path.basename(toks[0])
        args = [t for t in toks[1:] if not t.startswith("-")]
        flags = [t for t in toks[1:] if t.startswith("-")]
        targets: list[str] = []
        if prog in MUTATORS:
            targets = args
        elif prog == "git" and len(toks) > 1 and toks[1] in ("rm", "mv"):
            targets = args[1:]
        elif prog in ("sed", "perl") and any(WRITE_FLAGS_RE.match(t) for t in flags):
            targets = args[1:] if prog == "sed" else args
        elif prog in ("cp", "ln", "install") and args:
            targets = [args[-1]]
        elif prog == "tee":
            targets = args
        elif prog == "chmod" and args:
            targets = args[1:]
        targets += [x for x in (expand(t) for t in HEREDOC_TARGET_RE.findall(strip_quoted(seg))) if x]
        if prog == "cp" and len(args) >= 2 and targets:
            # A copied-in file gets the same content rules as an edit.
            src, dst = F.resolve(expand(args[-2]) or "", cwd), F.resolve(targets[0], cwd)
            if os.path.isdir(dst):
                dst = os.path.join(dst, os.path.basename(src))
            if os.path.isfile(src):
                old, new = F.read_file(dst), F.read_file(src)
                out += file_change_findings(dst, old, new, F.added_lines(old, new), cfg, rules)
        out += path_findings(targets, seg, cwd, cfg)
        if prog in ("python", "python3", "node", "bun", "deno", "perl", "ruby") and INLINE_WRITE_RE.search(seg):
            paths = sorted(set(re.findall(r"~?[\w./@-]*/[\w./@-]+|[\w@-][\w.@-]*\.[A-Za-z]{2,5}\b", seg)))
            out += path_findings(paths, seg, cwd, cfg)
        # A copy of the repository outside its worktree escapes the gates.
        if (prog == "cp" and any(re.match(r"^-[a-zA-Z]*[rRa]", f) for f in flags)) or prog == "rsync":
            if len(args) >= 2 and args[-1].startswith("/tmp") and \
                    any(is_repo_source(a, cwd) for a in args[:-1]):
                add("repo-copy", seg)
        if prog == "git" and len(toks) > 2 and (toks[1] == "clone" or toks[1:3] == ["worktree", "add"]):
            if any(a.startswith("/tmp") for a in args):
                add("repo-copy", seg)
    return out
