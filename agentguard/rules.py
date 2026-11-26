"""The rule engine. Pattern rules live in JSON rule packs, each with examples that prove it can fail;
rules that need code (test shrinkage, protected paths, masked exits, claims backed by evidence) are built in."""
from __future__ import annotations

import glob
import json
import os
import re
from dataclasses import dataclass, field

from . import config as C
from . import files as F

CORE_PACK = os.path.join(os.path.dirname(os.path.realpath(__file__)), "rulepacks", "core.json")
KINDS = {"line", "blob", "shell", "claim"}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}$")


@dataclass
class Finding:
    rule: str
    path: str
    line: int | None
    text: str
    why: str
    mode: str = "block"


@dataclass
class Rule:
    id: str
    kind: str
    pattern: re.Pattern
    help: str
    mode: str = "block"
    part: str = "all"
    exts: set = field(default_factory=lambda: set(F.CODE_EXT))
    files: str = "any"
    skip_block_comments: bool = False
    unless: re.Pattern | None = None
    unless_before: re.Pattern | None = None
    needs_evidence: bool = False
    examples: dict = field(default_factory=dict)
    source: str = ""
    pack: str = ""


# Built-in rules: id -> help. Listed by `agent-guard rules`; their tests live in tests/.
BUILTIN = {
    "assertion-drop": "This edit removes assertions from a test. Fix the code, not the test.",
    "test-removed": "This edit removes test cases. Fix the code, not the test.",
    "test-lock": "Your role is builder: tests are locked. Make the code pass them; report a wrong test to the coordinator.",
    "gate-config": "Checker, CI, hook and dependency files need the owner's approval. Ask instead of editing.",
    "guard-tamper": "Agents may not change or disable the guards and hook settings.",
    "masked-exit": "This hides the check's exit code. Use `set -o pipefail;` before piping, and never `|| true` a check.",
    "test-file-mutation": "Don't delete, move or rewrite committed tests or gate files from the shell.",
    "repo-copy": "Work in your worktree, not a copy under /tmp, so the gates see what you test.",
}


def _flags(s: str) -> int:
    return (re.I if "i" in s else 0) | (re.M if "m" in s else 0) | (re.S if "s" in s else 0)


def _exts(langs) -> set:
    out: set = set()
    for lang in langs or ["code"]:
        if lang.startswith("."):
            out.add(lang.lower())
        elif lang in F.LANGUAGES:
            out |= F.LANGUAGES[lang]
        else:
            raise ValueError(f"unknown language {lang!r}")
    return out


def validate(raw: dict) -> list[str]:
    errs = []
    rid = raw.get("id", "")
    if not isinstance(rid, str) or not ID_RE.match(rid):
        errs.append(f"bad id {rid!r}")
    if raw.get("kind") not in KINDS:
        errs.append(f"{rid}: kind must be one of {sorted(KINDS)}")
    for key in ("pattern", "help"):
        if not isinstance(raw.get(key), str) or not raw.get(key):
            errs.append(f"{rid}: {key} is required")
    for key in ("pattern", "unless", "unless_before"):
        if raw.get(key):
            try:
                re.compile(raw[key])
            except re.error as e:
                errs.append(f"{rid}: {key} does not compile: {e}")
    ex = raw.get("examples") or {}
    if not ex.get("refuse") or not ex.get("allow"):
        errs.append(f"{rid}: examples need at least one 'refuse' and one 'allow' case")
    if raw.get("mode", "block") not in ("block", "warn"):
        errs.append(f"{rid}: mode must be block or warn")
    if raw.get("files", "any") not in ("any", "test", "nontest"):
        errs.append(f"{rid}: files must be any, test or nontest")
    if raw.get("part", "all") not in ("all", "code", "comment"):
        errs.append(f"{rid}: part must be all, code or comment")
    try:
        _exts(raw.get("languages"))
    except ValueError as e:
        errs.append(f"{rid}: {e}")
    unknown = set(raw) - {"id", "kind", "pattern", "flags", "help", "mode", "part", "languages", "files",
                          "skip_block_comments", "unless", "unless_before", "needs_evidence", "examples", "source"}
    if unknown:
        errs.append(f"{rid}: unknown fields {sorted(unknown)}")
    return errs


