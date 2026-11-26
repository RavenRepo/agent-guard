# Contributing to agent-guard

Thanks for helping. The most valuable contributions are a **shortcut no rule catches yet**, and a **false positive**, meaning a rule that refused honest work. Both make every user's agents more trustworthy.

## Ground rules

- **Standard library only.** agent-guard runs inside every agent session and on CI runners, so it takes no runtime dependencies.
- **A rule must prove it can fail.** Every pattern rule ships with `refuse` examples (the fault) and `allow` examples (the clean twin), and `agent-guard rules check` runs them. A built-in rule gets the same pair as tests.
- **Fail open, and say so.** A bug in the guard must never stop every agent. Errors are logged and the action proceeds. Don't add a code path that swallows an error silently; the guard's own rules apply to its code (`tests/test_rules.py`).
- **No inline escape hatches.** An override written in a comment is a comment an agent can write. Exceptions belong in the operator's policy file.

## Development setup

```sh
git clone https://github.com/RavenRepo/agent-guard.git
cd agent-guard
tests/run.sh                      # rule self-test plus every test
bin/agent-guard rules             # list the rules
```

**Optional pieces:**
- **Sandbox tests** need Linux with `bwrap`, a systemd user manager and Node.
- **Client tests** need the coding clients installed.

Without them, those tests skip; the rest run anywhere with Python 3.10+ and git.

## Adding or changing a rule

Follow [docs/ADDING-RULES.md](docs/ADDING-RULES.md):

1. Capture a real example and its clean twin.
2. Write the rule with `refuse`, `allow` and `source`.
3. Run `agent-guard rules check`.
4. Measure it with `agent-guard replay` if you have recorded sessions.

New rules land in `warn` mode unless the replay shows almost no false positives.

## Pull requests

- **Keep it small:** one logical change per pull request.
- **Write the description for a reviewer:** what changed, why, and how you verified it. The template asks for each.
- **Write commit messages as a human engineer would:** an imperative subject, and a body that says why.
- **Add a line under "Unreleased" in `CHANGELOG.md`.**
- **Merging:** a maintainer merges after review, and CI must be green.

## Reporting a bypass

A way to get around a rule is a security issue. Please follow [SECURITY.md](SECURITY.md), not a public issue.

## Code of conduct

By participating you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
