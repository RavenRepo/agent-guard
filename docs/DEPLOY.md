# Deploying to agent hosts

## Before the first install

1. **Independent review** of this repository by a different model than the one that wrote it.
2. **Check the host.**
   - `/usr/bin/python3` (3.10+), `git` and `sudo` are needed.
   - The activation proof needs the clients the agents use (`claude`, `codex`) on the agent user's login `PATH`.
   - Guarded builds also need `bwrap`, a running systemd user manager for the agent user, and Node.
   - Check with `command -v bwrap node && systemctl --user is-system-running`, run as the agent user.
3. **Rehearse without touching the host.** As the agent user:

   ```sh
   bin/agent-guard prove --disposable warn      # each real client delivers both hooks; the shortcut is logged
   bin/agent-guard prove --disposable enforce   # the same, and the shortcut is blocked
   ```

   These register the hooks for that one run only, with a throwaway policy, against a scripted model on loopback.

## Install

```sh
sudo bash install/install.sh install --agent-user agent \
  --enforce-paths /srv/work/worktrees:/srv/work/repos
```

What it does, as one transaction:

1. **Stages and installs the package.** It copies the package into a root-owned staging directory, checks the copy, then installs it to `/usr/local/lib/agent-guard` by an atomic swap.
2. **Writes the policy.** `/etc/agent-guard/config.json` is root-owned. A new policy starts in `warn` mode. An existing policy keeps its mode unless `--mode` is given, and the summary prints the effective mode.
3. **Records the originals.** The first time it touches each registration file, it records the file's content, or the fact that it was absent, in `/var/lib/agent-guard/state.json`. A reinstall never overwrites that record.
4. **Registers system hooks** with atomic writes. Either both registrations are written or neither is.
   - **Claude Code:** `/etc/claude-code/managed-settings.json` (managed settings that users can't override).
   - **Codex:** `/etc/codex/hooks.json`. These are system hooks, which Codex marks as managed and trusts by policy. Hooks in a user's `~/.codex/hooks.json` wait for review in `/hooks` and are skipped until trusted, so they are not used.
   - **Pinned policy:** each registration names the policy explicitly (`hook --config /etc/agent-guard/config.json`), so nothing in an agent's environment can point the hook elsewhere.
5. **Proves activation** with `agent-guard prove --installed`, run as the agent user.
   - **Both clients:** each real client runs in a fresh profile against a scripted model, proposes a shortcut, and claims "All tests pass".
   - **Hooks fired:** the proof asserts that the PreToolUse and Stop hooks of both clients logged.
   - **Codex trust:** it also asserts that Codex lists the hooks as managed and enabled.
   - **On failure:** the registrations are rolled back, and the command exits non-zero.

The log is written to `/var/log/agent-guard/install-<time>.log`, which only administrators can read.

| Command | Effect |
|---|---|
| `sudo bash install/install.sh status` | Shows the registrations and the effective mode |
| `sudo bash install/install.sh undo` | Removes only agent-guard's entries. It deletes a registration file that agent-guard created once nothing else is left in it, and keeps other hooks. |
| `undo --purge` | Also removes the package, the policy and the state |

## Rollout

1. **Warn, for 2–3 days.**
   - Read `~agent/.local/state/agent-guard/events.jsonl`. Each line holds the rule, the file, the line and a hash of the text, never the text itself.
   - Run `agent-guard replay --since <date> --pattern <project>` to see real cases. It reports refused tool calls and rule hits separately, because one call can hit several rules.
2. **Tune.** Fix false positives in the rules, or add `allow` entries. An entry is ignored unless it has a rule, a path, a real calendar date for `until`, a reason and an approver.
3. **Enforce.** Before switching to `--mode enforce`, meet both of these:
   - **Runner parity:** the full check runs `agent-guard scan <base> [--role builder]`. It applies the same path policy as the hooks to added, modified, deleted and renamed files.
   - **Completion evidence:** the controller treats work as complete only when `agent-guard verdicts --session <id>` exits 0 and the runner's own result for the named check on the exact commit is green.
4. **Roles.** Assign builders by path in the policy: `"roles": [{"role": "builder", "paths": ["/srv/work/worktrees/*-builder-*"]}]`. Builders may not change tests. `AGENT_GUARD_ROLE=builder` at launch can add the role, but nothing can remove a role the policy assigns.
5. **Guarded builds.** For contract-sized tasks, the dispatcher starts `agent-guard run`, and landing requires `agent-guard verify` on the exact commit.

## Claims and completion

The Stop hook judges the final reply against what the transcript shows actually ran.

- **What backs a claim:**
  - A claim about tests needs a passing test run after the last code edit. A typecheck claim needs a typecheck, a lint claim a lint, and a "full check" claim the project's whole check.
  - Only exit statuses the client observed count. Text a command printed does not, because any command can print `rc=0`.
  - A check piped into `tail` without `pipefail` counts neither way.
- **Long checks:** a check that runs in the background (for example a 20-minute full check) is cited by the agent ("the log at X ends with check_rc=0"), not claimed. The controller verifies the citation.
- **Refusal limit:** an unbacked claim is refused up to `stop_blocks` times per episode (default 2). After that the turn may end, so the session never loops. The session's verdict is recorded as `unresolved`, and the user sees a message.
  - The verdict, not the reply, decides completion: `agent-guard verdicts` exits 1 while any verdict is unresolved.
  - A later stop that passes cleanly marks the verdict `resolved`.