def build(raw: dict, pack: str) -> Rule:
    fl = _flags(raw.get("flags", ""))
    return Rule(id=raw["id"], kind=raw["kind"], pattern=re.compile(raw["pattern"], fl), help=raw["help"],
                mode=raw.get("mode", "block"), part=raw.get("part", "all"), exts=_exts(raw.get("languages")),
                files=raw.get("files", "any"), skip_block_comments=bool(raw.get("skip_block_comments")),
                unless=re.compile(raw["unless"], fl) if raw.get("unless") else None,
                unless_before=re.compile(raw["unless_before"], fl) if raw.get("unless_before") else None,
                needs_evidence=bool(raw.get("needs_evidence")), examples=raw.get("examples") or {},
                source=raw.get("source", ""), pack=pack)


def read_pack(path: str) -> tuple[list[dict], list[str]]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        return [], [f"{path}: {e}"]
    rules = data.get("rules") if isinstance(data, dict) else None
    if not isinstance(rules, list):
        return [], [f"{path}: a pack is an object with a 'rules' list"]
    return rules, []


def pack_files(cfg: dict) -> list[str]:
    out = []
    for p in cfg.get("rule_packs") or []:
        p = os.path.expanduser(p)
        if os.path.isdir(p):
            out += sorted(glob.glob(os.path.join(p, "*.json")))
        elif os.path.isfile(p):
            out.append(p)
    return out


_CACHE: dict = {}


def load(cfg: dict) -> tuple[list[Rule], list[str]]:
    """Core rules plus extra packs. Extra packs can only add rules; an invalid rule is skipped and reported."""
    key = tuple(pack_files(cfg))
    if key in _CACHE:
        return _CACHE[key]
    rules: list[Rule] = []
    errors: list[str] = []
    seen = set(BUILTIN)
    for path in [CORE_PACK, *key]:
        raws, errs = read_pack(path)
        errors += errs
        for raw in raws:
            e = validate(raw)
            if raw.get("id") in seen:
                e.append(f"{raw.get('id')}: duplicate id (packs can add rules, not replace them)")
            if e:
                errors += [f"{os.path.basename(path)}: {x}" for x in e]
                continue
            seen.add(raw["id"])
            rules.append(build(raw, path))
    _CACHE[key] = (rules, errors)
    return rules, errors


def help_for(rule_id: str, rules: list[Rule]) -> str:
    if rule_id in BUILTIN:
        return BUILTIN[rule_id]
    return next((r.help for r in rules if r.id == rule_id), "")


# --------------------------------------------------------------------------- content rules

def _applies(rule: Rule, path: str, ext: str, test: bool) -> bool:
    if ext not in rule.exts:
        return False
    return rule.files == "any" or (rule.files == "test") == test


def content_findings(path: str, added: list[tuple[int | None, str]], rules: list[Rule]) -> list[Finding]:
    """Line and blob rules over the lines an edit adds to one file."""
    ext = F.ext_of(path)
    if ext not in F.CODE_EXT or F.is_generated(path):
        return []
    test = F.is_test(path)
    active = [r for r in rules if r.kind in ("line", "blob") and _applies(r, path, ext, test)]
    if not active:
        return []
    out: list[Finding] = []
    for ln, text in added:
        code, comment = F.split_comment(text, ext)
        block_comment = text.lstrip().startswith(("/*", "*")) and ext in F.C_LIKE_EXT
        for r in active:
            if r.kind != "line":
                continue
            target = text if r.part == "all" else code if r.part == "code" else comment
            if target is None or (r.skip_block_comments and block_comment):
                continue
            if r.part == "comment":
                target = target.strip()
            if r.pattern.search(target) and not (r.unless and r.unless.search(target)):
                out.append(Finding(r.id, path, ln, text.strip()[:200], r.help, r.mode))
    blob = "\n".join(t for _, t in added)
    for r in active:
        if r.kind != "blob":
            continue
        for m in r.pattern.finditer(blob):
            if r.unless_before and r.unless_before.search(blob[max(0, m.start() - 160):m.start()]):
                continue
            if r.unless and r.unless.search(m.group(0)):
                continue
            idx = blob.count("\n", 0, m.start())
            out.append(Finding(r.id, path, added[idx][0] if idx < len(added) else None,
                               m.group(0).strip()[:200], r.help, r.mode))
    return out


