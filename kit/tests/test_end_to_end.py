"""End to end: the tiny-service example runs the whole loop, command by command, in a throwaway repository."""
import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
from _slow import skip_unless_slow
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import evidence as evidence_module
import gate as gate_module
import interview
import pk
import store
from common import read_json, read_yaml, run_argv, sha256_bytes, sha256_file, utc_now, write_yaml
from test_neutrality import TOKEN as FORBIDDEN, URL  # the same compiled rules the repository-wide scan uses
from test_templates import FLOW  # a flow collection with something inside

ROOT = Path(__file__).resolve().parent.parent.parent
EXAMPLE = ROOT / "examples" / "tiny-service"
TASK = "P1-M01-S01-T01"
OTHER = "P1-M01-S01-T02"
BRANCH = f"task/{TASK}"
GATE = "G-P1"
EVIDENCE = f"program/evidence/{TASK}"
RESULTS = f"{EVIDENCE}/results.json"  # the record's name in gates.yaml (a 0.1 path, which names the task)
INDEX = store.pointer_path(TASK)  # the task's pointer, the only mutable evidence file
REPORT = f"{EVIDENCE}/report.md"
STATUS = f"program/status/{TASK}.json"
HANDBACK_OK = re.compile(re.escape(f"OK: {BRANCH} passes the hand-back gate at ") + r"([0-9a-f]{40})\Z")
MERGE_OK = re.compile(re.escape(f"OK: {BRANCH} merges into main at ") + r"([0-9a-f]{40})\Z")
QA_BUGS = [  # planted by QA, not by the implementer's producer
    {"id": "qa-404-as-200", "path": "src/tiny_service.py", "find": "return 404,", "replace": "return 200,",
     "test_args": ["discover", "-s", "src"]},
    {"id": "qa-echo-renamed", "path": "src/tiny_service.py", "find": '{"echo": value}',
     "replace": '{"echoed": value}', "test_args": ["discover", "-s", "src"]},
]


def _pip_available() -> bool:
    try:
        # The interpreter running the suite is the one the gate's `python3` wrapper starts.
        return subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True).returncode == 0
    except OSError:
        return False


# The only two things this test needs that a machine may lack: a POSIX sh (install.sh and the tool wrappers are
# shell scripts) and pip (the python preset's scan step runs `python3 -m pip check`).
NEEDS_SH = unittest.skipUnless(os.name == "posix" and shutil.which("sh") is not None,
                               "needs a POSIX sh: install.sh and the tool wrappers are shell scripts")
NEEDS_PIP = unittest.skipUnless(_pip_available(), "needs pip: the scan step runs `python3 -m pip check`")


def _wrapper(directory: Path, name: str, target: str) -> None:
    path = directory / name
    path.write_text(f'#!/bin/sh\nexec "{target}" "$@"\n', encoding="utf-8")
    path.chmod(0o755)


