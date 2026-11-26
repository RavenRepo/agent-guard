## What changes and why

<!-- The problem, the change, and how a user notices it. Link the issue: Fixes #123 -->

## How it was verified

<!-- Commands you ran and their results. "tests/run.sh: N passed" is the minimum. -->

## Checklist

- [ ] `tests/run.sh` passes locally (rule self-test plus the full suite)
- [ ] A new or changed rule carries `refuse` and `allow` examples and a `source`, and `agent-guard rules check` passes
- [ ] A change to a built-in rule adds a test with the fault and its clean twin
- [ ] False-positive risk is stated (or a replay summary is attached for a new blocking rule)
- [ ] `CHANGELOG.md` has an entry under "Unreleased"
- [ ] No new runtime dependency (agent-guard uses the Python standard library only)