ASSERT_RE = re.compile(r"\bexpect(?:Type(?:Of)?)?\s*\(|\bassert\w*\s*[(.]|\bassert\s+[^=\s]|\.should\b|\bt\.(?:Error|Fatal|Fail)f?\(")
TESTCASE_RE = re.compile(r"\b(?:it|test)(?:\.each\([^)]*\))?\s*\(\s*['\"`]|^\s*(?:async\s+)?def\s+test_\w+|\bfunc\s+Test\w+\(", re.M)


def test_shrink_findings(path: str, old: str, new: str) -> list[Finding]:
    if not F.is_test(path) or F.ext_of(path) not in F.CODE_EXT:
        return []
    out = []
    a_old, a_new = len(ASSERT_RE.findall(old)), len(ASSERT_RE.findall(new))
    t_old, t_new = len(TESTCASE_RE.findall(old)), len(TESTCASE_RE.findall(new))
    if a_new < a_old:
        out.append(Finding("assertion-drop", path, None, f"assertions {a_old} -> {a_new}", BUILTIN["assertion-drop"]))
    if t_new < t_old:
        out.append(Finding("test-removed", path, None, f"test cases {t_old} -> {t_new}", BUILTIN["test-removed"]))
    return out


def path_policy(path: str, cfg: dict, change: str = "M", label: str | None = None, host: bool = True) -> list[Finding]:
    """Rules about which files may change at all. The hook and the runner scan share this, so they agree.
    change is a git status letter: A (added), M (modified), D (deleted); renames are checked on both sides.
    host=False (the runner scan of repository-relative paths) checks repository-level guard files only."""
    shown = label or path
    if (host and F.is_guard_path(path, cfg)) or F.PROJECT_GUARD_RE.search(F.norm(path)):
        return [Finding("guard-tamper", shown, None, shown, BUILTIN["guard-tamper"])]
    out: list[Finding] = []
    if F.is_gate_config(path):
        out.append(Finding("gate-config", shown, None, shown, BUILTIN["gate-config"]))
    if F.is_test(path) and C.role_for(path, cfg) == "builder":
        out.append(Finding("test-lock", shown, None, shown, BUILTIN["test-lock"]))
    if change == "D" and F.is_test(path):
        out.append(Finding("test-removed", shown, None, "file deleted", BUILTIN["test-removed"]))
    return out


def file_change_findings(path: str, old: str, new: str, added: list[tuple[int | None, str]], cfg: dict,
                         rules: list[Rule], deleting: bool = False) -> list[Finding]:
    """Everything that applies to one file change: guards anywhere, product rules inside repositories only."""
    if F.is_guard_path(path, cfg):
        return [Finding("guard-tamper", path, None, path, BUILTIN["guard-tamper"])]
    if not F.repo_root(path):
        return []  # scratch files outside a repository are not product code
    out = path_policy(path, cfg, "D" if deleting else "M")
    out += test_shrink_findings(path, old, new)
    out += content_findings(path, added, rules)
    return out


# --------------------------------------------------------------------------- self-test

def selftest(rules: list[Rule], cfg: dict | None = None) -> list[str]:
    """Every pattern rule must refuse each 'refuse' example and pass each 'allow' example."""
    from .claims import claim_findings
    from .shell import shell_findings
    cfg = dict(cfg or {}, roles=[], _role_override=None)
    failures = []
    for r in rules:
        for expect, cases in (("refuse", r.examples.get("refuse", [])), ("allow", r.examples.get("allow", []))):
            for case in cases:
                if r.kind in ("line", "blob"):
                    path, text = (case["path"], case["text"]) if isinstance(case, dict) else ("src/example.ts", case)
                    hits = content_findings(path, list(enumerate(text.splitlines(), 1)), [r])
                elif r.kind == "shell":
                    hits = shell_findings(case, "/nonexistent-agent-guard-selftest", cfg, [r], builtins=False)
                else:
                    hits = claim_findings(case, [], [r], builtins=False)
                fired = any(h.rule == r.id for h in hits)
                if (expect == "refuse") != fired:
                    failures.append(f"{r.id}: expected {expect} for {str(case)[:90]!r}")
    return failures
