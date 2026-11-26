"""Hook behaviour end to end: every rule must refuse its planted fault and pass the clean twin."""
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
BIN = os.path.join(ROOT, "bin", "agent-guard")
TMP = tempfile.mkdtemp(prefix="agent-guard-test-")
atexit.register(shutil.rmtree, TMP, True)
CFG = os.path.join(TMP, "config.json")
REPO = os.path.join(TMP, "repo")
SCRATCH = os.path.join(TMP, "scratch")
SPEC = "it('a', () => {\n  expect(a()).toBe(1);\n  expect(b()).toBe(2);\n});\nit('b', () => {\n  expect(c()).toBe(3);\n});\n"
for d in (os.path.join(REPO, "src", "__tests__"), SCRATCH):
    os.makedirs(d, exist_ok=True)
with open(CFG, "w") as fh:
    json.dump({"log": os.path.join(TMP, "events.jsonl"), "rule_packs": []}, fh)
subprocess.run(["git", "init", "-q", REPO], check=True)
with open(os.path.join(REPO, "src", "__tests__", "b.spec.ts"), "w") as fh:
    fh.write(SPEC)
subprocess.run(["git", "-C", REPO, "add", "src/__tests__/b.spec.ts"], check=True)


def run(kind, event, env=None):
    e = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_GUARD_")}
    e["AGENT_GUARD_CONFIG"] = CFG
    e.update(env or {})
    p = subprocess.run([sys.executable, BIN, "hook", kind], input=json.dumps(event), capture_output=True, text=True, env=e)
    return p.returncode, p.stderr


def write(rel, content, env=None):
    return run("pre", {"tool_name": "Write", "cwd": REPO, "tool_input": {"file_path": os.path.join(REPO, rel), "content": content}}, env)


def edit(rel, old, new, env=None):
    return run("pre", {"tool_name": "Edit", "cwd": REPO,
                       "tool_input": {"file_path": os.path.join(REPO, rel), "old_string": old, "new_string": new}}, env)


def bash(cmd, env=None):
    return run("pre", {"tool_name": "Bash", "cwd": REPO, "tool_input": {"command": cmd, "description": "x || true"}}, env)


def config(**extra):
    path = os.path.join(TMP, f"cfg-{len(os.listdir(TMP))}.json")
    with open(path, "w") as fh:
        json.dump({"log": os.path.join(TMP, "e.jsonl"), "rule_packs": [], **extra}, fh)
    return {"AGENT_GUARD_CONFIG": path}


class Base(unittest.TestCase):
    def refused(self, res, rule):
        code, err = res
        self.assertEqual(code, 2, f"expected a refusal for {rule}, got exit {code}: {err}")
        self.assertIn(f"[{rule}]", err)

    def passed(self, res):
        code, err = res
        self.assertEqual(code, 0, f"expected a pass, got exit {code}: {err}")


class ContentRules(Base):
    def test_rules_reach_the_agent_through_the_hook(self):
        self.refused(write("src/a.ts", "// @ts-ignore\nconst x: number = f();\n"), "suppress-checker")
        self.refused(write("src/a.ts", "export function f() {\n  // TODO: handle tenants\n  return 1;\n}\n"), "placeholder")
        self.refused(write("src/a.ts", "export class A {\n  // ... existing code ...\n}\n"), "elision")
        self.refused(write("src/a.ts", "// await anchorStore.verify(head);\nexport const x = 1;\n"), "commented-out-code")
        self.refused(write("src/__tests__/a.spec.ts", "it.skip('refuses tenant B', () => {});\n"), "skipped-test")
        self.refused(write("src/a.ts", "export const x = y as any;\n"), "type-escape")
        self.refused(write("src/a.ts", "try { await f(); } catch (e) { console.log(e); }\n"), "swallowed-error")
        self.refused(write("src/a.ts", "if (process.env.NODE_ENV === 'test') { return true; }\n"), "test-env-branch")
        self.refused(write("src/a.mjs", "const probe = process.env.PATH === '/runtime:/usr/bin';\n"), "sandbox-probe")
        self.refused(write("src/a.ts", "export function verifyAnchor(head: string): boolean { return true; }\n"), "vacuous-guard")
        self.refused(write("src/a.ts", "export const CATALOG_TABLES: readonly string[] = [];\n"), "vacuous-gate-list")
        self.passed(write("src/a.ts", "// Refuse an unbound tenant: RLS would hide every row.\nexport const x: number = 1;\n"))

    def test_edit_reports_the_file_line(self):
        path = os.path.join(REPO, "src", "lines.ts")
        with open(path, "w") as fh:
            fh.write("export const a = 1;\nexport const b = 2;\nexport const c = 3;\n")
        code, err = edit("src/lines.ts", "export const c = 3;", "export const c = y as any;")
        self.assertEqual(code, 2)
        self.assertIn("lines.ts:3", err)

    def test_scratch_outside_a_repository_is_not_product_code(self):
        self.passed(run("pre", {"tool_name": "Write", "cwd": REPO, "tool_input": {
            "file_path": f"{SCRATCH}/probe.mjs", "content": "try { f(); } catch (e) { console.log(e.message); }\n"}}))


