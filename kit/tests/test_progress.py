"""0.5.4: `pk drift` D7 (progress docs behind state), `pk status ready`, the stop-hook adapter and `pk hook`."""
import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import io
import json
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

import handoff
import hook
import pk
import progress
from common import KitError, write_json, write_yaml
from test_register import reg, task
from test_worktree import commit_all, make_repo, run_cli, sh

KIT = Path(__file__).resolve().parent.parent
T1, T2, T3, T4, T5 = (f"P1-M01-S01-T0{n}" for n in range(1, 6))
ADAPTER = hook.ADAPTERS[0]
SETTINGS = hook.SETTINGS[ADAPTER].as_posix()
PROGRESS = {"docs": ["program/DASHBOARD.md", "program/SUCCESSOR_PROMPT.md"],
            "state": ["program/status", "program/rulings.md", "program/controller-state.json"]}


def status_record(tid, state):
    return {"task_id": tid, "status": state, "role": "controller", "updated_at": "2026-10-31T00:00:00Z",
            "note": "", "evidence": [], "blocked_by": [], "history": []}


class ProgressBase(unittest.TestCase):
    def setUp(self):
        self.root = make_repo(self)
        program = self.root / "program"
        program.mkdir()
        shutil.copy(KIT / "templates" / "HANDOFF.md", program / "HANDOFF.md")
        shutil.copy(KIT / "templates" / "controller-state.json", program / "controller-state.json")
        write_yaml(program / "register.yaml", reg(
            task(T1), task(T2, depends_on=[T1]), task(T3, depends_on=[T1, T2]), task(T4), task(T5, depends_on=[T4])))
        self.checks(PROGRESS)
        self.write("program/DASHBOARD.md", "# dashboard\n")
        self.write("program/SUCCESSOR_PROMPT.md", "# successor\n")
        self.write("program/rulings.md", "# rulings\n")
        commit_all(self.root, "program")
        handoff.update(self.root)
        commit_all(self.root, "hand-off")
        env = {key: value for key, value in os.environ.items() if key != hook.PROJECT_ENV[ADAPTER]}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def checks(self, section=None):
        data = {"build": [], "test": [], "scan": []}
        if section is not None:
            data["progress"] = section
        write_yaml(self.root / "program" / "checks.yaml", data)

    def state_commit(self, tid, state, message=None):
        write_json(self.root / "program" / "status" / f"{tid}.json", status_record(tid, state))
        commit_all(self.root, message or f"status {tid} {state}")

    def docs_commit(self, text="updated\n"):
        for rel in PROGRESS["docs"]:
            self.write(rel, text)
        commit_all(self.root, "progress docs")

    def hook_run(self, payload=None, root=None):
        stdin = io.StringIO(json.dumps(payload if payload is not None else {"hook_event_name": "Stop"}))
        with mock.patch("sys.stdin", stdin), mock.patch("hook.find_root", return_value=root or self.root):
            return run_cli("drift", self.root, "drift", "--hook", ADAPTER)


