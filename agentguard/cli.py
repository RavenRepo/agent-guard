"""agent-guard command line."""
from __future__ import annotations

import argparse
import collections
import datetime as _dt
import glob
import json
import os
import re
import sys

from . import VERSION
from . import config as C
from . import rules as R


def cmd_rules(args) -> int:
    cfg = C.load()
    rules, errors = R.load(cfg)
    if args.action == "list":
        for rid, why in sorted(R.BUILTIN.items()):
            print(f"{rid:24} built-in  block  {why}")
        for r in rules:
            print(f"{r.id:24} {r.kind:8}  {r.mode:5}  {r.help}  [{os.path.basename(r.pack)}]")
    elif args.action == "check":
        packs = args.packs or [R.CORE_PACK]
        failed = 0
        for path in packs:
            raws, errs = R.read_pack(path)
            for e in errs:
                print("ERROR", e)
                failed += 1
            built = []
            for raw in raws:
                e = R.validate(raw)
                if path != R.CORE_PACK and (raw.get("id") in R.BUILTIN or raw.get("id") in {r.id for r in rules if r.pack == R.CORE_PACK}):
                    e.append(f"{raw.get('id')}: duplicate of a core rule id")
                if e:
                    print("ERROR", *e, sep="\n  ")
                    failed += len(e)
                    continue
                built.append(R.build(raw, path))
            for f in R.selftest(built, cfg):
                print("FAIL", f)
                failed += 1
            print(f"{path}: {len(built)} rule(s), {'OK' if not failed else f'{failed} problem(s)'}")
        return 1 if failed else 0
    for e in errors:
        print("pack error:", e, file=sys.stderr)
    return 1 if errors else 0