class TestProtection(Base):
    def setUp(self):
        with open(os.path.join(REPO, "src", "__tests__", "b.spec.ts"), "w") as fh:
            fh.write(SPEC)

    def test_assertion_drop(self):
        self.refused(edit("src/__tests__/b.spec.ts", "  expect(a()).toBe(1);\n  expect(b()).toBe(2);\n", "  expect(a()).toBe(1);\n"), "assertion-drop")
        self.passed(edit("src/__tests__/b.spec.ts", "  expect(a()).toBe(1);\n", "  expect(a()).toBe(1);\n  expect(a2()).toBe(1);\n"))

    def test_test_removed(self):
        self.refused(write("src/__tests__/b.spec.ts", "it('a', () => {\n  expect(a()).toBe(1);\n  expect(b()).toBe(2);\n  expect(c()).toBe(3);\n});\n"), "test-removed")

    def test_test_lock_for_builders(self):
        env = {"AGENT_GUARD_ROLE": "builder"}
        self.refused(edit("src/__tests__/b.spec.ts", "toBe(3)", "toBe(4)", env), "test-lock")
        self.passed(write("src/a.ts", "export const x = 1;\n", env))

    def test_gate_config(self):
        self.refused(edit("tsconfig.json", '"strict": true', '"strict": false'), "gate-config")
        self.refused(write(".github/workflows/ci.yml", "jobs: {}\n"), "gate-config")
        self.refused(write("package.json", "{}\n"), "gate-config")

    def test_guard_tamper(self):
        self.refused(run("pre", {"tool_name": "Write", "cwd": REPO, "tool_input": {"file_path": os.path.expanduser("~/.claude/settings.json"), "content": "{}"}}), "guard-tamper")
        self.refused(run("pre", {"tool_name": "Edit", "cwd": REPO, "tool_input": {"file_path": os.path.join(ROOT, "agentguard", "hook.py"), "old_string": "a", "new_string": "b"}}), "guard-tamper")
        self.refused(bash("sed -i 's/enforce/warn/' ~/.config/agent-guard/config.json"), "guard-tamper")
        self.refused(bash("echo '{}' > ~/.codex/hooks.json"), "guard-tamper")
        self.passed(bash("cat ~/.codex/hooks.json"))

    def test_allow_entries_expire_and_never_cover_the_guard(self):
        env = config(allow=[{"rule": "gate-config", "path": "*/tsconfig.json", "until": "2999-01-01", "reason": "t", "approved_by": "owner"},
                            {"rule": "gate-config", "path": "*/turbo.json", "until": "2000-01-01", "reason": "expired", "approved_by": "owner"},
                            {"rule": "guard-tamper", "path": "*", "reason": "never honoured"}])
        self.passed(edit("tsconfig.json", "a", "b", env))
        self.refused(edit("turbo.json", "a", "b", env), "gate-config")
        self.refused(run("pre", {"tool_name": "Write", "cwd": REPO, "tool_input": {"file_path": os.path.join(ROOT, "bin", "agent-guard"), "content": ""}}, env), "guard-tamper")