class BehindTests(ProgressBase):
    def test_docs_newer_than_every_state_commit_are_current(self):
        result = progress.behind(self.root, progress.config(self.root))
        self.assertTrue(result["ok"], result)
        self.assertIn("progress docs current", result["detail"])

    def test_a_state_commit_after_the_docs_drifts_and_names_the_commits(self):
        self.state_commit(T1, "in_progress", "dispatch T1")
        self.state_commit(T4, "in_progress", "dispatch T4")
        result = progress.behind(self.root, progress.config(self.root))
        self.assertFalse(result["ok"])
        self.assertEqual([line.split(" ", 1)[1] for line in result["newer"]], ["dispatch T4", "dispatch T1"])
        self.assertIn("2 newer state commit(s) on main", result["detail"])
        self.docs_commit()
        self.assertTrue(progress.behind(self.root, progress.config(self.root))["ok"])

    def test_the_oldest_doc_decides(self):
        self.state_commit(T1, "in_progress")
        self.write("program/DASHBOARD.md", "only the dashboard moved\n")
        commit_all(self.root, "dashboard only")
        result = progress.behind(self.root, progress.config(self.root))
        self.assertFalse(result["ok"])  # SUCCESSOR_PROMPT.md was last changed before the state commit
        self.assertIn("program/SUCCESSOR_PROMPT.md last changed in", result["detail"])

    def test_at_most_six_commits_are_named(self):
        for number in range(8):
            self.write("program/rulings.md", f"# rulings\nR{number}\n")
            commit_all(self.root, f"ruling R{number}")
        result = progress.behind(self.root, progress.config(self.root))
        self.assertEqual((len(result["newer"]), result["more"]), (6, 2))
        self.assertIn("(and 2 more)", result["detail"])

    def test_a_doc_never_committed_counts_every_state_commit(self):
        self.checks({"docs": ["program/NOT_YET.md"], "state": PROGRESS["state"]})
        commit_all(self.root, "checks")
        result = progress.behind(self.root, progress.config(self.root))
        self.assertFalse(result["ok"])
        self.assertIn("program/NOT_YET.md never committed on main", result["detail"])

    def test_a_branch_commit_merged_after_the_docs_counts(self):
        sh(self.root, "git", "switch", "-q", "-c", "task/" + T1)
        self.state_commit(T1, "review", "on the branch")
        sh(self.root, "git", "switch", "-q", "main")
        self.docs_commit("docs after the branch commit\n")
        sh(self.root, "git", "merge", "-q", "--no-ff", "-m", "merge T1", "task/" + T1)
        result = progress.behind(self.root, progress.config(self.root))
        self.assertFalse(result["ok"])
        self.assertTrue(any("on the branch" in line for line in result["newer"]), result)

    def test_absent_config_is_skipped_and_says_so(self):
        self.checks()
        commit_all(self.root, "no progress section")
        self.state_commit(T1, "in_progress")
        result = progress.behind(self.root, progress.config(self.root))
        self.assertTrue(result["ok"])
        self.assertIn("skipped: not configured", result["detail"])

    def test_the_section_is_checked_by_shape_by_the_gate_and_doctor(self):
        import gate

        for bad in ({"docs": [], "state": ["x"]}, {"docs": ["x"]}, {"docs": ["../x"], "state": ["y"]},
                    {"docs": ["x"], "state": ["y"], "quiet_when": ["argv"]}, {"docs": ["x"], "state": ["y"], "z": 1},
                    ["x"]):
            with self.subTest(bad=bad):
                with self.assertRaises(KitError):
                    progress.parse(bad)
                self.assertTrue(any("progress" in problem for problem in pk._checks_problems(
                    {"build": [["true"]], "test": [["true"]], "scan": [["true"]], "progress": bad})))
        good = dict(PROGRESS, quiet_when="exit 1")
        self.assertEqual(gate._parse_checks({"build": [["b"]], "test": [["t"]], "scan": [["s"]], "progress": good},
                                            ())["progress"]["quiet_when"], "exit 1")
        self.assertEqual(pk._checks_problems({"build": [["b"]], "test": [["t"]], "scan": [["s"]], "progress": good}),
                         [])


class ReadyTests(ProgressBase):
    def ready(self):
        found = progress.ready(self.root)
        return [i["id"] for i in found["ready"]], [(i["id"], i["waiting_on"]) for i in found["near_ready"]]

    def test_ready_and_near_ready(self):
        self.assertEqual(self.ready(), ([T1, T4], [(T2, T1), (T5, T4)]))  # T3 waits on two
        self.state_commit(T1, "done")
        self.assertEqual(self.ready(), ([T2, T4], [(T3, T2), (T5, T4)]))
        self.state_commit(T2, "in_progress")
        self.assertEqual(self.ready(), ([T4], [(T3, T2), (T5, T4)]))

    def test_a_worktree_or_a_task_or_wip_branch_takes_a_task_out(self):
        sh(self.root, "git", "branch", "task/" + T1)
        sh(self.root, "git", "branch", f"wip/{T4}-try")
        self.assertEqual(self.ready(), ([], [(T2, T1), (T5, T4)]))
        sh(self.root, "git", "branch", "-D", "task/" + T1, f"wip/{T4}-try")
        sh(self.root, "git", "worktree", "add", "-q", "--detach", str(self.root / ".worktrees" / T4.lower()))
        self.assertEqual(self.ready(), ([T1], [(T2, T1), (T5, T4)]))

    def test_the_command_plain_and_json(self):
        code, out, err = run_cli("status", self.root, "status", "ready")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines(), [f"{T1} ready: t {T1}", f"{T4} ready: t {T4}",
                                            f"{T2} near-ready, after {T1} (not_started): t {T2}",
                                            f"{T5} near-ready, after {T4} (not_started): t {T5}"])
        code, out, _ = run_cli("status", self.root, "status", "ready", "--json")
        data = json.loads(out)
        self.assertEqual(sorted(data), ["near_ready", "ready"])
        self.assertEqual(data["near_ready"][0], {"id": T2, "title": "t " + T2, "waiting_on": T1,
                                                 "waiting_status": "not_started"})