def cmd_selftest(args) -> int:
    cfg = C.load()
    rules, errors = R.load(cfg)
    failures = errors + R.selftest(rules, cfg)
    for f in failures:
        print("FAIL", f)
    print(f"{len(rules)} pattern rules, {sum(len(r.examples.get('refuse', [])) + len(r.examples.get('allow', [])) for r in rules)} examples: "
          f"{'OK' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


def emit(findings) -> int:
    for f in findings:
        print(json.dumps({"rule": f.rule, "mode": f.mode, "path": f.path, "line": f.line, "text": f.text}))
    sys.stderr.write(f"{len(findings)} finding(s)\n")
    return 1 if any(f.mode == "block" for f in findings) else 0


def cmd_scan(args) -> int:
    from .hook import allowed
    from .scan import scan_diff
    cfg = C.load()
    if args.role:
        cfg["_role_override"] = args.role
    rules, _ = R.load(cfg)
    return emit([f for f in scan_diff(args.base, cfg, rules, args.repo) if not allowed(f, cfg)])


def cmd_verdicts(args) -> int:
    cfg = C.load()
    folder = os.path.join(os.path.expanduser(cfg.get("state_dir") or C.DEFAULTS["state_dir"]), "verdicts")
    unresolved = 0
    for path in sorted(glob.glob(os.path.join(folder, "*.json"))):
        with open(path, encoding="utf-8") as fh:
            v = json.load(fh)
        if args.session and v.get("session") != args.session:
            continue
        if v.get("status") == "unresolved":
            unresolved += 1
        if v.get("status") == "unresolved" or args.all:
            print(json.dumps(v))
    return 1 if unresolved else 0


def cmd_files(args) -> int:
    from .scan import scan_files
    cfg = C.load()
    rules, _ = R.load(cfg)
    return emit(scan_files(args.paths, rules))


def cmd_replay(args) -> int:
    """Replay recorded Claude Code tool calls and turn ends through the rules (read-only), to measure a rule before enforcing it."""
    from .claims import claim_findings, transcript_events
    from .hook import pre_findings
    from .shell import configure
    from . import files as F
    cfg = C.load()
    configure(cfg)
    if args.role:
        cfg["_role_override"] = args.role
    rules, _ = R.load(cfg)
    if args.rule:
        rules = [r for r in rules if r.id in args.rule]
    F.read_file = lambda p: ""  # judge each Write as new content: the files on disk have moved on since
    since = args.since
    calls, hits, claims, ex = collections.Counter(), collections.Counter(), collections.Counter(), collections.defaultdict(list)
    refused_calls = refused_turns = turns = 0
    redact = lambda t: re.sub(r"[A-Za-z0-9_\-]{32,}", "[redacted]", t)[:120]
    files = [f for f in glob.glob(os.path.join(os.path.expanduser(args.projects), f"*{args.pattern}*", "**", "*.jsonl"), recursive=True)
             if os.path.getmtime(f) >= _dt.datetime.fromisoformat(since).timestamp()]
    for f in files:
        for line in open(f, errors="replace"):
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if o.get("timestamp", "") < since:
                continue
            m = o.get("message") or {}
            if m.get("role") != "assistant" or not isinstance(m.get("content"), list):
                continue
            for it in m["content"]:
                if not isinstance(it, dict) or it.get("type") != "tool_use" or it.get("name") not in ("Edit", "Write", "MultiEdit", "Bash"):
                    continue
                calls[it["name"]] += 1
                fs = pre_findings({"tool_name": it["name"], "tool_input": it.get("input") or {}, "cwd": o.get("cwd") or "/"}, cfg, rules)
                refused_calls += bool(fs)
                for rid in sorted({x.rule for x in fs}):
                    hits[rid] += 1
                    if len(ex[rid]) < args.examples:
                        x = next(y for y in fs if y.rule == rid)
                        ex[rid].append(f"{it['name']}: {os.path.basename(x.path or '')}:{x.line or ''} {redact(x.text)}")
        evs = transcript_events(f)
        for i, (k, d) in enumerate(evs):
            if k == "assistant_text" and (i + 1 == len(evs) or evs[i + 1][0] == "user_text") and d.get("ts", "") >= since:
                turns += 1
                found = claim_findings(d.get("text", ""), evs[:i + 1], rules)
                refused_turns += bool(found)
                for x in found:
                    claims[x.rule] += 1
                    if len(ex[x.rule]) < args.examples:
                        ex[x.rule].append(redact(x.text))
    print(json.dumps({"host": os.uname().nodename, "since": since, "role": cfg.get("_role_override"),
                      "transcripts": len(files), "tool_calls": dict(calls), "tool_calls_total": sum(calls.values()),
                      "refused_tool_calls": refused_calls, "rule_hits_by_rule": dict(hits.most_common()),
                      "turn_ends": turns, "refused_turn_ends": refused_turns, "claim_hits_by_rule": dict(claims),
                      "limits": ["a call can hit several rules: rule hits exceed refused calls",
                                 "writes are judged as new files (history on disk has moved on)",
                                 "tool calls and turn ends are both filtered to the --since window"],
                      "examples": ex}, indent=1))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="agent-guard", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("hook", help="harness hook: reads the event on stdin")
    h.add_argument("event", choices=["pre", "stop"])
    h.add_argument("--config", help="the policy file (registrations pin it; it overrides every other source)")
    rl = sub.add_parser("rules", help="list the rules, or check rule packs")
    rl.add_argument("action", choices=["list", "check"], nargs="?", default="list")
    rl.add_argument("packs", nargs="*")
    sub.add_parser("selftest", help="every pattern rule refuses its faults and allows its clean twins")
    s = sub.add_parser("scan", help="the content rules over git diff BASE...HEAD")
    s.add_argument("base", nargs="?", default="origin/main")
    s.add_argument("--repo", default=".")
    s.add_argument("--role", choices=["builder"], help="apply a role's file policy (builders may not change tests)")
    v = sub.add_parser("verdicts", help="completion verdicts recorded by the Stop hook; exit 1 if any is unresolved")
    v.add_argument("--session")
    v.add_argument("--all", action="store_true", help="also list resolved verdicts")
    fl = sub.add_parser("files", help="the content rules over whole files (calibration)")
    fl.add_argument("paths", nargs="+")
    rp = sub.add_parser("replay", help="replay recorded agent sessions through the rules (read-only)")
    rp.add_argument("--since", default=(_dt.date.today() - _dt.timedelta(days=7)).isoformat())
    rp.add_argument("--pattern", default="")
    rp.add_argument("--projects", default="~/.claude/projects")
    rp.add_argument("--rule", action="append")
    rp.add_argument("--role", choices=["builder"])
    rp.add_argument("--examples", type=int, default=3)
    for mode in ("serve", "run", "verify"):
        cmd = sub.add_parser(mode, help={"serve": "guarded-build tools over MCP stdio", "run": "launch a guarded builder",
                                         "verify": "re-check an accepted change on the landing commit"}[mode])
        cmd.add_argument("--repo", required=True)
        cmd.add_argument("--contract", required=True)
        cmd.add_argument("--node", help="the Node executable to mount in the sandbox")
        if mode == "verify":
            cmd.add_argument("--bundle", required=True)
            cmd.add_argument("--commit", required=True)
        else:
            cmd.add_argument("--out", required=True, help="a new operator-owned run directory")
        if mode == "serve":
            cmd.add_argument("--run-id", help=argparse.SUPPRESS)
    pv = sub.add_parser("prove", help="make the real Claude Code and Codex clients deliver events to the guard")
    where = pv.add_mutually_exclusive_group(required=True)
    where.add_argument("--installed", action="store_true", help="use the system registrations and policy")
    where.add_argument("--disposable", choices=["warn", "enforce"], help="register for this run only, with a throwaway policy")
    pv.add_argument("--client", action="append", choices=["claude", "codex"])
    pv.add_argument("--probe-parent", help="where to create the probe repository (default: the first enforce path)")
    pv.add_argument("--allow-missing", action="store_true", help="pass when a client is not installed")
    sub.add_parser("version")
    args = p.parse_args(argv)
    if args.cmd == "hook":
        from .hook import run
        return run(args.event, args.config)
    if args.cmd == "prove":
        from .prove import run as prove
        return prove(args.installed, args.disposable or "warn", args.client or ["claude", "codex"], args.probe_parent, args.allow_missing)
    if args.cmd == "version":
        print(VERSION)
        return 0
    if args.cmd in ("serve", "run", "verify"):
        from . import mcp
        try:
            return {"serve": mcp.serve_main, "run": mcp.launch, "verify": mcp.verify}[args.cmd](args)
        except mcp.ERRORS as error:
            print(f"agent-guard stopped: {error}", file=sys.stderr)
            return 1
    return {"rules": cmd_rules, "selftest": cmd_selftest, "scan": cmd_scan, "files": cmd_files, "replay": cmd_replay,
            "verdicts": cmd_verdicts}[args.cmd](args)