class ShellRules(Base):
    def test_shell_patterns(self):
        self.refused(bash("git commit --no-verify -m 'wip'"), "hook-bypass")
        self.refused(bash("git config core.hooksPath /dev/null"), "hook-bypass")
        self.passed(bash("git config --get core.hooksPath; cat lefthook.yml"))
        self.refused(bash("pnpm exec tsc --ignoreConfig src/a.ts"), "checker-weakened")
        self.refused(bash("git push --force origin feat/x"), "force-push")

    def test_masked_exit(self):
        self.refused(bash("pnpm test || true"), "masked-exit")
        self.refused(bash("pnpm --filter @acme/api test 2>&1 | tail -n 40"), "masked-exit")
        self.refused(bash("npx vitest run; true"), "masked-exit")
        self.refused(bash("set +e; pnpm check"), "masked-exit")
        self.passed(bash("set -o pipefail; pnpm --filter @acme/api test 2>&1 | tail -n 40"))
        self.passed(bash("rg -n vitest packages | head"))
        self.passed(bash("pnpm exec vitest list --help | grep -i reporter"))
        self.passed(bash("pnpm exec prettier --write src/a.ts"))

    def test_shell_test_mutation(self):
        self.refused(bash("rm src/__tests__/b.spec.ts"), "test-file-mutation")
        self.refused(bash("git rm -q src/__tests__/b.spec.ts"), "test-file-mutation")
        self.refused(bash("sed -i 's/toBe(3)/toBe(4)/' src/__tests__/b.spec.ts"), "test-file-mutation")
        self.refused(bash("python3 -c \"open('src/__tests__/b.spec.ts','w').write('')\""), "test-file-mutation")
        self.refused(bash("echo '{}' > tsconfig.json"), "test-file-mutation")
        self.refused(bash("F=tsconfig.json; echo '{}' > $F"), "test-file-mutation")
        self.passed(bash("cat src/__tests__/b.spec.ts > /tmp/copy.txt"))
        self.passed(bash("sed -n '1,20p' src/__tests__/b.spec.ts"))
        self.passed(bash(f"SP={SCRATCH}; cp $SP/x.json $SP/tsconfig.json"))

    def test_own_untracked_test_may_be_removed(self):
        with open(os.path.join(REPO, "src", "__tests__", "zz.spec.ts"), "w") as fh:
            fh.write("it('x', () => { expect(1).toBe(1); });\n")
        self.passed(bash("rm src/__tests__/zz.spec.ts"))

    def test_copied_and_heredoc_content_is_checked(self):
        with open(os.path.join(SCRATCH, "draft.spec.ts"), "w") as fh:
            fh.write("it.skip('a', () => { expect(1).toBe(1); });\n")
        self.refused(bash(f"cp {SCRATCH}/draft.spec.ts src/__tests__/new.spec.ts"), "skipped-test")
        self.refused(bash("cat > src/c.ts <<'EOF'\nexport const x = y as any;\nEOF"), "type-escape")
        self.passed(bash("cat > src/c.ts <<'EOF'\nexport const x: number = 1;\nEOF"))
        self.passed(bash(f"cat > {SCRATCH}/mech.sh <<'EOF'\necho \"## required sections, no placeholder\"\npnpm test || true\nEOF"))

    def test_repo_copy(self):
        self.refused(bash("cp -r . /tmp/repo-copy"), "repo-copy")
        self.refused(bash(f"rsync -a {REPO} /tmp/repo-copy"), "repo-copy")
        self.refused(bash("git worktree add /tmp/wt HEAD"), "repo-copy")
        self.passed(bash("cp -r fixtures/one /tmp/fx"))


class CodexShapes(Base):
    def test_apply_patch_tool(self):
        patch = "*** Begin Patch\n*** Add File: src/d.ts\n+export const x = y as any;\n*** End Patch\n"
        self.refused(run("pre", {"tool_name": "apply_patch", "cwd": REPO, "tool_input": {"input": patch}}), "type-escape")

    def test_code_mode_exec(self):
        js = ('text(await tools.exec_command({cmd:"pnpm test || true","max_output_tokens":500}));\n'
              'text(await tools.apply_patch("*** Begin Patch\\n*** Add File: src/e.ts\\n+// TODO: later\\n+export const e = 1;\\n*** End Patch\\n"));')
        code, err = run("pre", {"tool_name": "exec", "cwd": REPO, "tool_input": {"input": js}})
        self.assertEqual(code, 2, err)
        self.assertIn("[masked-exit]", err)
        self.assertIn("[placeholder]", err)

    def test_shell_tool_argv(self):
        self.refused(run("pre", {"tool_name": "shell", "cwd": REPO, "tool_input": {"command": ["bash", "-lc", "git commit --no-verify -m x"]}}), "hook-bypass")


