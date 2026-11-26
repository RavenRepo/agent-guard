"""The guarded builder's tool surface (MCP stdio), the restricted launcher, and the landing check."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

from . import self_command
from .broker import Broker, Contract, Denied, canonical, digest, git, require, strict_json

ERRORS = (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError, RecursionError)
VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


STRING = {"type": "string"}
EDIT = obj({"path": STRING, "before": {"type": ["string", "null"]}, "after": STRING})
TOOLS = [
    {"name": "task", "description": "Read the assignment and file grants.", "inputSchema": obj({})},
    {"name": "read", "description": "Read one file from the pinned base.", "inputSchema": obj({"path": STRING})},
    {"name": "submit", "description": "Submit one complete batch against the base. A denial discards every edit. "
     "Use an exact unique before string, or null to create a granted file. An acceptance closes the run.",
     "inputSchema": obj({"edits": {"type": "array", "items": EDIT, "minItems": 1, "maxItems": 100}})},
]


def call(broker, name, args):
    c = broker.contract
    if name == "task" and args == {}:
        return {"task": c.task, "base": c.base, "editable": c.editable,
                "context": sorted(set(c.sources) - set(c.editable))}
    if name == "read" and set(args) == {"path"}:
        path = args["path"]
        require(isinstance(path, str) and path in c.sources, "path outside the read grant")
        return {"path": path, "content": c.sources[path]}
    if name == "submit" and set(args) == {"edits"}:
        return broker.submit(args["edits"])
    raise Denied("unknown tool or invalid arguments")


def serve(broker):
    initialized = False
    stream = sys.stdin.buffer
    while line := stream.readline(2**20 + 1):
        if len(line) > 2**20 or not line.endswith(b"\n"):
            return 2
        reply = {"jsonrpc": "2.0", "id": None}
        try:
            req = strict_json(line)
            require(isinstance(req, dict) and req.get("jsonrpc") == "2.0", "invalid request")
            if "id" not in req:
                continue
            reply["id"] = req["id"]
            method = req["method"]
            params = req.get("params", {})
            if method == "initialize":
                require(not initialized, "already initialized")
                version = params["protocolVersion"]
                result = {"protocolVersion": version if version in VERSIONS else VERSIONS[0],
                          "capabilities": {"tools": {}}, "serverInfo": {"name": "agent-guard", "version": "1"}}
                initialized = True
            elif method == "ping":
                result = {}
            elif initialized and method == "tools/list":
                result = {"tools": TOOLS}
            elif initialized and method == "tools/call":
                try:
                    value = call(broker, params["name"], params.get("arguments", {}))
                    result = {"content": [{"type": "text", "text": canonical(value)}]}
                except ERRORS as exc:
                    result = {"isError": True, "content": [{"type": "text", "text": str(exc)[:400]}]}
            else:
                reply["error"] = {"code": -32601, "message": "method unavailable"}
            if "error" not in reply:
                reply["result"] = result
        except ERRORS:
            reply["error"] = {"code": -32600, "message": "invalid request"}
        print(canonical(reply), flush=True)
    return 0 if broker.finished else 1


def launch(args):
    repo = Path(args.repo).resolve(strict=True)
    policy = Path(args.contract).resolve(strict=True)
    out = Path(args.out).resolve()
    require(not out.exists(), "choose a new run directory")
    cli = shutil.which("claude")
    require(cli, "Claude Code is required for the run command")
    run_id = uuid.uuid4().hex
    server = [*self_command(), "serve", "--repo", str(repo), "--contract", str(policy), "--out", str(out), "--run-id", run_id]
    if args.node:
        server += ["--node", str(Path(args.node).resolve(strict=True))]
    config = {"mcpServers": {"guard": {"command": server[0], "args": server[1:]}}}
    names = [f"mcp__guard__{tool['name']}" for tool in TOOLS]
    prompt = ("Read the task using the guard. Read the granted source and submit a complete edit. "
              "Correct any rejected submission using the feedback. Finish once the guard accepts.")
    with tempfile.TemporaryDirectory() as cwd:
        run = subprocess.run([cli, "--print", "--restricted", "--tools", "", "--strict-mcp-config",
                              "--mcp-config", canonical(config), "--allowedTools", ",".join(names),
                              "--permission-mode", "dontAsk", "--disable-slash-commands",
                              "--no-chrome", "--no-session-persistence", "--setting-sources", "", "--", prompt],
                             env={**os.environ, "ENABLE_TOOL_SEARCH": "false", "ENABLE_CLAUDEAI_MCP_SERVERS": "false"},
                             cwd=cwd, timeout=900)
    require(run.returncode == 0, f"agent exited {run.returncode}")
    receipt = out / "accepted/receipt.json"
    require(receipt.is_file() and strict_json(receipt.read_bytes())["run_id"] == run_id,
            "agent stopped without an accepted change for this run")
    print("Guard accepted:", out / "accepted")
    return 0


def verify(args):
    # Receipts and policies come from operator-owned storage. A builder's copy cannot attest to its own work.
    bundle = Path(args.bundle)
    receipt = strict_json((bundle / "receipt.json").read_bytes())
    contract = Contract(args.repo, args.contract, args.node)
    require(contract.policy_hash == receipt["contract_sha256"] and contract.base == receipt["base"],
            "contract mismatch")
    require(digest((bundle / "change.patch").read_bytes()) == receipt["patch_sha256"], "patch mismatch")
    require(all(isinstance(x, str) and not x.startswith("-") for x in (args.commit, receipt["base"])),
            "invalid revision")
    head = git(args.repo, "rev-parse", "--verify", "--end-of-options", args.commit + "^{commit}").decode().strip()
    git(args.repo, "merge-base", "--is-ancestor", receipt["base"], head)
    tree = git(args.repo, "rev-parse", head + "^{tree}").decode().strip()
    require(tree == receipt["tree"], "commit tree differs from the tested tree")
    sources, _ = contract.read_tree(head)
    base = contract.control(contract.sources, contract.witness)
    checks, plant = contract.check_candidate(sources)
    print(canonical({"verified_commit": head, "tree": tree, "run_id": receipt["run_id"],
                     "contract_sha256": contract.policy_hash, "checks": checks,
                     "base_failure": base, "planted_failure": plant}))
    return 0


def serve_main(args):
    return serve(Broker(Contract(args.repo, args.contract, args.node), args.out, args.run_id))
