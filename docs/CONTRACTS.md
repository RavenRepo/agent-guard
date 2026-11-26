# Guarded-build contracts (format 2)

A guarded build turns "fix this" into a task with an observable outcome. Before the agent starts, the operator writes down what the change must do. The broker accepts a submission only when three things hold:
- the base reproduces the defect;
- every behaviour case passes on the candidate;
- a planted bad implementation is caught.

The agent gets three tools: `task`, `read` (granted files at the pinned base) and `submit` (a batch of exact-text edits). It has no shell, no other files, and no way to mark itself done. The launcher fails unless the broker issued an accepted receipt for this run.

## Fields

| Field | Meaning |
|---|---|
| `format` | `2` |
| `task` | The observable change, in words |
| `base` | The full commit SHA to build on |
| `editable` | Exact production files the agent may change. Tests, CI, configuration, ADRs, lockfiles and hook settings are refused even if listed. |
| `context` | Read-only files the agent may read and the probes need |
| `drivers` | `{file: sha256}`: observation scripts beside the contract, pinned by hash, mounted read-only |
| `cases` | `[{name, argv, input, expect}]`. The program reads `input` as JSON on stdin and must print one JSON value. The broker compares it with `expect` outside the sandbox. At least two different `expect` values are required. |
| `base_witness` | `{case, observed}`: the wrong result the base must produce. This proves the case can see the defect. |
| `plant` | `{path, content, case, observed}`: a replacement file and the wrong result it must produce. This proves the cases can catch a shortcut, so use a constant answer when two legitimate outcomes differ. |

## Placeholders in `argv`

Each contract instance gets its own ordinary-looking sandbox layout, so code can't tell the checks apart by fixed paths:

| Placeholder | Mount |
|---|---|
| `{src}` | The candidate sources (also the working directory), for example `/home/runner/app-3f9a1c` |
| `{checks}` | The drivers, for example `/opt/tools-81b20e` |
| `{node}` | The Node binary given with `--node`, at `/usr/local/bin/node` |

`PATH` is `/usr/local/bin:/usr/bin:/bin`, and `HOME` and `USER` are set. Drivers receive the paths they need as arguments (see `examples/classifier/report_cli.py`).

## Sandbox

Each probe runs under `systemd-run --user` with `bwrap`:
- no network, no host home, no daemon sockets, and a cleared environment;
- read-only system binaries and libraries, plus the sources and drivers;
- an 8 MiB `/tmp`;
- 40 tasks, 384 MiB, one CPU of quota and 6 seconds.

A crash, timeout, malformed output or missing runtime fails closed.

## Landing

After applying `accepted/change.patch` to the base, run the landing check:

```sh
agent-guard verify --repo REPO --contract CONTRACT --node NODE --bundle RUN/accepted --commit LANDING_SHA
```

It checks the contract and patch hashes, the ancestry and the entire tree. It then re-runs the witness, the cases and the plant on that exact commit. A different tree needs a fresh run.

## Writing the next contract

1. Pin the base, and name the observable change.
2. Write cases that exercise the real entry point as well as the function. Include one where the command, not just the function, would break.
3. Pick the witness from the defect as reported, at a caller that matters.
4. Plant the cheapest wrong implementation (a constant, a disabled branch) and record what it returns.
5. When a defect escapes anyway, reproduce it, add a case, and version the contract.