class StopClaims(Base):
    def stop(self, entries, active=False, session=None, env=None, last=None):
        path = os.path.join(TMP, "t.jsonl")
        with open(path, "w") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")
        event = {"hook_event_name": "Stop", "cwd": REPO, "stop_hook_active": active, "transcript_path": path}
        if session:
            event["session_id"] = session
        if last is not None:
            event["last_assistant_message"] = last
        return run("stop", event, env)

    @staticmethod
    def call(i, name, cmd="", path=None):
        inp = {"command": cmd} if name == "Bash" else {"file_path": path or os.path.join(REPO, "src", "a.ts")}
        return {"message": {"role": "assistant", "content": [{"type": "tool_use", "id": i, "name": name, "input": inp}]}}

    @staticmethod
    def result(i, err=False, text="ok"):
        return {"message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": i, "is_error": err, "content": text}]}}

    @staticmethod
    def say(text):
        return {"message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}

    def test_claims(self):
        self.refused(self.stop([self.call("1", "Edit"), self.result("1"), self.say("Done. All tests pass.")]), "unbacked-claim")
        self.refused(self.stop([self.call("1", "Edit"), self.result("1"), self.call("2", "Bash", "pnpm test"),
                                self.result("2", True, "Exit code 1\nFAIL"), self.say("Tests pass now.")]), "unbacked-claim")
        self.refused(self.stop([self.call("1", "Bash", "pnpm test"), self.result("1"), self.call("2", "Edit"), self.result("2"),
                                self.say("Everything works.")]), "unbacked-claim")
        self.passed(self.stop([self.call("1", "Edit"), self.result("1"), self.call("2", "Bash", "set -o pipefail; pnpm test 2>&1 | tail"),
                               self.result("2", False, "Tests 12 passed"), self.say("All tests pass (exit 0).")]))
        # Printed text is not evidence: any command can print rc=0. A background check is cited, not claimed.
        self.refused(self.stop([self.call("1", "Edit"), self.result("1"), self.call("2", "Bash", "nohup bash run-check.sh > check.log &"),
                                self.result("2"), self.call("3", "Bash", "tail -3 check.log"),
                                self.result("3", False, "Tasks:    9 successful, 9 total\ncheck_rc=0"), self.say("The full check passes at the head (rc 0).")]), "unbacked-claim")
        self.passed(self.stop([self.call("1", "Edit"), self.result("1"), self.call("2", "Bash", "nohup bash run-check.sh > check.log &"),
                               self.result("2"), self.say("The full check is running; its log will be at check.log.")]))
        self.passed(self.stop([self.call("1", "Bash", "pnpm test"), self.result("1"), self.call("2", "Write", path="/home/x/evidence.md"),
                               self.result("2"), self.say("All tests pass.")]))
        self.passed(self.stop([self.call("1", "Edit"), self.result("1"), self.say("I changed the guard. I have not run the tests yet.")]))

    def test_the_check_must_match_the_claim(self):
        lint = [self.call("1", "Edit"), self.result("1"), self.call("2", "Bash", "pnpm lint"), self.result("2")]
        self.refused(self.stop(lint + [self.say("All tests pass.")]), "unbacked-claim")
        self.passed(self.stop(lint + [self.say("Lint passes.")]))
        wrapped = config(check_wrappers=["run-check"])
        typecheck = [self.call("1", "Edit"), self.result("1"), self.call("2", "Bash", "~/.local/bin/run-check pnpm -F @acme/core typecheck"), self.result("2")]
        self.passed(self.stop(typecheck + [self.say("Typecheck passes.")], env=wrapped))
        self.refused(self.stop(typecheck + [self.say("The full check passes.")], env=wrapped), "unbacked-claim")
        full = [self.call("1", "Edit"), self.result("1"), self.call("2", "Bash", "TURBO_FORCE=1 pnpm check"), self.result("2")]
        self.passed(self.stop(full + [self.say("The full check passes and all tests pass.")]))
        masked = [self.call("1", "Edit"), self.result("1"), self.call("2", "Bash", "pnpm test 2>&1 | tail -5"), self.result("2")]
        self.refused(self.stop(masked + [self.say("All tests pass.")]), "unbacked-claim")

    def test_refusals_are_bounded_and_the_verdict_is_durable(self):
        env = config(state_dir=os.path.join(TMP, "state"), stop_blocks=2)
        claim = [self.call("1", "Edit"), self.result("1"), self.say("All tests pass.")]
        self.refused(self.stop(claim, session="s-1", env=env), "unbacked-claim")
        self.refused(self.stop(claim, active=True, session="s-1", env=env), "unbacked-claim")
        code, err = self.stop(claim, active=True, session="s-1", env=env)
        self.assertEqual(code, 0, err)
        verdicts = lambda: subprocess.run([sys.executable, BIN, "verdicts"], capture_output=True, text=True,
                                          env={**{k: v for k, v in os.environ.items() if not k.startswith("AGENT_GUARD_")}, **env})
        v = verdicts()
        self.assertEqual(v.returncode, 1, v.stdout)
        self.assertIn('"status": "unresolved"', v.stdout)
        backed = [self.call("1", "Edit"), self.result("1"), self.call("2", "Bash", "pnpm test"), self.result("2"), self.say("All tests pass.")]
        self.passed(self.stop(backed, session="s-1", env=env))
        self.assertEqual(verdicts().returncode, 0)

    def test_codex_stop_payload(self):
        # Codex sends last_assistant_message and no transcript for ephemeral sessions; exit 0 must print JSON.
        env = config(state_dir=os.path.join(TMP, "state2"))
        code, err = run("stop", {"hook_event_name": "Stop", "cwd": REPO, "session_id": "cx-1", "transcript_path": None,
                                 "stop_hook_active": False, "last_assistant_message": "All tests pass."}, env)
        self.assertEqual(code, 2)
        self.assertIn("[unbacked-claim]", err)
        e = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_GUARD_")}
        e.update(env)
        p = subprocess.run([sys.executable, BIN, "hook", "stop"], input=json.dumps({"cwd": REPO, "session_id": "cx-2",
                           "last_assistant_message": "Done; I have not run the tests."}), capture_output=True, text=True, env=e)
        self.assertEqual((p.returncode, json.loads(p.stdout)), (0, {}))

    def test_forbidden_claim_and_no_loop(self):
        self.refused(self.stop([self.say("The audit log is production-ready.")]), "forbidden-claim")
        self.passed(self.stop([self.say("Nothing is production-ready yet.")]))
        self.passed(self.stop([self.call("1", "Edit"), self.result("1"), self.say("All tests pass.")], active=True))


class ModesAndPacks(Base):
    def test_fail_open_on_garbage(self):
        e = dict(os.environ, AGENT_GUARD_CONFIG=CFG)
        p = subprocess.run([sys.executable, BIN, "hook", "pre"], input="not json", capture_output=True, text=True, env=e)
        self.assertEqual(p.returncode, 0)

    def test_warn_mode_and_scope(self):
        self.passed(write("src/a.ts", "export const x = y as any;\n", config(mode="warn")))
        self.passed(write("src/a.ts", "export const x = y as any;\n", config(enforce_paths=["/nonexistent/project"])))

    def test_added_pack_rule_blocks_and_warn_rule_only_logs(self):
        pack = os.path.join(TMP, "pack.json")
        with open(pack, "w") as fh:
            json.dump({"rules": [
                {"id": "no-sleep-in-tests", "kind": "line", "part": "code", "files": "test", "pattern": r"\bsetTimeout\(|\bsleep\(",
                 "help": "Wait for the event, not for time.", "examples": {
                     "refuse": [{"path": "src/__tests__/a.spec.ts", "text": "await new Promise((r) => setTimeout(r, 500));"}],
                     "allow": [{"path": "src/__tests__/a.spec.ts", "text": "await vi.waitFor(() => expect(done).toBe(true));"}]}},
                {"id": "no-console-log", "kind": "line", "part": "code", "files": "nontest", "mode": "warn", "pattern": r"\bconsole\.log\(",
                 "help": "Use the logger.", "examples": {"refuse": ["console.log(x);"], "allow": ["logger.info(x);"]}},
                {"id": "type-escape", "kind": "line", "pattern": "x", "help": "tries to replace a core rule",
                 "examples": {"refuse": ["x"], "allow": ["y"]}}]}, fh)
        env = config(rule_packs=[pack])
        self.refused(write("src/__tests__/s.spec.ts", "await new Promise((r) => setTimeout(r, 500));\n", env), "no-sleep-in-tests")
        self.passed(write("src/b.ts", "console.log(x);\n", env))
        self.refused(write("src/b.ts", "export const z = y as any;\n", env), "type-escape")
        self.passed(write("src/b.ts", "export const x = 1;\n", env))


if __name__ == "__main__":
    unittest.main(verbosity=2)
