# agent-guard

**Guardrails for AI coding agents.** agent-guard refuses an agent's shortcuts the moment it tries them: skipped tests, silenced type checkers, `TODO` instead of code, exit codes hidden behind `| tail`, and "all tests pass" with no passing run. It also gates completion claims on real checks, and proves behaviour for guarded builds. It works with **Claude Code** and **Codex** hooks, needs no dependencies, and runs on your machines.

[![CI](https://github.com/RavenRepo/agent-guard/actions/workflows/ci.yml/badge.svg)](https://github.com/RavenRepo/agent-guard/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)
![Dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen.svg)
![Status: alpha](https://img.shields.io/badge/status-alpha-orange.svg)
[![Sponsor](https://img.shields.io/badge/sponsor-%E2%9D%A4-db61a2.svg?logo=githubsponsors)](https://github.com/sponsors/RavenRepo)

```text
agent-guard refused this action:
- [masked-exit] /work/api 'pnpm -F @acme/api typecheck 2>&1 | tail -3'
  This hides the check's exit code. Use `set -o pipefail;` before piping, and never `|| true` a check.
```

## Why

Coding agents optimise for the check they can see. Under pressure they **game the test instead of fixing the code**, a failure mode called reward hacking or specification gaming:
- weakening an assertion, or skipping the failing test;
- casting to `any`, or adding `@ts-ignore`;
- swallowing the error;
- writing a check that always returns `true`;
- copying the repo to `/tmp` to test outside the gate;
- reporting success without having run anything.

Code review catches some of it, late. agent-guard catches it **before the edit or command happens**, tells the agent what to do instead, and keeps the evidence.

## What it does

| Layer | When it runs | What you get |
|---|---|---|
| **Hooks** | Before every file edit and shell command, and when the agent stops (Claude Code and Codex hook events) | Shortcuts are refused with a reason the agent reads. A success claim needs a passing run of the *matching* check after the last code edit; an unbacked claim is refused, then recorded as an unresolved verdict. |
| **Runner scan** | In CI or your full check, before merge | `agent-guard scan BASE` applies the same rules to the branch diff: additions, edits, deletions and renames. |
| **Guarded builds** | For tasks you define with a contract | The agent can only read granted files and submit edits. A change is accepted only if it reproduces the defect's fix in a sandbox, passes every behaviour case, and catches a planted bad implementation. The check is re-run on the exact commit before landing. |

## What it catches

There are 23 data-driven rules, plus 8 built-in rules; run `agent-guard rules` for the full list.

| Family | Rules |
|---|---|
| Silencing the checker | `suppress-checker` (`@ts-ignore`, `eslint-disable`, `# type: ignore`, `# noqa`…), `type-escape` (`as any`, `as unknown as`), `ts-expect-error`, `checker-weakened` (`--ignoreConfig`, `--passWithNoTests`, `vitest -u`) |
| Placeholders instead of code | `placeholder` (TODO, "for now", "in production this would…"), `placeholder-code` (`throw new Error('not implemented')`), `elision` (`// ... existing code ...`), `commented-out-code`, `shortcut-unreferenced` |
| Gaming tests | `skipped-test` (`.skip`, `.only`, `xit`, `@pytest.mark.skip`), `assertion-drop`, `test-removed`, `test-lock` (builders may not touch tests), `test-file-mutation` (rewriting committed tests from the shell) |
| Fake implementations | `vacuous-guard` (`verify…() { return true }`), `vacuous-gate-list` (`…_CHECKS = []`), `swallowed-error`, `test-env-branch` (`if (NODE_ENV === 'test')`), `sandbox-probe` |
| Getting around the gate | `masked-exit` (`\|\| true`, `\| tail` without `pipefail`), `hook-bypass` (`--no-verify`, `core.hooksPath`), `gate-config` (CI, lint, tsconfig and dependency files), `guard-tamper`, `force-push`, `repo-copy` |
| Claims | `unbacked-claim` ("all tests pass" with no passing test run since the last edit), `forbidden-claim` ("production-ready") |

**Design choices that keep the agent from gaming the guard itself:**
- **Rules check only lines the agent adds,** so existing code is never blocked.
- **No inline overrides.** An override written in a comment is just another comment an agent can write. Exceptions live in the operator's policy, each with an expiry date, a reason and an approver.
- **The policy belongs to the operator.** The system policy wins over anything in the agent's environment, and the hooks pin it.
- **Only real exit statuses count as evidence.** Printed text like `rc=0` does not.

## Quick start

```sh
git clone https://github.com/RavenRepo/agent-guard.git && cd agent-guard
tests/run.sh                                  # rule self-test plus the test suite
bin/agent-guard rules                         # what it refuses
echo '{"tool_name":"Bash","cwd":".","tool_input":{"command":"pnpm test || true"}}' | bin/agent-guard hook pre; echo "exit $?"
```

**See it work with your real clients,** without changing anything on the machine. Each client runs against a scripted model on loopback; no account and no network are used.

```sh
bin/agent-guard prove --disposable enforce
```

**Install as a package** (Python 3.10+):

```sh
pipx install git+https://github.com/RavenRepo/agent-guard.git
agent-guard selftest
```

## Set up the hooks

### One developer

**Claude Code:** add to `~/.claude/settings.json`.

```json
{
  "hooks": {
    "PreToolUse": [{"matcher": "Edit|Write|MultiEdit|NotebookEdit|Bash",
                    "hooks": [{"type": "command", "command": "agent-guard hook pre", "timeout": 10}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "agent-guard hook stop", "timeout": 10}]}]
  }
}
```

**Codex:** add the same `hooks` object, with `"matcher": "*"` for `PreToolUse`, to `~/.codex/hooks.json`. Then trust the hooks in `/hooks`. Codex skips user hooks until they are reviewed.

### A team host or a fleet of agents

This is the recommended setup. It is a root-owned install with system hooks (Claude Code managed settings, and `/etc/codex/hooks.json`, which Codex trusts by policy). It runs an activation proof with the real clients and rolls back if the proof fails.

```sh
sudo bash install/install.sh install --agent-user agent --enforce-paths /srv/work
```

Start in `warn` mode, read the event log, then switch to `enforce`. See [docs/DEPLOY.md](docs/DEPLOY.md).

### CI

Add the runner scan to your full check, so nothing that slipped past the hooks reaches `main`:

```yaml
- run: pipx run --spec git+https://github.com/RavenRepo/agent-guard.git agent-guard scan origin/main
```

## Grow the rules

A rule is data: a pattern, plus examples of the shortcut and of the honest code next to it. To add one:

1. Write the rule, with both kinds of example.
2. Run `agent-guard rules check pack.json` to prove it refuses the faults and allows the clean twins.
3. Run `agent-guard replay` to measure it against recorded sessions.
4. Ship it in `warn` mode, and promote it to blocking.

See [docs/ADDING-RULES.md](docs/ADDING-RULES.md).

## Documentation

| Guide | Covers |
|---|---|
| [docs/DEPLOY.md](docs/DEPLOY.md) | Install, activation proof, rollout from warn to enforce, completion verdicts, undo |
| [docs/ADDING-RULES.md](docs/ADDING-RULES.md) | Rule format, self-test, replay, exceptions |
| [docs/CONTRACTS.md](docs/CONTRACTS.md) | Guarded-build contracts, the sandbox, landing checks |
| [CHANGELOG.md](CHANGELOG.md) | Release history |

## FAQ

**Does it send anything anywhere?**
No. It makes no network calls. The event log holds rule ids, paths, line numbers and hashes, never your code.

**How much does it slow the agent down?**
One short-lived process per tool call: about 120 ms median on a laptop, mostly Python start-up.

**What if the guard itself has a bug?**
It fails open. The action proceeds and the error is logged, so a guard bug never stops every agent. The runner scan is the backstop.

**Can it judge code quality?**
No. It refuses named, mechanical shortcuts, and proves behaviour you specify. Design and readability still need review.

## Limits

- **The pattern rules are scanners, not parsers.** A determined agent can still hide a change; the runner scan and human review stay.
- **Guarded builds suit bounded tasks** (functions, scripts, CLIs). A probe gets no network, 384 MiB, one CPU and 6 seconds.
- **The Stop hook doesn't decide completion.** It records verdicts; your controller decides, using the verdict and the runner's own result.
- **Tested on Linux** with Claude Code 2.1.332 and Codex 0.168.0.

## Sponsor the project

agent-guard is free and open source. Sponsorship pays for the work that keeps it useful:
- growing and tuning the rule catalogue as new shortcuts turn up;
- re-testing the hooks against each new release of the supported clients;
- triaging false positives quickly.

If it keeps your agents honest, please consider [sponsoring on GitHub](https://github.com/sponsors/RavenRepo).

## Contributing and security

Contributions are welcome. False positives and shortcuts no rule catches help most. See [CONTRIBUTING.md](CONTRIBUTING.md). To report a way around the guard, follow [SECURITY.md](SECURITY.md).

## License

[Apache License 2.0](LICENSE)
