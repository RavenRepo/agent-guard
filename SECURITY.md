# Security policy

agent-guard is a security control: it stands between a coding agent and the shortcuts the agent may take. A way to get around it is a vulnerability, even when no secret is exposed.

## In scope

- **Bypasses:** an edit, command or claim that a documented rule should refuse but doesn't, because of how the guard parses or scopes it. This is different from a pattern no rule covers yet; for those, open a "Missed shortcut" issue.
- **Tampering:** a way for an agent to disable, redirect or weaken the hooks, the policy or the rule packs.
- **Sandbox escapes:** a way out of the sandbox in guarded builds, or a way to make the broker accept a change whose behaviour wasn't proven.
- **Leaks:** secrets or file contents leaking into the event log. The log is meant to hold rule ids, paths, line numbers and hashes only.
- **Installer flaws:** anything in the installer that leaves a host less safe than before, or that can't be undone.

## Reporting

Please report privately through **GitHub's private vulnerability reporting**: on this repository, open the Security tab and choose "Report a vulnerability". Include:

- the version (`agent-guard version`), the client and its version;
- the smallest event JSON or command that reproduces it;
- what you expected, and what happened.

We aim to acknowledge a report within 3 working days. We agree a fix and disclosure date with you, and credit you in the advisory unless you prefer otherwise.

## Supported versions

Until 1.0, only the latest minor release receives security fixes.

| Version | Supported |
|---|---|
| 0.3.x | Yes |
| < 0.3 | No |