class DriftReportTests(ProgressBase):
    def test_drift_prints_d7_and_the_ready_list_as_information(self):
        code, out, _ = run_cli("drift", self.root, "drift", "--strict")
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"(?m)^  D7 ok +progress docs current")
        self.assertIn("ready to start (information, not a check): 4", out)
        self.assertIn(f"    {T1} ready: t {T1}", out)
        self.assertEqual(out.splitlines()[-1], "  0 of 7 checks drift")
        self.state_commit(T1, "in_progress", "dispatch T1")
        code, out, _ = run_cli("drift", self.root, "drift", "--strict")
        self.assertEqual(code, 1)
        self.assertRegex(out, r"(?m)^  D7 DRIFT progress docs behind state: .*dispatch T1")
        self.assertRegex(out.splitlines()[-1], r"^  [12] of 7 checks drift$")  # D3 too: the hand-off lists statuses


class HookTests(ProgressBase):
    def test_clean_is_silent(self):
        self.assertEqual(self.hook_run(), (0, "", ""))

    def test_stale_docs_block_with_the_reason_and_the_ready_list(self):
        self.state_commit(T1, "done", "T1 done")
        code, out, err = self.hook_run()
        self.assertEqual((code, err), (0, ""))
        answer = json.loads(out)  # standard output holds the answer and nothing else
        self.assertEqual(sorted(answer), ["decision", "reason"])
        self.assertEqual(answer["decision"], "block")
        for part in ("program/DASHBOARD.md, program/SUCCESSOR_PROMPT.md", "T1 done", "progress pass",
                     "republish its tracker", "fill free agent slots", f"{T2} ready", f"{T3} near-ready, after {T2}"):
            self.assertIn(part, answer["reason"])

    def test_a_stale_hand_off_alone_blocks(self):
        self.checks()  # no progress section: D7 is skipped, D3 still counts
        commit_all(self.root, "no progress section")
        self.assertEqual(self.hook_run(), (0, "", ""))
        self.state_commit(T1, "in_progress")  # the hand-off lists statuses: it is stale now
        reason = json.loads(self.hook_run()[1])["reason"]
        self.assertIn("pk drift D3", reason)
        self.assertNotIn("D7", reason)

    def test_stop_hook_active_and_quiet_when_keep_it_silent(self):
        self.state_commit(T1, "in_progress")
        self.assertEqual(self.hook_run({"stop_hook_active": True}), (0, "", ""))
        self.checks(dict(PROGRESS, quiet_when="test -f program/QUIET"))
        commit_all(self.root, "quiet when")
        self.state_commit(T4, "in_progress")
        self.assertEqual(json.loads(self.hook_run()[1])["decision"], "block")
        self.write("program/QUIET", "")
        self.assertEqual(self.hook_run(), (0, "", ""))

    def test_a_problem_is_one_line_on_stderr_and_never_blocks(self):
        self.write("program/checks.yaml", "progress: [unclosed\n")
        code, out, err = self.hook_run()
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("error: pk drift --hook:"), err)

    def test_the_project_directory_variable_names_the_root(self):
        self.state_commit(T1, "in_progress")
        elsewhere = make_repo(self)
        with mock.patch.dict(os.environ, {hook.PROJECT_ENV[ADAPTER]: str(self.root)}):
            code, out, _ = self.hook_run(root=elsewhere)  # find_root would name another repository
        self.assertEqual(json.loads(out)["decision"], "block")


