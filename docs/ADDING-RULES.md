# Adding a rule

Use this when a review, an incident or a replay finds a shortcut the rules don't catch.

## 1. Capture the finding

Write down three things:
- where it appeared;
- a real line (redacted);
- what the clean version of the same code looks like.

The clean twin matters as much as the fault, because it is what keeps the rule from refusing honest work.

## 2. Write the rule

A rule pack is a JSON file with a `rules` list.

- **Local or experimental rules** go in `/etc/agent-guard/rules.d/<name>.json` on the hosts. Packs can only add rules, never replace a core rule.
- **Rules every host should have** go in `agentguard/rulepacks/core.json`, through a reviewed change to this repository.

```json
{
  "rules": [
    {
      "id": "no-sleep-in-tests",
      "kind": "line",
      "part": "code",
      "files": "test",
      "pattern": "\\bsetTimeout\\(|\\bsleep\\(",
      "help": "Wait for the event, not for time.",
      "mode": "warn",
      "source": "flaky queue test found in a review, 2026-12-02",
      "examples": {
        "refuse": [{"path": "src/__tests__/a.spec.ts", "text": "await new Promise((r) => setTimeout(r, 500));"}],
        "allow":  [{"path": "src/__tests__/a.spec.ts", "text": "await vi.waitFor(() => expect(done).toBe(true));"}]
      }
    }
  ]
}
```

| Field | Values |
|---|---|
| `id` | Lowercase words joined with `-`. Unique across all packs. |
| `kind` | `line`: each added line. `blob`: the added text as one string, for multi-line patterns. `shell`: each segment of a shell command, with quoted strings and heredoc bodies removed. `claim`: the agent's final message. |
| `pattern`, `flags` | A Python regular expression. `flags` may contain `i`, `m` and `s`. |
| `part` (line) | `all`, `code` (the line without its comment), or `comment` (the comment text only) |
| `languages` | `js`, `ts`, `py`, `sh`, `go`, `rs`, `sql`, `code` (the default: all code), or extensions such as `.mjs` |
| `files` | `any`, `test` or `nontest` |
| `unless` | Skip when the matched line, comment or match also matches this pattern. Example: `shortcut:` notes that name a ticket. |
| `unless_before` (blob) | Skip when the text just before the match matches this pattern. Example: best-effort cleanup calls. |
| `skip_block_comments` | Ignore `/* … */` and JSDoc lines (for rules about commented-out code) |
| `needs_evidence` (claim) | Refuse only when no check passed after the last code edit |
| `mode` | `block` refuses; `warn` only logs. Start new rules in `warn`. |
| `help` | What the agent should do instead. It is shown to the agent on refusal. |
| `source` | Where the pattern was found (required in the core pack) |
| `examples` | At least one `refuse` and one `allow` case. Line, blob and claim cases are text or `{path, text}`; shell cases are commands. |

## 3. Prove it can fail

```sh
bin/agent-guard rules check /etc/agent-guard/rules.d/no-sleep.json
```

This validates the fields and runs the examples. A rule that doesn't refuse its own fault example, or that refuses its clean twin, is rejected.

## 4. Measure it on real work

On a host with agent transcripts, replay the recorded sessions through the rule. This is read-only:

```sh
bin/agent-guard replay --since 2026-11-20 --pattern myproject --rule no-sleep-in-tests
```

Read every example it prints:
- a true positive is the reason the rule exists;
- for a false positive, tighten the pattern, add an `unless`, or add the case to `allow`.

## 5. Ship in warn mode, then block

Deploy the pack with `"mode": "warn"`. After two or three days, read `events.jsonl` (`rule_mode: "warn"` lines). Then switch the rule to `block`.

**Some checks need code,** for example counting assertions or looking at git state. Add those to `agentguard/rules.py` or `shell.py` as built-ins, list them in `BUILTIN`, and add a fault and a clean case to `tests/`.

## Exceptions

**A legitimate case a rule refuses** gets an `allow` entry in `/etc/agent-guard/config.json`. Never use an inline marker.

```json
{"rule": "type-escape", "path": "*/src/db/driver-internals.ts", "until": "2027-01-31",
 "reason": "pg client internals have no public type", "approved_by": "maintainer"}
```