class Project:
    """A temporary repository the kit is installed into, with every `pk` call recorded."""

    def __init__(self, base: Path, with_gitleaks: bool):
        self.base = base
        self.repo = base / "repo"
        self.scratch = base / "tmp"
        self.cache = base / "cache"
        self.scratch.mkdir()
        self.invoked: set[str] = set()
        tools = base / "tools"
        tools.mkdir()
        path = os.environ.get("PATH", "")
        # A stand-in scanner that execs `true`. The python preset's scan step runs `gitleaks detect --no-git`; this
        # test proves the gate calls the scanner and fails without it, not that the scanner finds any secret.
        _wrapper(tools, "gitleaks", "true")
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update(PATH=os.pathsep.join([str(tools), path]) if with_gitleaks else path,
                        TMPDIR=str(self.scratch), PROGRAM_KIT_CACHE=str(self.cache),
                        GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                        GIT_AUTHOR_NAME="Example Author", GIT_AUTHOR_EMAIL="author@example.invalid",
                        GIT_COMMITTER_NAME="Example Author", GIT_COMMITTER_EMAIL="author@example.invalid")
        # What the gate runs with: python3 and git only, so a real gitleaks on this machine cannot be found.
        bare = base / "bare"
        bare.mkdir()
        _wrapper(bare, "python3", sys.executable)
        _wrapper(bare, "git", shutil.which("git"))
        self.gate_env = dict(self.env, PATH=self.env["PATH"] if with_gitleaks else str(bare))

    # --- running things ------------------------------------------------------------------

    def run(self, argv, cwd=None, expect=0, env=None):
        proc = subprocess.run([str(a) for a in argv], cwd=str(cwd or self.repo), capture_output=True, text=True,
                              env=env or self.env)
        if expect is not None and proc.returncode != expect:
            raise AssertionError(f"{' '.join(map(str, argv))}: exit {proc.returncode}, expected {expect}\n"
                                 f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
        return proc

    def pk(self, *args, cwd=None, expect=0, env=None):
        cwd = Path(cwd or self.repo)
        self.invoked.add(args[0])
        return self.run([sys.executable, cwd / "kit" / "scripts" / "pk.py", *args], cwd=cwd, expect=expect, env=env)

    def git(self, *args, cwd=None):
        return self.run(["git", *args], cwd=cwd).stdout.strip()

    def commit(self, message, *paths, cwd=None):
        self.git("add", *(paths or ["-A"]), cwd=cwd)
        self.git("commit", "-q", "-m", message, cwd=cwd)
        return self.git("rev-parse", "HEAD", cwd=cwd)

    def put(self, source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    @property
    def worktree(self) -> Path:
        return self.repo / ".worktrees" / TASK.lower()

    def commit_in_worktree(self, message, *paths):
        return self.commit(message, *paths, cwd=self.worktree)

    def edit_in_worktree(self, relative, old, new):
        path = self.worktree / relative
        text = path.read_text(encoding="utf-8")
        assert text.count(old) == 1, (relative, old)
        path.write_text(text.replace(old, new), encoding="utf-8")

    def latest(self, kind="results", cwd=None):
        """(index entry, record) of the task's latest record in a checkout (default: the task worktree)."""
        return store.load_latest(cwd or self.worktree, TASK, kind)

    def handback_fails(self):
        """Run the hand-back gate expecting a refusal; returns its output lines, none of which may be an OK line."""
        gate = self.pk("gate", "handback", TASK, "--branch", BRANCH, expect=1, env=self.gate_env)
        lines = gate.stdout.splitlines()
        assert [ln for ln in lines if ln.startswith("OK: ")] == [], lines
        return lines

    # --- set up: everything up to the point where the implementer hands back ------------

    def prepare(self, plan_edit=None) -> None:
        repo = self.repo
        repo.mkdir()
        self.git("init", "-q", "-b", "main")
        (repo / "README.md").write_text("A throwaway repository for the tiny-service example.\n", encoding="utf-8")
        self.commit("initial commit")

        installed = self.run(["sh", ROOT / "install.sh", repo, "--preset", "python"])
        assert "created" in installed.stdout, installed.stdout  # the installer ran `pk init --preset python` itself
        # the installer ran `pk init --preset python` itself; the recorded `pk init` call is the refused one at the end
        self.put(EXAMPLE / "BRIEF.md", repo / "program" / "BRIEF.md")
        self.put(EXAMPLE / "checks.yaml", repo / "program" / "checks.yaml")
        self.put(EXAMPLE / "HANDOFF.md", repo / "program" / "HANDOFF.md")
        self.put(EXAMPLE / ".gitignore", repo / ".gitignore")
        self.pk("doctor")
        self.pk("manifest", "check")

        self.pk("interview", "replay", EXAMPLE / "ledger.yaml", EXAMPLE / "artifacts")
        assert "next stage: 7" in self.pk("interview", "next").stdout
        approvals = repo / "program" / "approvals"
        assert sorted(path.name for path in approvals.iterdir()) == [f"stage-{n}.json" for n in range(1, 7)]
        assert "program/adr/0001-stdlib-http.md" in read_json(approvals / "stage-4.json")["artifacts"]
        self.pk("trace", "validate", "--stage", "6")
        self.pk("register", "validate")
        self.pk("register", "assign-ids")  # every ID is already there, so nothing changes
        assert (repo / "program" / "register.yaml").read_bytes() == (EXAMPLE / "artifacts" / "register.yaml").read_bytes()
        self.pk("briefs", "generate")
        self.pk("briefs", "check")
        assert "nothing to create" in self.pk("evidence", "init").stdout  # a pointer comes with its first record
        self.commit("install the kit and replay the recorded discovery")

        # Dispatch: SPEC, PLAN, review and the in_progress status go to main as one commit, then the worktree.
        for name in ("SPEC.md", "PLAN.md", "review.md"):
            self.put(EXAMPLE / "tasks" / TASK / name, repo / "program" / "tasks" / TASK / name)
        if plan_edit is not None:  # a PLAN the branch cannot fix: the gate reads it from main
            plan = repo / "program" / "tasks" / TASK / "PLAN.md"
            plan.write_text(plan_edit(plan.read_text(encoding="utf-8")), encoding="utf-8")
        self.pk("status", "set", TASK, "in_progress", "--role", "controller")
        self.commit(f"dispatch {TASK}")
        files = set(self.git("show", "--name-only", "--format=", "HEAD").splitlines())
        assert files == {f"program/tasks/{TASK}/{n}" for n in ("SPEC.md", "PLAN.md", "review.md")} | {STATUS}, files
        wt = Path(self.pk("worktree", "new", TASK).stdout.strip())
        assert wt.resolve() == self.worktree.resolve(), wt
        assert BRANCH in self.pk("worktree", "list").stdout

        # The controller answers a question with a ruling while the implementer works.
        self.pk("rulings", "add", "--role", "controller", "--question", "Which port does the service default to?",
                "--decision", "8080 on 127.0.0.1; both can be overridden on the command line.",
                "--precedent", "Local services bind the loopback address unless a ruling says otherwise.")
        assert "Which port" in self.pk("rulings", "list").stdout
        self.pk("rulings", "validate")
        self.commit("ruling R1: default port", "program/rulings.md")

        # The implementer: the work and the evidence producer, then main merged into the branch.
        self.put(EXAMPLE / "evidence" / TASK / "produce.py", wt / EVIDENCE / "produce.py")
        shutil.copytree(EXAMPLE / "src", wt / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.commit(f"{TASK}: health and echo endpoints with tests and evidence producer", cwd=wt)
        self.git("merge", "--no-edit", "-m", "merge main into the task branch", "main", cwd=wt)
        assert self.git("status", "--porcelain", cwd=wt) == ""


def _example_files(example: Path) -> list[Path]:
    """The example's tracked files: `git ls-files` run in the directory, else (no git checkout) a walk that skips
    bytecode. A git-ignored file such as a Finder `.DS_Store` is never part of the example. A new file is read
    once it is tracked or staged (`git add`); an untracked, non-ignored one is skipped until then."""
    try:
        done = subprocess.run(["git", "ls-files", "-z", "--", "."], cwd=example, capture_output=True)
    except OSError:
        done = None
    if done is not None and done.returncode == 0 and done.stdout:
        names = [n for n in done.stdout.decode("utf-8").split("\0") if n]
        return [example / n for n in names if (example / n).is_file()]
    return [path for path in example.rglob("*") if path.is_file() and "__pycache__" not in path.parts]


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()

    @skip_unless_slow()
    @NEEDS_SH
    @NEEDS_PIP
    def test_tiny_service_runs_the_whole_loop(self):
        started = time.monotonic()
        project = Project(self.base, with_gitleaks=True)
        project.prepare()
        repo, wt = project.repo, project.worktree

        # Hand-back gate, from the main checkout: the last line is the only OK line.
        gate = project.pk("gate", "handback", TASK, "--branch", BRANCH, env=project.gate_env)
        lines = gate.stdout.splitlines()
        match = HANDBACK_OK.match(lines[-1])
        self.assertIsNotNone(match, lines[-5:])
        self.assertEqual([ln for ln in lines if ln.startswith("OK: ")], [lines[-1]])
        gated = project.git("rev-parse", BRANCH)
        self.assertEqual(match.group(1), gated)
        entry, results = project.latest()
        record = entry["path"]
        results_file = wt / record
        status = project.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=wt).stdout.splitlines()
        self.assertEqual(sorted(status), sorted([f"?? {INDEX}", f"?? {record}"]))  # the record and its pointer alone
        project.pk("evidence", "validate", results_file, cwd=wt)
        self.assertIsNone(results["supersedes"])
        self.assertEqual(results["head"], gated)
        self.assertTrue(results["planted_bugs_all_killed"] and results["all_commands_as_expected"])
        bugs = {row["id"]: row for row in results["planted_bugs"]}
        self.assertEqual(len(bugs), 5)  # four real bugs and the non-building self-test row
        for row in bugs.values():  # each row says what was planted and which commands judged it
            self.assertEqual((row["path"], row["build_argv"]),
                             ("src/tiny_service.py", ["python3", "-m", "compileall", "-q", "."]))
            self.assertEqual(row["test_argv"][:3], ["python3", "-m", "unittest"])
        self.assertEqual([i for i, row in bugs.items() if not row["killed"]], ["self-test-noncompiling"])
        self.assertTrue(any(c["derived"].get("tests", 0) >= 10 for c in results["commands"]))
        inputs = store.input_digests(results["inputs"])
        self.assertIn("src/tiny_service.py", inputs)
        self.assertIn(f"program/tasks/{TASK}/PLAN.md", inputs)

        # Evidence commit: the record, its pointer, the report and the status file, nothing else.
        project.pk("status", "set", TASK, "review", "--role", "implementer", "--evidence", record, cwd=wt)
        report = ["# Report: " + TASK, "## Status", "DONE", "## Gate", lines[-1], "## Commands"]
        report += [f"- {' '.join(c['argv'])}: exit {c['exit']}" for c in results["commands"]]
        report += ["## Planted bugs"] + [f"- {i}: builds {r['builds']}, killed {r['killed']}" for i, r in bugs.items()]
        (wt / REPORT).write_text("\n".join(report) + "\n", encoding="utf-8")
        project.commit(f"{TASK}: evidence", record, INDEX, REPORT, STATUS, cwd=wt)
        self.assertEqual(sorted(project.git("show", "--name-only", "--format=", "HEAD", cwd=wt).splitlines()),
                         sorted([record, INDEX, REPORT, STATUS]))

        # QA gets its own detached worktree at the reviewed commit and the diff as a file; the implementer's
        # worktree stays free. The acceptance is committed on the task branch, below.
        review = project.pk("worktree", "review", TASK, "--exclude", "program/tasks").stdout.splitlines()
        review_wt = Path(review[0])
        self.assertEqual(project.git("rev-parse", "HEAD", cwd=review_wt), project.git("rev-parse", BRANCH))
        diff = Path(review[1].removeprefix("diff: ")).read_text(encoding="utf-8")
        self.assertIn("src/tiny_service.py", diff)
        self.assertNotIn("program/evidence", diff)
        # what the diff leaves out is said, and the stat file still shows every path the branch changed
        self.assertIn("left out of the diff: program/evidence, program/status, program/tasks", review)
        stat = Path(review[-1].removeprefix("stat of every path: ")).read_text(encoding="utf-8")
        self.assertIn(f"commit under review: {project.git('rev-parse', BRANCH)}", stat)
        self.assertIn(f"program/evidence/{TASK}/report.md", stat)
        self.assertEqual(project.git("status", "--porcelain"), "")  # nothing tracked was touched on main

        # QA: binding, drift, its own commands and planted bugs, then the acceptance.
        bound = project.pk("gate", "binding", TASK, "--branch", BRANCH).stdout
        self.assertIn("fresh at the tip", bound)
        drift = json.loads(project.pk("stale", "--strict", cwd=wt).stdout)
        self.assertEqual((drift["checked"], drift["drifted"]), (1, 0))
        self.assertEqual(drift["tasks"][0]["state"], "fresh")
        project.pk("evidence", "validate", cwd=wt)  # names, history and pointers intact
        for path, digest in inputs.items():
            self.assertEqual(sha256_bytes(project.run(["git", "show", f"{gated}:{path}"], cwd=wt).stdout.encode()),
                             digest)
        commands = []
        plan = (wt / "program" / "tasks" / TASK / "PLAN.md").read_text(encoding="utf-8")
        for argv, expect in gate_module.parse_plan(plan):
            code, output = run_argv(argv, wt, env=project.env)
            commands.append({"label": " ".join(argv), "argv": argv, "exit": code, "expected_exit": expect,
                             "as_expected": code == expect, "derived": {}, "output_sha256": sha256_bytes(
                                 output.encode()), "output_tail": output[-2000:]})
        self.assertTrue(all(c["as_expected"] for c in commands))
        with mock.patch.object(tempfile, "tempdir", str(project.scratch)):
            qa = evidence_module.Evidence(wt, TASK)
            qa_bugs = qa.planted_bugs(QA_BUGS, [sys.executable, "-m", "compileall", "-q", "."],
                                      [sys.executable, "-m", "unittest"])
        self.assertEqual([row["id"] for row in qa_bugs if row["killed"]], [b["id"] for b in QA_BUGS])
        acceptance = {"task_id": TASK, "decision": "accepted_development",
                      "evidence_sha256": sha256_file(results_file), "source_commit": results["head"],
                      "accepted_at": utc_now(), "role": "qa", "commands": commands, "planted_bugs": qa_bugs,
                      "findings": [], "blocked_by": []}
        for key in evidence_module.required_keys("acceptance"):
            self.assertIn(key, acceptance)
        written = project.scratch / "acceptance.json"  # QA's draft, outside the repository
        written.write_text(json.dumps(acceptance, indent=2) + "\n", encoding="utf-8")
        project.pk("evidence", "accept", TASK, written, cwd=wt)
        accepted = project.latest("acceptance")[0]["path"]
        project.pk("status", "set", TASK, "done", "--role", "qa", cwd=wt)
        project.commit(f"{TASK}: QA acceptance, done", accepted, INDEX, STATUS, cwd=wt)  # one commit
        self.assertEqual(sorted(project.git("show", "--name-only", "--format=", "HEAD", cwd=wt).splitlines()),
                         sorted([accepted, INDEX, STATUS]))
        history = project.pk("evidence", "history", TASK, cwd=wt).stdout.splitlines()
        self.assertEqual([line.split("\t")[:2] for line in history], [["current", "acceptance"], ["current", "results"]])
        project.pk("status", "validate", cwd=wt)
        project.pk("gate", "binding", TASK, "--branch", BRANCH)
        self.assertEqual(project.git("status", "--porcelain", cwd=wt), "")

        # Controller: merge check, merge, one bookkeeping commit.
        check = project.pk("gate", "merge-check", "--branch", BRANCH, env=project.gate_env).stdout.splitlines()
        self.assertTrue(MERGE_OK.match(check[-1]), check[-3:])
        self.assertEqual(MERGE_OK.match(check[-1]).group(1), project.git("rev-parse", BRANCH))
        self.assertEqual([ln for ln in check if ln.startswith("OK: ")], [check[-1]])
        # the controller's independent gate run: the build, the tests and the scans, on the trial merge
        self.assertEqual([ln for ln in check if ln.startswith("step ")],
                         ["step build: ok", "step test: ok", "step scan: ok"])
        # The run is recorded in the cost log with the model it ran on; it is far from the fresh-agent threshold.
        project.pk("cost", "add", "--task", TASK, "--role", "implementer", "--model", "example-model",
                   "--tokens", "180000", "--tools", "90", "--seconds", "1500")
        self.assertEqual(project.pk("cost", "report").stdout.splitlines(),
                         [f"{TASK}\t180000 tokens\t90 tool calls\t1500 s\t1 run(s)"])
        # Both tasks are full-tier, so an integration round is refused: a full-tier task merges on its own.
        refused = project.pk("integrate", "start", "both", TASK, OTHER, expect=1)
        self.assertIn(f"{TASK} is full-tier", refused.stderr)
        # one command lands the task: merge check again, merge, claims, hand-off and one bookkeeping commit
        landed = project.pk("merge", TASK, "--trailer", "Co-Authored-By: Example Agent",
                            env=project.gate_env).stdout.splitlines()
        self.assertTrue(MERGE_OK.match(landed[0]), landed)
        self.assertLess(len(landed), 10)
        self.assertIn(f"claims: {GATE} rewritten", landed)
        self.assertEqual(project.git("status", "--porcelain"), "")
        self.assertEqual(project.git("log", "-1", "--format=%s"),
                         f"chore(program): {TASK} merged (full tier); hand-off and claims")
        self.assertIn('"tier": "full"', project.pk("status", "show", TASK).stdout)
        # finish: the task and review worktrees and the merged branch go, and nothing is forced
        # (it first names the git-ignored files the worktrees hold, and drops them only when told)
        (wt / "local.pyc").write_bytes(b"git-ignored, like the bytecode the test runs may have left\n")
        refused = project.pk("worktree", "finish", TASK, expect=1)
        self.assertIn("holds git-ignored files that would go with it", refused.stderr)
        self.assertIn("local.pyc", refused.stderr)
        self.assertTrue(wt.is_dir() and review_wt.is_dir())
        finished = project.pk("worktree", "finish", TASK, "--drop-ignored").stdout
        self.assertIn(f"removed: branch {BRANCH}", finished)
        self.assertFalse(wt.exists() or review_wt.exists())
        self.assertEqual(json.loads(project.pk("stale").stdout)["drifted"], 0)
        project.pk("doctor")  # the evidence on main: every record named by its digest, none rewritten, all pointed at
        self.assertIn(f"{TASK}\tdone", project.pk("status", "list").stdout)
        self.assertEqual(project.pk("status", "show", OTHER).stdout.count('"status": "not_started"'), 1)
        project.pk("status", "validate")
        impact = json.loads(project.pk("trace", "impact", "J2").stdout)
        self.assertEqual(impact["tasks"]["done"], [TASK])
        self.assertEqual(json.loads(project.pk("trace", "impact", "NFR-002").stdout)["tasks"]["not_started"], [OTHER])
        project.pk("trace", "sync")
        self.assertEqual(project.git("status", "--porcelain"), "")  # the synced matrix equals the recorded one

        # A change request after the freeze: create, impact, approve, apply, then amend the register approval.
        cr = project.pk("change", "create", "--title", "Say in the register that the access log omits bodies",
                        "--why", "NFR-001 forbids logging request bodies and the task text should say so.",
                        "--asked-by", "the service owner").stdout.strip()
        self.assertEqual(cr, "CR-001")
        project.pk("change", "impact", cr, "NFR-002")
        project.pk("change", "approve", cr, "--by", "the service owner")
        register_file = repo / "program" / "register.yaml"
        text = register_file.read_text(encoding="utf-8")
        self.assertIn("one access log line per request", text)
        register_file.write_text(text.replace("one access log line per request",
                                              "one access log line per request, never with the body", 1),
                                 encoding="utf-8")
        project.pk("change", "apply", cr, "--note", "task text says the log never holds the request body")
        project.pk("briefs", "check")
        self.assertIn("next stage: 6", project.pk("interview", "next").stdout)  # the approved register changed
        project.pk("interview", "amend", "6")
        self.assertIn("next stage: 7", project.pk("interview", "next").stdout)
        project.pk("interview", "validate")
        project.pk("interview", "report", "1")

        # Hand-off: the controller's state names the next task; the generated part is current.
        project.put(EXAMPLE / "controller-state.json", repo / "program" / "controller-state.json")
        self.assertIn("updated", project.pk("handoff", "update").stdout)
        project.pk("handoff", "check")
        handoff = (repo / "program" / "HANDOFF.md").read_text(encoding="utf-8")
        self.assertIn(OTHER, handoff)
        self.assertIn("- done: 1", handoff)
        self.assertIn("- not_started: 1", handoff)
        self.assertIn("kit/rules/RULES.md", handoff)

        # The gate document and its validation: dev has evidence, staging does not exist.
        project.pk("gate", "claims", GATE)
        self.assertIn("claims consistent", project.pk("gate", "validate", GATE).stdout)
        claims = {p.name: read_json(p) for p in (repo / "program" / "claims" / GATE).glob("*.json")}
        self.assertEqual(len(claims), 2)
        mine = next(doc for doc in claims.values() if doc["record"] == RESULTS)
        self.assertEqual(mine["environment"], {"dev": "evidenced", "staging": "not_evaluated"})
        other = next(doc for doc in claims.values() if doc["record"] != RESULTS)
        self.assertEqual(other["environment"], {"dev": "not_evaluated", "staging": "not_evaluated"})
        project.commit("program state after the merge: hand-off, change request, claims")
        project.pk("handoff", "check")
        self.assertEqual(project.git("status", "--porcelain"), "")

        # Work the user asks for outside the register is an ad-hoc task; a follow-up has an ID and is closed.
        self.assertEqual(project.pk("adhoc", "add", "--title", "a --port example in the README").stdout.strip(),
                         "AH-0001")
        self.assertEqual(project.pk("followups", "add", "--area", "docs", "--item", "say how to stop it").stdout.strip(),
                         "F-0001")
        project.pk("followups", "close", "F-0001", "--reason", "covered by the README", "--by", "R1")
        # One controller step in one command: a ruling, a follow-up, the hand-off and the commit.
        step = project.pk("record", "--ruling", "The access log task goes next. It stays on the full tier.",
                          "--followup", "docs=say in the README how to change the port",
                          "--now", "writing the SPEC of the access log task",
                          "--trailer", "Co-Authored-By: Example Agent").stdout.splitlines()
        self.assertEqual(step[0], "R2 recorded")
        self.assertEqual(project.git("status", "--porcelain"), "")
        self.assertEqual(project.git("log", "-1", "--format=%s"), "R2: The access log task goes next")
        handoff = (repo / "program" / "HANDOFF.md").read_text(encoding="utf-8")
        self.assertIn("- writing the SPEC of the access log task", handoff)
        self.assertIn("F-0002 docs: say in the README how to change the port", handoff)
        self.assertIn("AH-0001 a --port example in the README (ad hoc, queued)", handoff)
        self.assertIn("#### Rulings (2; next R3)", handoff)
        project.pk("handoff", "check")

        # The two remaining commands: a pin file with a local cache entry, and the kit's own manifest.
        payload = b"a tiny pinned file\n"
        digest = sha256_bytes(payload)
        (project.cache / digest).mkdir(parents=True)
        (project.cache / digest / "tiny.txt").write_bytes(payload)
        pins = project.base / "pins.json"
        pins.write_text(json.dumps([{"url": "local-only", "sha256": digest, "size": len(payload),
                                     "filename": "tiny.txt"}]), encoding="utf-8")
        self.assertIn("cached tiny.txt", project.pk("fetch", "verify", "--pins", pins).stdout)
        pins.write_text(json.dumps([{"url": "local-only", "sha256": "0" * 64, "size": 1, "filename": "gone.txt"}]),
                        encoding="utf-8")
        refused = project.pk("fetch", "verify", "--pins", pins, expect=1)
        self.assertIn("pk fetch --pins", refused.stderr)
        project.pk("manifest", "check")

        # The release fingerprint: the example's release set (two tracks) runs at main's head, the record validates,
        # and committing it breaks neither the evidence checks nor doctor.
        project.put(EXAMPLE / "release-set.yaml", repo / "program" / "release-set.yaml")
        project.commit("add the release set", "program/release-set.yaml")
        self.assertEqual(project.git("status", "--porcelain"), "")
        scratch = project.base / "fingerprint-output"
        ran = project.pk("fingerprint", "run", "--scratch", scratch, env=project.gate_env).stdout
        head12 = project.git("rev-parse", "HEAD")[:12]
        fingerprint_dir = f"program/evidence/fingerprint/{head12}"
        self.assertIn(f"fingerprint {head12}: complete true, all as expected true, 2 command(s)", ran)
        fingerprint = read_json(repo / fingerprint_dir / "fingerprint.json")
        self.assertTrue(fingerprint["complete"] and fingerprint["all_as_expected"])
        self.assertEqual({name: row["commands"] for name, row in fingerprint["tracks"].items()},
                         {"tests": ["unit"], "checks": ["compile"]})
        table = read_json(repo / fingerprint["tables"]["unittest"]["path"])
        self.assertGreaterEqual(len([name for name in table if name]), 10)
        self.assertEqual(table[""], "ok")
        self.assertEqual(fingerprint["toolchain"]["versions"][0]["line"].split()[0], "Python")
        self.assertIsNotNone(fingerprint["toolchain"]["kit_manifest_sha256"])
        self.assertTrue((scratch / "unit.log").is_file())
        project.pk("fingerprint", "validate", fingerprint_dir)
        self.assertIn("  unit\ttest\t", project.pk("fingerprint", "plan").stdout)
        project.commit("record the release fingerprint", fingerprint_dir)
        self.assertIn("1 record(s) ok", project.pk("fingerprint", "validate").stdout)
        project.pk("evidence", "validate")
        project.pk("doctor")
        project.pk("init", expect=1)  # program/ exists: init refuses to overwrite it

        pk.build_parser()  # fills pk.COMMANDS: only init and doctor register at import, the rest when it is built
        self.assertEqual(sorted(project.invoked), sorted(pk.COMMANDS))
        self.assertTrue(time.monotonic() - started < 120, "the end-to-end test took two minutes or more")

    @skip_unless_slow()
    @NEEDS_SH
    @NEEDS_PIP
    def test_gate_fails_with_missing_tool_without_gitleaks(self):
        project = Project(self.base, with_gitleaks=False)
        project.prepare()
        gate = project.pk("gate", "handback", TASK, "--branch", BRANCH, expect=1, env=project.gate_env)
        self.assertIn("FAILED: scan missing tool: gitleaks", gate.stdout)
        self.assertEqual([ln for ln in gate.stdout.splitlines() if ln.startswith("OK: ")], [])
        self.assertIsNone(project.latest())

    @skip_unless_slow()
    @NEEDS_SH
    @NEEDS_PIP
    def test_gate_refuses_a_branch_whose_unit_test_fails(self):
        project = Project(self.base, with_gitleaks=True)
        project.prepare()
        project.edit_in_worktree("src/tiny_service.py", 'return 200, {"status": "ok"}', 'return 200, {"status": "down"}')
        project.commit_in_worktree("break the health answer")
        lines = project.handback_fails()
        self.assertIn("FAILED: test python3 -m unittest discover -s src (exit 1)", lines)
        self.assertIsNone(project.latest())  # nothing was produced past the failed step
        self.assertEqual([ln for ln in lines if ln.startswith("step ")], ["step build: ok"])

    @skip_unless_slow()
    @NEEDS_SH
    @NEEDS_PIP
    def test_a_branch_that_skipped_the_gate_is_refused_at_the_merge_check(self):
        project = Project(self.base, with_gitleaks=True)
        project.prepare()
        wt = project.worktree
        # No option reads the rules from anywhere but main.
        for action in (("handback", TASK), ("merge-check",), ("binding", TASK)):
            refused = project.pk("gate", *action, "--branch", BRANCH, "--main", BRANCH, expect=1, env=project.gate_env)
            self.assertEqual((refused.stdout, refused.stderr), ("", f"error: unrecognized arguments: --main {BRANCH}\n"))
        # The implementer adds a guarded file nobody approved, never runs the gate, and types the OK line itself.
        (wt / "requirements.txt").write_text("left-pad==1.0\n", encoding="utf-8")
        gated = project.commit_in_worktree("add a dependency file")
        project.run(["python3", Path(EVIDENCE) / "produce.py"], cwd=wt, env=project.gate_env)
        record = project.latest()[0]["path"]
        project.pk("status", "set", TASK, "review", "--role", "implementer", "--evidence", record, cwd=wt)
        (wt / REPORT).write_text(f"## Gate\n\nOK: {BRANCH} passes the hand-back gate at {gated}\n", encoding="utf-8")
        project.commit_in_worktree("evidence nobody gated", record, INDEX, REPORT, STATUS)
        # Everything QA is told to run passes: the record is real and it binds.
        project.pk("evidence", "validate", wt / record, cwd=wt)
        self.assertIn(f" at {gated} bound; fresh at the tip", project.pk("gate", "binding", TASK, "--branch",
                                                                        BRANCH).stdout)
        # The controller's merge check is the independent gate run, and it refuses the branch.
        check = project.pk("gate", "merge-check", "--branch", BRANCH, expect=1, env=project.gate_env)
        self.assertEqual(check.stdout, "FAILED: guarded files changed: requirements.txt\n")
        # The same branch with a rejecting acceptance cannot be marked done either.
        acceptance = {"task_id": TASK, "decision": "rejected", "evidence_sha256": sha256_file(wt / record),
                      "source_commit": gated, "accepted_at": utc_now(), "role": "qa", "commands": [],
                      "planted_bugs": [], "findings": [{"n": 1, "severity": "critical", "location": "requirements.txt",
                                                        "fix": "remove it or get the guarded edit approved"}],
                      "blocked_by": []}
        draft = project.scratch / "rejection.json"
        draft.write_text(json.dumps(acceptance), encoding="utf-8")
        project.pk("evidence", "accept", TASK, draft, cwd=wt)  # a rejection is appended like any acceptance
        rejected = project.latest("acceptance")[0]["path"]
        done = project.pk("status", "set", TASK, "done", "--role", "qa", cwd=wt, expect=1)
        self.assertEqual(done.stderr, f"error: {TASK}: latest acceptance {rejected}: decision is 'rejected', not "
                                      "'accepted_development'\n")
        self.assertIn('"status": "review"', project.pk("status", "show", TASK, cwd=wt).stdout)

    @skip_unless_slow()
    @NEEDS_SH
    @NEEDS_PIP
    def test_a_planted_bug_no_test_catches_is_recorded_not_killed_and_refused(self):
        project = Project(self.base, with_gitleaks=True)
        project.prepare()
        producer = Path(EVIDENCE) / "produce.py"
        survivor = '    {"id": "comment-only", "path": SERVICE, "find": "a larger request body is refused",\n' \
                   '     "replace": "bigger bodies are refused"},\n'
        project.edit_in_worktree(producer.as_posix(), "PLANTED_BUGS = [\n", "PLANTED_BUGS = [\n" + survivor)
        project.commit_in_worktree("plant a bug the tests cannot see")
        lines = project.handback_fails()  # the producer appends its record; the gate then refuses it
        self.assertIn("FAILED: evidence", lines)
        self.assertIn("planted_bugs_all_killed: is not true (a planted bug survived)", lines)
        entry, _ = project.latest()
        results_file = project.worktree / entry["path"]
        project.pk("evidence", "validate", results_file, cwd=project.worktree)  # the record itself is well formed
        results = read_json(results_file)
        rows = {row["id"]: row for row in results["planted_bugs"]}
        self.assertEqual((rows["comment-only"]["builds"], rows["comment-only"]["killed"],
                          rows["comment-only"]["kill_reason"]), (True, False, "not killed"))
        self.assertFalse(results["planted_bugs_all_killed"])
        # QA's binding check refuses the same record when it is committed to the branch.
        project.commit_in_worktree("commit the record anyway", entry["path"], INDEX)
        binding = project.pk("gate", "binding", TASK, "--branch", BRANCH, expect=1)
        self.assertIn(f"error: {entry['path']}: planted_bugs_all_killed: is not true (a planted bug survived)",
                      binding.stderr)

    @skip_unless_slow()
    @NEEDS_SH
    @NEEDS_PIP
    def test_a_plan_command_whose_real_exit_differs_from_the_plan_is_refused(self):
        project = Project(self.base, with_gitleaks=True)
        project.prepare(plan_edit=lambda text: text.replace(" # expect 2\n", "\n"))  # real exit is 2, plan says 0
        lines = project.handback_fails()
        self.assertIn("FAILED: plan python3 src/tiny_service.py --port abc (exit 2)", lines)
        self.assertIsNone(project.latest())

    def test_example_ledger_validates_and_closes_all_required_topics(self):
        root = self.base / "ledger-only"
        (root / "program").mkdir(parents=True)
        shutil.copyfile(EXAMPLE / "ledger.yaml", root / "program" / "ledger.yaml")
        self.assertEqual(interview.ledger_validate(root), [])
        coverage = interview.load_coverage(root)
        entries = read_yaml(EXAMPLE / "ledger.yaml")["entries"]
        condition_tags = set(read_yaml(ROOT / "kit" / "interview" / "coverage.yaml")["condition_tags"])
        used = {tag for item in entries for tag in item["tags"]}
        self.assertEqual(used & condition_tags, set())
        not_applicable = set()
        for stage in (1, 2, 3, 4, 5, 6):
            for topic_id, state in interview.topic_states(root, stage).items():
                if state["state"] == "not_applicable":
                    not_applicable.add(topic_id)
                    self.assertFalse(coverage["stages"][stage]["topics"][topic_id]["required"])
                else:
                    self.assertEqual(state["state"], "closed", f"{topic_id}: {state}")
        self.assertEqual(not_applicable, {"T1.9", "T3.3"})
        self.assertEqual([item["id"] for item in entries if item["kind"] == "assumption"
                          and item["confirmed"] is not True], [])
        self.assertEqual([item["id"] for item in entries if item["kind"] == "question"
                          and item["id"] in interview._unanswered(entries)], [])
        self.assertEqual(interview.stopping_rule(root, 1), [])
        for stage, info in coverage["stages"].items():  # the interview stayed within each stage's question budget
            asked = [item for item in entries if item["kind"] == "question"
                     and set(item["topics"]) & set(info["topics"])]
            self.assertLessEqual(len(asked), info["budget"], f"stage {stage}")
        self.assertLessEqual(len(entries), 40)  # an example people read: as small as the coverage map allows

    def test_example_files_are_neutral_and_signoff_hashes_are_current(self):
        files = _example_files(EXAMPLE)
        self.assertTrue(files)
        for path in files:
            text = path.read_text(encoding="utf-8")
            where = path.relative_to(ROOT).as_posix()
            self.assertIsNone(FORBIDDEN.search(text), where)
            self.assertIsNone(URL.search(text), where)
            self.assertEqual([ln for ln in text.splitlines() if ln.startswith("OK: ")], [], where)
        signoff = (EXAMPLE / "artifacts" / "freeze" / "signoff.md").read_text(encoding="utf-8")
        listed = dict(m.groups() for m in re.finditer(r"`([^`]+)`\s*:\s*`([0-9a-f]{64})`", signoff))
        self.assertTrue(listed)
        for relative, digest in listed.items():
            self.assertEqual(sha256_file(EXAMPLE / "artifacts" / relative), digest, relative)
        for stage in (1, 2, 3, 4):
            for relative in interview.load_coverage(ROOT)["stages"][stage]["artifacts"]:
                self.assertIn(relative, listed)
        self.assertIn("adr/0001-stdlib-http.md", listed)

    def test_a_git_ignored_file_in_the_example_directory_is_not_scanned(self):
        planted = EXAMPLE / ".DS_Store"
        self.assertFalse(planted.exists(), "remove the stray file first")
        ignored = subprocess.run(["git", "check-ignore", "-q", ".DS_Store"], cwd=EXAMPLE).returncode
        if ignored != 0:
            self.skipTest("not a git checkout, or .DS_Store is not ignored here")
        planted.write_bytes(b"\x00\x01 not utf-8 \xff\xfe")
        try:
            self.assertNotIn(planted, _example_files(EXAMPLE))
            self.assertTrue(_example_files(EXAMPLE))
            self.test_example_files_are_neutral_and_signoff_hashes_are_current()
        finally:
            planted.unlink()

    def test_example_replay_is_current_and_its_stage_6_answers_do_not_reopen_the_freeze(self):
        root = self.base / "replayed"
        (root / "program").mkdir(parents=True)
        interview.replay(root, EXAMPLE / "ledger.yaml", EXAMPLE / "artifacts")
        self.assertEqual(interview.next_stage(root), 7)
        self.assertEqual(sha256_file(root / "program" / "ledger.yaml"), sha256_file(EXAMPLE / "ledger.yaml"))
        approvals = root / "program" / "approvals"
        self.assertEqual(sorted(p.name for p in approvals.iterdir()), [f"stage-{n}.json" for n in range(1, 7)])
        self.assertIn("frozen_entries_sha256", read_json(approvals / "stage-5.json"))
        (approvals / "stage-6.json").unlink()  # back to the moment after approval 5
        ledger = read_yaml(EXAMPLE / "ledger.yaml")
        later = set(interview.load_coverage(root)["stages"][6]["topics"])
        answers = [item for item in ledger["entries"] if set(item["topics"]) <= later]
        self.assertTrue(answers)
        earlier = [item for item in ledger["entries"] if not set(item["topics"]) & later]

        def put(entries):
            write_yaml(root / "program" / "ledger.yaml", {"sources": ledger["sources"], "entries": entries})
            return interview.next_stage(root)

        self.assertEqual(put(earlier), 6)  # the ledger as it stood when stage 5 was approved
        self.assertEqual(put(earlier + answers[:1]), 6)
        self.assertEqual(put(ledger["entries"]), 6)
        interview.approve(root, 6, interview.stage_artifacts(root, 6, interview.load_coverage(root)["stages"][6]))
        self.assertEqual(interview.next_stage(root), 7)
        (approvals / "stage-6.json").unlink()
        self.assertEqual(put([dict(earlier[0], text="another outcome"), *ledger["entries"][1:]]), 5)

    def test_no_example_yaml_comment_teaches_a_flow_collection(self):
        files = sorted(EXAMPLE.rglob("*.yaml"))
        self.assertIn(EXAMPLE / "artifacts" / "environments.yaml", files)
        for path in files:
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if line.lstrip().startswith("#"):
                    self.assertIsNone(FLOW.search(line), f"{path.relative_to(ROOT).as_posix()}:{number}: {line}")


if __name__ == "__main__":
    unittest.main()
