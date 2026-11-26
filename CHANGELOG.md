# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/). Until 1.0, a minor version may change behaviour.

## [0.3.0] - Unreleased

### Added
- **Packaging.** `pyproject.toml` with an `agent-guard` console script and the core rule pack as package data. `python -m agentguard` also works.
- **Open-source project files.**
  - Licensing: Apache-2.0 `LICENSE` and `NOTICE`.
  - Community: `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md` (bypasses count as vulnerabilities) and `SUPPORT.md`.
  - GitHub templates: issue templates for false positives, missed shortcuts and bugs, a pull request template, and `CODEOWNERS`.
  - Automation: a CI workflow on Python 3.10 and 3.13 that also builds and installs the package. Draft pull requests skip it.
  - Dependency updates for the workflow's actions.
- **A `check_wrappers` policy setting** for project scripts that run another check (for example a lock or queue wrapper). The command they wrap decides what kind of check it is.

### Changed
- **Project-neutral defaults.**
  - No project-specific program names remain in the shell analysis.
  - `repo-copy` recognises a repository by a real `.git`, not by path names.
  - `shortcut-unreferenced` accepts any issue key (`ABC-123`), `#123`, a plan step or an ADR.
  - Rule sources describe the finding without naming the codebase it came from.
- **Child processes run the same copy of agent-guard,** whether it is a checkout, an installed tree or a package. This covers the MCP server for guarded builds and the hooks in disposable proofs.

### Removed
- The example contract that depended on a private repository. The self-contained classifier example remains.

## [0.2.0] - 2026-11-26

These changes answer an independent review of the hook prototype (request changes before any warn rollout). Each finding applied to this product as well.

- **Activation is proven with the real clients.**
  - `agent-guard prove` runs Claude Code and Codex in fresh profiles against scripted models on loopback, and asserts that both the PreToolUse and the Stop hook logged.
  - For Codex it also asserts that the hooks are listed as managed and enabled.
  - Codex hooks are now registered as system hooks (`/etc/codex/hooks.json`), which Codex trusts by policy. User hooks stay untrusted and are skipped.
  - `--disposable enforce` tests blocking without touching the host.
- **The installer is a transaction.** It is rewritten in Python. It installs from a root-owned staging copy and writes every file atomically; either both registrations are written or neither is.
  - **Rollback:** a failed proof rolls back the registrations.
  - **Originals:** each registration file's original content, or its absence, is recorded on first touch, and a reinstall never overwrites that record.
  - **Undo** removes only agent-guard's entries, and deletes a file it created once nothing else is left in it.
  - **Mode and log:** the summary prints the effective mode, and the log goes to `/var/log/agent-guard`, after the root check.
- **Runner parity.** `scan` applies the same path policy as the hooks to added, modified, deleted and renamed files: guard files, gate configuration, the builder test lock, and removed tests. `--role builder` applies a role.
- **Claims are matched to checks, and only observed exit statuses count.**
  - A tests claim needs a test run, a typecheck claim a typecheck, and so on. Log text no longer counts as evidence, and neither does a check piped into a filter without `pipefail`.
  - Unbacked claims are refused up to `stop_blocks` times; after that the session's verdict is recorded as unresolved, and `agent-guard verdicts` reports it to the controller.
  - On exit 0 the Stop hook prints JSON, as Codex requires.
- **The policy is validated.**
  - An `allow` entry needs a rule, a path, a real calendar date, a reason and an approver.
  - An invalid mode falls back to `enforce`.
  - Roles are assigned by path in the policy, and the environment can only add the builder role.
  - The system policy wins over `AGENT_GUARD_CONFIG`, and registrations pin it with `--config`.
- **Repository detection** requires a real `.git` (a directory with `HEAD`, or a `gitdir:` file). A stray `/tmp/.git` no longer turns scratch files into product code.
- **Replay** reports refused tool calls separately from rule hits, filters turn ends to the same window, and accepts `--role`.

## [0.1.0] - 2026-11-26

First release. It combines the two prototypes built on 2026-11-26.

**From the hook prototype**
- Hooks for Claude Code and Codex: refusals before each tool call, and a check of success claims when the agent stops.
- The runner-side diff scan.

**From the guarded-build prototype**
- The three-tool builder, sandboxed behaviour probes, failure controls, and the exact-commit landing check.

**Changes made while combining them**
- **One rule engine.** Its pattern rules live in JSON packs. Each rule carries fault and clean examples, and `selftest` and `rules check` run them. Extra packs can add rules, but can't replace core ones. Rules can run in `warn` mode.
- **Guarded builds use the full content rules,** not the earlier three-rule screen.
- **The sandbox-aware bypass from the prototype review is closed.**
  - The new `sandbox-probe` rule refuses checks on `PATH`, `HOME`, the working directory, `/.dockerenv` and fixed mount paths.
  - Each contract gets a randomised, ordinary-looking layout with a normal `PATH`. This is contract format 2, with `{src}`, `{checks}` and `{node}` placeholders.
- **`agent-guard replay`** measures a rule against recorded sessions before enforcing it.
- **A system install** with root-owned files, managed Claude settings, warn mode by default, a planted-fault proof and undo.