class HookInstallTests(ProgressBase):
    def settings(self):
        return json.loads((self.root / SETTINGS).read_text())

    def ignore(self):
        self.write(".gitignore", f".worktrees/\n{SETTINGS}\n")
        commit_all(self.root, "ignore local settings")

    def test_refuses_unless_the_file_is_ignored_and_untracked(self):
        code, out, err = run_cli("hook", self.root, "hook", "install", ADAPTER)
        self.assertEqual(code, 1)
        self.assertIn("is not git-ignored; add the line", err)
        self.assertFalse((self.root / SETTINGS).exists())
        self.write(SETTINGS, "{}\n")
        sh(self.root, "git", "add", "-f", SETTINGS)
        self.ignore()
        code, _, err = run_cli("hook", self.root, "hook", "install", ADAPTER)
        self.assertEqual(code, 1)
        self.assertIn("is tracked by git", err)

    def test_install_merges_is_idempotent_and_remove_takes_only_its_own_entry(self):
        self.ignore()
        other = {"type": "command", "command": "echo other", "timeout": 5}
        before = {"permissions": {"allow": ["Bash(ls:*)"]},
                  "hooks": {"Stop": [{"hooks": [other]}], "PreToolUse": [{"matcher": "Bash", "hooks": [other]}]}}
        self.write(SETTINGS, json.dumps(before))
        code, out, err = run_cli("hook", self.root, "hook", "install", ADAPTER)
        self.assertEqual((code, err), (0, ""), out)
        after = self.settings()
        self.assertEqual(after["permissions"], before["permissions"])
        self.assertEqual(after["hooks"]["PreToolUse"], before["hooks"]["PreToolUse"])
        self.assertEqual(after["hooks"]["Stop"][0], {"hooks": [other]})
        ours = after["hooks"]["Stop"][1]["hooks"][0]
        self.assertEqual(ours["command"], hook.command(ADAPTER))
        self.assertTrue(ours["command"].endswith(f"/kit/scripts/pk.py drift --hook {ADAPTER}"))
        self.assertIn("already installed", run_cli("hook", self.root, "hook", "install", ADAPTER)[1])
        self.assertEqual(self.settings(), after)
        self.assertIn("\tinstalled", run_cli("hook", self.root, "hook", "list")[1])
        code, out, _ = run_cli("hook", self.root, "hook", "remove", ADAPTER)
        self.assertEqual(code, 0)
        self.assertEqual(self.settings(), before)
        self.assertIn("not installed", run_cli("hook", self.root, "hook", "remove", ADAPTER)[1])
        self.assertFalse((self.root / SETTINGS).with_name("settings.json").exists())  # never the shared file
        self.assertEqual(sorted(p.name for p in (self.root / ".git" / "hooks").iterdir() if not p.name.endswith(
            ".sample")), [])  # never a git hook

    def test_install_creates_the_file_and_moves_an_old_path(self):
        self.ignore()
        run_cli("hook", self.root, "hook", "install", ADAPTER)
        created = self.settings()
        self.assertEqual(list(created), ["hooks"])
        created["hooks"]["Stop"][0]["hooks"][0]["command"] = f"python3 /old/kit/scripts/pk.py drift --hook {ADAPTER}"
        self.write(SETTINGS, json.dumps(created))
        self.assertIn("updated", run_cli("hook", self.root, "hook", "install", ADAPTER)[1])
        self.assertEqual(len(self.settings()["hooks"]["Stop"]), 1)
        run_cli("hook", self.root, "hook", "remove", ADAPTER)
        self.assertEqual(self.settings(), {})

    def test_a_file_that_is_not_json_is_left_alone(self):
        self.ignore()
        self.write(SETTINGS, "{not json")
        code, _, err = run_cli("hook", self.root, "hook", "install", ADAPTER)
        self.assertEqual(code, 1)
        self.assertIn("is not valid JSON", err)
        self.assertEqual((self.root / SETTINGS).read_text(), "{not json")


if __name__ == "__main__":
    unittest.main()
