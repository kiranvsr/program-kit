"""The extension interface (0.7.0, ext.py): a fixture extension that registers every point, loaded from main.

Each rule is tried against the cheapest way round it: an api the kit does not speak, code taken from a branch or the
working copy, PK_EXT_FROM honoured by a gate or a status write, a hook that loosens a kit check, an exception that is
swallowed, a command that replaces a kit command and an extension path that is not guarded or not full-tier. Without
program/extensions.yaml the kit behaves exactly as before.
"""
import _paths  # noqa: F401  (must come first: puts kit/scripts on sys.path)
from _slow import skip_unless_slow
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import adopt
import dashboard
import drift
import ext
import fingerprint as fp
import gate
import handoff
import pk
import status
import worktree
from common import KitError, read_json, sha256_bytes, write_yaml
from test_adopt import install as install_legacy, source as adoption_source
from test_status import write_register
from test_worktree import commit_all, make_repo, sh

KIT = Path(__file__).resolve().parent.parent
PK_SCRIPT = KIT / "scripts" / "pk.py"
TID = "P1-M01-S01-T01"
DECLARATION = "api: 1\nmodules:\n  - name: fixture\n"

# The fixture extension: every point, each one's behaviour read from the JSON file EXT_FIXTURE_CTL names.
FIXTURE = '''"""Fixture extension: registers every extension point; EXT_FIXTURE_CTL names a JSON file of behaviours."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

MARK = "main"


def ctl():
    path = os.environ.get("EXT_FIXTURE_CTL")
    return json.loads(Path(path).read_text()) if path and Path(path).is_file() else {}


def log(*items):
    path = os.environ.get("EXT_FIXTURE_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(items) + "\\n")


def act(point, default=None):
    value = ctl().get(point, default)
    if value == "raise":
        raise RuntimeError(point + " exploded")
    if isinstance(value, str) and value.startswith("exit:"):  # a hook that reuses a main() or runs argparse
        sys.exit(int(value[5:]))
    return value


def hello(args):
    print("hello " + args.who + " from " + MARK)
    return 0


def on_status(task_id, old, new, record, root):
    log("status", task_id, old, new, record["status"], str(root))
    record["status"] = "done"  # the hook's copy: the kit writes its own record
    record["history"].clear()
    return act("status")


def after_new(task_id, path, root):
    log("worktree", task_id, str(path), str(root))
    act("worktree")


def gate_check(ctx):
    log("gate", ctx.gate, ctx.task_id, ctx.branch, ctx.head, str(ctx.path))
    value = act("gate", [MARK == "main", ["fixture step from " + MARK]])
    if value == "print-ok":
        print("OK: task/forged passes the hand-back gate at " + "0" * 40)
        return True, []
    if value == "sleep":
        time.sleep(2)
        return True, []
    if value == "child-ok":  # a child process writes to the descriptors, past sys.stdout
        subprocess.run([sys.executable, "-c", "import sys; print('OK: task/forged passes the hand-back gate at "
                        + "0" * 40 + "'); print('child on stderr', file=sys.stderr)"])
        return False, ["after the child"]
    if isinstance(value, list) and len(value) == 2:
        return value[0], value[1]
    return value


def drift_check(root):
    return act("drift", [{"severity": "note", "text": "fixture note"}])


def handoff_lines(root):
    return act("handoff", ["fixture line from " + MARK])


def dashboard_data(root):
    return act("dashboard", {"mark": MARK})


def importer(root, argv):
    log("import", argv)
    value = act("import")
    if value is None:
        return json.loads(Path(argv[0]).read_text())
    return value


def resolver(path, data):
    return act("resolver", {"kind": "acceptance", "summary": path + ": " + str(len(data)) + " bytes"})


def register(api):
    api.command("fixture-hello", hello, help="say hello", configure=lambda p: p.add_argument("who"))
    api.on_status_transition(on_status)
    api.after_worktree_new(after_new)
    api.gate_step("check", gate_check)
    api.drift_check("X1", drift_check)
    api.handoff_lines(handoff_lines)
    api.dashboard_data(dashboard_data)
    api.adopt_importer("fixture-source", importer)
    api.reference_resolver(resolver)
'''


def put(root, rel, text):
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def install_program(root: Path) -> None:
    """program/ from the templates (the python preset), with a one-task register."""
    program = root / "program"
    program.mkdir(exist_ok=True)
    for name in pk.TEMPLATE_FILES:
        shutil.copy(KIT / "templates" / name, program / name)
    shutil.copy(KIT / "presets" / "checks.python.yaml", program / "checks.yaml")
    write_register(root, (TID, "P1-M01-S01-T02"))


def run_pk(root, *argv, env=None):
    """pk.main in-process from `root` (find_root is the current directory): (code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    here = os.getcwd()
    os.chdir(root)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.dict(os.environ, env or {}):
            code = pk.main(list(argv))
    finally:
        os.chdir(here)
    return code, out.getvalue(), err.getvalue()


class ExtBase(unittest.TestCase):
    """A repository on main with program/ and the fixture extension committed; the fixture's control file."""

    def setUp(self):
        self.addCleanup(ext.close)
        ext.close()
        self.root = make_repo(self)
        install_program(self.root)
        put(self.root, ".gitignore", ".worktrees/\n__pycache__/\n*.pyc\n")
        put(self.root, "program/extensions.yaml", DECLARATION)
        put(self.root, "program/extensions/fixture.py", FIXTURE)
        commit_all(self.root, "program and the fixture extension")
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        self.ctl_file = self.scratch / "ctl.json"
        self.log_file = self.scratch / "log.jsonl"
        patcher = mock.patch.dict(os.environ, {"EXT_FIXTURE_CTL": str(self.ctl_file),
                                               "EXT_FIXTURE_LOG": str(self.log_file)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(ext.ENV, None)

    def ctl(self, **behaviour):
        self.ctl_file.write_text(json.dumps(behaviour))

    def logged(self, kind=None):
        if not self.log_file.exists():
            return []
        rows = [json.loads(line) for line in self.log_file.read_text().splitlines()]
        return [row for row in rows if kind is None or row[0] == kind]

    def commit_main(self, message="change"):
        commit_all(self.root, message)

    def pk(self, *argv, env=None):
        return run_pk(self.root, *argv, env=env)


# --- declaration and loading ------------------------------------------------------------------------------------

class WithoutDeclarationTests(unittest.TestCase):
    """Without program/extensions.yaml the kit behaves exactly as before."""

    def setUp(self):
        self.addCleanup(ext.close)
        self.root = make_repo(self)
        install_program(self.root)
        commit_all(self.root, "program")
        os.environ.pop(ext.ENV, None)

    def test_nothing_loads_and_every_point_is_a_no_op(self):
        self.assertIsNone(ext.load(self.root))
        self.assertIsNone(ext.status_refusal(self.root, TID, "not_started", "in_progress", {}))
        self.assertIsNone(ext.after_worktree_new(self.root, TID, self.root))
        self.assertEqual(ext.steps_for(self.root, "handback"), [])
        self.assertEqual(ext.drift_results(self.root), ([], []))
        self.assertEqual((ext.handoff_lines(self.root), ext.dashboard_data(self.root)), ([], {}))
        self.assertIsNone(ext.resolve_reference(self.root, "a", b""))
        self.assertIsNone(ext.load_problem(self.root, "x"))
        self.assertEqual(ext.problems(self.root), [])

    def test_commands_status_handoff_and_dashboard_are_unchanged(self):
        code, out, err = run_pk(self.root, "ext", "list")
        self.assertEqual((code, out, err), (0, "extensions: none (no program/extensions.yaml on main)\n", ""))
        status.transition(self.root, TID, "parked", "controller", note="waits")
        handoff.update(self.root)
        block = (self.root / "program" / "HANDOFF.md").read_text()
        now = block.split("#### Now\n", 1)[1].split("\n\n", 1)[0]
        self.assertEqual(now, "- Fingerprint: none")  # the kit's own Now list, and nothing after it
        page, summary = dashboard.render(self.root)
        self.assertEqual(summary, {"tasks": 2, "gates": 0, "tokens": 0})
        self.assertNotIn("<h2>Extensions</h2>", page)
        self.assertNotIn("@@EXTENSIONS@@", page)

    def test_an_extension_folder_without_the_declaration_is_inert(self):
        put(self.root, "program/extensions/fixture.py", FIXTURE)
        commit_all(self.root, "code without a declaration")
        self.assertIsNone(ext.load(self.root))
        self.assertEqual(run_pk(self.root, "fixture-hello", "x")[0], 1)

    def test_no_main_and_no_repository_mean_no_extensions(self):
        bare = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, bare)
        self.assertIsNone(ext.load(bare))
        sh(bare, "git", "init", "-q", "-b", "main")
        put(bare, "program/extensions.yaml", "api: 2\n")
        self.assertIsNone(ext.load(bare))  # main has no commit: nothing is committed there
        self.assertEqual(run_pk(bare, "ext", "list")[0], 0)


class DeclarationTests(ExtBase):
    def declare(self, text):
        put(self.root, "program/extensions.yaml", text)
        self.commit_main("declaration")

    def test_an_api_mismatch_refuses_every_command_naming_both_versions(self):
        self.declare("api: 2\nmodules:\n  - name: fixture\n")
        for argv in (("status", "list"), ("doctor",), ("ext", "list"), ("handoff", "check"), ("fixture-hello", "x")):
            with self.subTest(argv=argv):
                code, out, err = self.pk(*argv)
                self.assertEqual(code, 1, out)
                self.assertIn("declares extension api 2, but this kit's extension api is 1", err)
        self.assertEqual(ext.API_VERSION, 1)
        with self.assertRaisesRegex(KitError, "api 2.*api is 1"):
            status.transition(self.root, TID, "parked", "controller", note="wait")

    def test_a_declaration_that_does_not_read_refuses_every_command(self):
        cases = (("api: 1\nmodules:\n  - name: Fixture\n", "must match"),
                 ("api: 1\nmodules:\n  - name: fixture\n  - name: fixture\n", "declared twice"),
                 ("api: 1\nextra: 1\n", "unknown key(s) extra"),
                 ("api: one\n", "api must be a whole number"),
                 ("api: true\n", "api must be a whole number"),
                 ("modules: []\n", "api must be a whole number"),
                 ("api: 1\nmodules:\n  - name: fixture\n    path: x\n", "the one key name"),
                 ("- 1\n", "must be a mapping"))
        for text, words in cases:
            with self.subTest(text=text):
                self.declare(text)
                code, _, err = self.pk("status", "list")
                self.assertEqual(code, 1)
                self.assertIn(words, err)

    def test_an_empty_module_list_loads_nothing_and_refuses_nothing(self):
        self.declare("api: 1\nmodules: []\n")
        registry = ext.load(self.root)
        self.assertEqual((registry.modules, registry.errors, registry.directory), ([], [], None))
        self.assertEqual(self.pk("ext", "check")[:2], (0, "ext check: ok, api 1, 0 module(s) loaded from main's "
                                                          "committed tree (nothing run)\n"))

    def test_a_bad_pk_ext_from_value_is_refused(self):
        code, _, err = self.pk("status", "list", env={ext.ENV: "branch"})
        self.assertEqual(code, 1)
        self.assertIn("PK_EXT_FROM='branch' is not understood", err)


class LoadFromMainTests(ExtBase):
    def test_the_code_is_main_s_not_the_working_copy_s_nor_a_branch_s(self):
        put(self.root, "program/extensions/fixture.py", FIXTURE.replace('MARK = "main"', 'MARK = "dirty"'))
        self.assertEqual(ext.handoff_lines(self.root), ["fixture line from main"])
        wt = worktree.new(self.root, TID)
        put(wt, "program/extensions/fixture.py", FIXTURE.replace('MARK = "main"', 'MARK = "branch"'))
        commit_all(wt, "the branch changes its extension")
        self.assertEqual(ext.handoff_lines(wt), ["fixture line from main"])  # loaded in the branch's checkout
        code, out, _ = run_pk(wt, "fixture-hello", "you")
        self.assertEqual((code, out), (0, "hello you from main\n"))

    def test_the_working_copy_is_loaded_through_a_private_directory_too(self):
        with mock.patch.dict(os.environ, {ext.ENV: ext.WORKTREE}), \
                mock.patch.object(sys, "dont_write_bytecode", False):
            registry = ext.load(self.root)
            self.assertEqual(ext.handoff_lines(self.root), ["fixture line from main"])
        self.assertFalse(str(registry.directory).startswith(str(self.root)))
        self.assertEqual(list(self.root.rglob("__pycache__")), [])

    def test_pk_ext_from_worktree_loads_the_working_copy(self):
        put(self.root, "program/extensions/fixture.py", FIXTURE.replace('MARK = "main"', 'MARK = "dirty"'))
        with mock.patch.dict(os.environ, {ext.ENV: ext.WORKTREE}):
            self.assertEqual(ext.handoff_lines(self.root), ["fixture line from dirty"])
            self.assertIn("working copy", ext.load(self.root).where())
        self.assertEqual(ext.handoff_lines(self.root), ["fixture line from main"])
        code, out, _ = self.pk("fixture-hello", "me", env={ext.ENV: ext.WORKTREE})
        self.assertEqual(out, "hello me from dirty\n")

    def test_the_code_runs_from_a_private_directory_that_is_removed_and_leaves_no_bytecode(self):
        with contextlib.chdir(self.root), mock.patch.object(sys, "dont_write_bytecode", False):
            registry = ext.load(self.root)
        directory = registry.directory
        self.assertTrue((directory / "fixture.py").is_file())
        self.assertFalse(str(directory).startswith(str(self.root)))
        self.assertTrue(registry.package.startswith("_pk_ext_"))
        self.assertIn(f"{registry.package}.fixture", sys.modules)
        self.assertNotIn("fixture", sys.modules)
        ext.close()
        self.assertFalse(directory.exists())
        self.assertEqual([name for name in sys.modules if name.startswith(registry.package)], [])
        self.assertEqual([p for p in self.root.rglob("*.pyc")] + [p for p in self.root.rglob("__pycache__")], [])

    def test_a_pk_process_leaves_no_directory_and_no_bytecode(self):
        tmp = self.scratch / "tmp"
        tmp.mkdir()
        env = dict(os.environ, TMPDIR=str(tmp))
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        proc = subprocess.run([sys.executable, str(PK_SCRIPT), "fixture-hello", "x"], cwd=str(self.root), env=env,
                              capture_output=True, text=True)
        self.assertEqual((proc.returncode, proc.stdout), (0, "hello x from main\n"), proc.stderr)
        self.assertEqual(list(tmp.iterdir()), [])
        self.assertEqual(sh(self.root, "git", "status", "--porcelain", "--ignored"), "")

    def test_a_module_named_like_a_kit_module_shadows_nothing(self):
        put(self.root, "program/extensions.yaml", "api: 1\nmodules:\n  - name: status\n")
        put(self.root, "program/extensions/status.py", "STATUSES = ()\n\ndef register(api):\n    pass\n")
        self.commit_main()
        registry = ext.load(self.root)
        self.assertEqual(registry.errors, [])
        self.assertIs(sys.modules["status"], status)
        self.assertIn("done", status.STATUSES)

    def test_a_package_with_a_relative_import_loads(self):
        put(self.root, "program/extensions.yaml", "api: 1\nmodules:\n  - name: pkg\n")
        put(self.root, "program/extensions/pkg/__init__.py", "from .lines import LINES\n\n\ndef register(api):\n"
                                                             "    api.handoff_lines(lambda root: list(LINES))\n")
        put(self.root, "program/extensions/pkg/lines.py", "LINES = ('from a package',)\n")
        self.commit_main()
        self.assertEqual(ext.handoff_lines(self.root), ["from a package"])

    def test_the_same_sources_are_not_loaded_twice(self):
        first = ext.load(self.root)
        self.assertIs(ext.load(self.root), first)
        put(self.root, "program/extensions/fixture.py", FIXTURE + "\n# changed\n")
        self.commit_main()
        second = ext.load(self.root)
        self.assertIsNot(second, first)
        self.assertFalse(first.directory.exists())

    def test_a_symlink_in_the_extension_tree_is_refused(self):
        os.symlink("fixture.py", self.root / "program" / "extensions" / "link.py")
        self.commit_main()
        with self.assertRaisesRegex(KitError, "symlink"):
            ext.load(self.root)


class ModuleErrorTests(ExtBase):
    def broken(self, code, name="fixture"):
        put(self.root, f"program/extensions/{name}.py", code)
        self.commit_main()

    def test_each_kind_of_broken_module_is_named_and_contributes_nothing(self):
        cases = (("raise ValueError('at import')\n", "ValueError: at import"),
                 ("X = 1\n", "defines no register(api)"),
                 ("def register(api):\n    api.on_status_transition(lambda *a: None)\n    raise KeyError('late')\n",
                  "KeyError"),
                 ("def register(api):\n    api.drift_check('D1', lambda root: [])\n", "a code is X<n>"),
                 ("def register(api):\n    api.gate_step('s', lambda ctx: (True, []), gates=('deploy',))\n",
                  "gates must be"),
                 ("def register(api):\n    api.handoff_lines('not callable')\n", "must be callable"),
                 ("def register(api):\n    api.dashboard_data(dict)\n    api.dashboard_data(dict)\n",
                  "dashboard_data is registered twice"),
                 ("import sys\nsys.exit(3)\n", "SystemExit"))
        for code, words in cases:
            with self.subTest(words=words):
                self.broken(code)
                registry = ext.load(self.root)
                self.assertEqual(len(registry.errors), 1, registry.errors)
                self.assertIn(words, registry.errors[0])
                self.assertEqual((registry.status_hooks, registry.drift_checks, registry.handoff), ([], [], []))

    def test_a_missing_or_doubled_module_file_is_named(self):
        (self.root / "program" / "extensions" / "fixture.py").unlink()
        self.commit_main()
        self.assertIn("no program/extensions/fixture.py", ext.load(self.root).errors[0])
        put(self.root, "program/extensions/fixture.py", FIXTURE)
        put(self.root, "program/extensions/fixture/__init__.py", FIXTURE)
        self.commit_main()
        self.assertIn("both", ext.load(self.root).errors[0])

    def test_registering_after_register_returned_is_refused(self):
        self.broken("KEEP = []\n\ndef register(api):\n    KEEP.append(api)\n")
        registry = ext.load(self.root)
        kept = sys.modules[f"{registry.package}.fixture"].KEEP[0]
        with self.assertRaisesRegex(ext.ExtensionError, "after register"):
            kept.on_status_transition(lambda *a: None)

    def test_ext_check_and_doctor_report_a_module_that_does_not_load(self):
        self.broken("raise ImportError('no such helper')\n")
        code, out, err = self.pk("ext", "check")
        self.assertEqual(code, 1)
        self.assertIn("error: extension fixture: ImportError: no such helper", err)
        code, out, err = self.pk("doctor")
        self.assertEqual(code, 1)
        self.assertIn("extension fixture: ImportError: no such helper (loaded from main's committed tree)", err)
        code, out, _ = self.pk("ext", "list")
        self.assertIn("fixture: did not load: ImportError: no such helper", out)

    def test_a_cross_module_collision_refuses_the_later_module(self):
        put(self.root, "program/extensions.yaml", DECLARATION + "  - name: other\n")
        put(self.root, "program/extensions/other.py",
            "def register(api):\n    api.drift_check('X1', lambda root: [])\n")
        self.commit_main()
        registry = ext.load(self.root)
        self.assertEqual(registry.errors, ["other: ExtensionError: drift_check X1 is already registered by fixture"])
        self.assertEqual([code for _, code, _ in registry.drift_checks], ["X1"])


class ListAndCheckTests(ExtBase):
    def test_list_names_every_point_the_fixture_registered(self):
        code, out, err = self.pk("ext", "list")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines(), [
            "extensions: api 1, 1 module(s) from main's committed tree",
            "  fixture: command fixture-hello; on_status_transition; after_worktree_new; gate_step check (handback, "
            "merge); drift_check X1; handoff_lines; dashboard_data; adopt_importer fixture-source; "
            "reference_resolver"])

    def test_check_loads_without_running_any_hook(self):
        self.ctl(status="raise", worktree="raise", gate="raise", drift="raise", handoff="raise")
        code, out, err = self.pk("ext", "check")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "ext check: ok, api 1, 1 module(s) loaded from main's committed tree (nothing run)\n")
        self.assertEqual(self.logged(), [])
        self.assertEqual(len(ext.load(self.root).modules), 1)

    def test_doctor_passes_with_a_healthy_extension(self):
        code, out, err = self.pk("doctor")
        self.assertEqual((code, out), (0, "doctor: ok\n"), err)


# --- the points -----------------------------------------------------------------------------------------------

class CommandTests(ExtBase):
    def test_an_extension_command_runs_with_its_own_arguments(self):
        self.assertEqual(self.pk("fixture-hello", "world"), (0, "hello world from main\n", ""))
        self.assertIn("fixture-hello", pk.build_parser(ext.load(self.root)).format_help())

    def test_a_command_that_collides_with_a_kit_command_is_refused_and_the_kit_command_runs(self):
        put(self.root, "program/extensions/fixture.py",
            FIXTURE.replace('api.command("fixture-hello"', 'api.command("status"'))
        self.commit_main()
        registry = ext.load(self.root)
        self.assertEqual(len(registry.errors), 1)
        self.assertIn("command status collides with a kit command", registry.errors[0])
        self.assertEqual(registry.commands, {})
        code, out, err = self.pk("status", "list")
        self.assertEqual((code, err), (0, ""))  # the kit's status list (no record yet: nothing printed)
        self.assertEqual(out, "")
        self.assertIn("status", ext.kit_commands())
        self.assertIn("ext", ext.kit_commands())

    def test_an_exception_in_a_command_is_one_error_line(self):
        put(self.root, "program/extensions/fixture.py", FIXTURE.replace(
            '    print("hello " + args.who + " from " + MARK)', '    raise OSError("disk")'))
        self.commit_main()
        code, out, err = self.pk("fixture-hello", "x")
        self.assertEqual((code, err), (1, "error: extension command fixture-hello (fixture) raised OSError: disk\n"))


class StatusHookTests(ExtBase):
    def record(self):
        path = self.root / "program" / "status" / f"{TID}.json"
        return read_json(path) if path.exists() else None

    def test_the_hook_sees_every_move_after_the_kit_checks_and_a_refusal_writes_nothing(self):
        status.transition(self.root, TID, "parked", "controller", note="waits")
        self.assertEqual(self.logged("status")[0][1:5], [TID, "not_started", "parked", "parked"])
        before = self.record()
        self.ctl(status="the programme says no")
        with self.assertRaisesRegex(KitError, f"{TID}: parked -> not_started refused by extension fixture: the "
                                              "programme says no"):
            status.transition(self.root, TID, "not_started", "controller")
        self.assertEqual(self.record(), before)

    def test_the_hook_is_not_reached_when_the_kit_refuses(self):
        with self.assertRaisesRegex(KitError, "not allowed"):
            status.transition(self.root, TID, "review", "implementer")
        self.assertEqual(self.logged("status"), [])

    def test_an_exception_in_the_hook_is_a_refusal_naming_it(self):
        self.ctl(status="raise")
        with self.assertRaisesRegex(KitError, "refused by extension fixture: on_status_transition raised "
                                              "RuntimeError: status exploded"):
            status.transition(self.root, TID, "parked", "controller", note="waits")
        self.assertIsNone(self.record())

    def test_anything_but_none_or_text_is_a_refusal_and_empty_text_too(self):
        for value, words in ((True, "returned bool, not None or a refusal string"),
                             ({"ok": True}, "returned dict"), ("", "(no reason given)")):
            with self.subTest(value=value):
                self.ctl(status=value)
                with self.assertRaisesRegex(KitError, words):
                    status.transition(self.root, TID, "parked", "controller", note="waits")
                self.assertIsNone(self.record())

    def test_the_hook_gets_a_copy_and_cannot_make_a_task_done(self):
        status.transition(self.root, TID, "parked", "controller", note="waits")  # the fixture sets its copy to done
        record = self.record()
        self.assertEqual((record["status"], len(record["history"])), ("parked", 1))
        self.ctl(status=None)
        status.transition(self.root, TID, "not_started", "controller")
        status.transition(self.root, TID, "in_progress", "controller")
        status.transition(self.root, TID, "review", "implementer")
        with self.assertRaisesRegex(KitError, "acceptance"):  # an accepting hook leaves the kit's own check standing
            status.transition(self.root, TID, "done", "qa")
        self.assertEqual(self.record()["status"], "review")

    def test_reopen_is_seen_too(self):
        self.ctl(status="no reopening")
        record = {"task_id": TID, "status": "done", "role": "qa", "updated_at": "", "note": "", "evidence": [],
                  "blocked_by": [], "history": []}
        path = self.root / "program" / "status" / f"{TID}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(KitError, "done -> review refused by extension fixture: no reopening"):
            status.reopen(self.root, TID, "late work")
        self.assertEqual(read_json(path)["status"], "done")

    def test_pk_ext_from_refuses_every_status_write(self):
        for env in ({ext.ENV: ext.WORKTREE}, {ext.ENV: "anything"}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                with self.assertRaisesRegex(KitError, "refuses to run while PK_EXT_FROM is set"):
                    status.transition(self.root, TID, "parked", "controller", note="waits")
                with self.assertRaisesRegex(KitError, "refuses to run while PK_EXT_FROM is set"):
                    status.reopen(self.root, TID, "late work")
        self.assertIsNone(self.record())
        code, out, err = self.pk("status", "set", TID, "parked", "--role", "controller", "--note", "w",
                                 env={ext.ENV: ext.WORKTREE})
        self.assertEqual(code, 1)
        self.assertIn("PK_EXT_FROM", err)

    def test_a_module_that_does_not_load_refuses_every_status_write(self):
        put(self.root, "program/extensions/fixture.py", "raise ValueError('broken')\n")
        self.commit_main()
        with self.assertRaisesRegex(KitError, "did not load .*ValueError: broken"):
            status.transition(self.root, TID, "parked", "controller", note="waits")
        self.assertIsNone(self.record())


class WorktreeHookTests(ExtBase):
    def branches(self):
        return sh(self.root, "git", "branch", "--format=%(refname:short)").split()

    def test_the_hook_runs_after_the_worktree_exists(self):
        path = worktree.new(self.root, TID)
        self.assertEqual(self.logged("worktree"), [["worktree", TID, str(path), str(self.root)]])
        self.assertTrue(path.is_dir())

    def test_a_raising_hook_removes_the_worktree_and_branch_it_made(self):
        self.ctl(worktree="raise")
        with self.assertRaisesRegex(KitError, "after_worktree_new raised RuntimeError: worktree exploded; the "
                                              "worktree .* and branch task/P1-M01-S01-T01 it made are removed"):
            worktree.new(self.root, TID)
        self.assertFalse((self.root / ".worktrees" / TID.lower()).exists())
        self.assertNotIn(f"task/{TID}", self.branches())
        self.assertEqual(len(self.logged("worktree")), 1)

    def test_a_module_that_does_not_load_refuses_before_anything_is_made(self):
        put(self.root, "program/extensions/fixture.py", "raise ValueError('broken')\n")
        self.commit_main()
        with self.assertRaisesRegex(KitError, "did not load"):
            worktree.new(self.root, TID)
        self.assertNotIn(f"task/{TID}", self.branches())


class GateStepUnitTests(ExtBase):
    """`gate.ext_steps` and `ext.run_step`: what passes, what fails, what is printed."""

    def steps(self, **behaviour):
        self.ctl(**behaviour)
        out = io.StringIO()
        checks = {"step_timeout": {"limits": {"default": 5}}}
        with contextlib.redirect_stdout(out):
            code = gate.ext_steps("merge", self.root, checks, TID, f"task/{TID}", "a" * 40, self.root,
                                  trial=self.root)
        return code, out.getvalue()

    def test_a_passing_step_prints_its_lines_with_the_tail_prefix(self):
        code, out = self.steps()
        self.assertEqual((code, out), (0, "step ext:fixture.check: ok\n    | fixture step from main\n"))
        self.assertEqual(self.logged("gate"), [["gate", "merge", TID, f"task/{TID}", "a" * 40, str(self.root)]])

    def test_not_ok_fails_and_its_lines_follow(self):
        code, out = self.steps(gate=[False, ["the programme's validator found 2 problems"]])
        self.assertEqual((code, out), (1, "FAILED: ext:fixture.check (not ok)\n"
                                          "    | the programme's validator found 2 problems\n"))

    def test_an_exception_fails_the_step_naming_it(self):
        code, out = self.steps(gate="raise")
        self.assertEqual(code, 1)
        self.assertIn("    | raised RuntimeError: gate exploded", out)

    def test_only_true_and_a_list_of_text_pass(self):
        for value in ("yes", [1, []], [True, "text"], [True, [1]], None):
            with self.subTest(value=value):
                code, out = self.steps(gate=value)
                self.assertEqual(code, 1, out)
                self.assertIn("not (ok, lines)", out)

    def test_a_step_cannot_print_an_ok_line(self):
        code, out = self.steps(gate="print-ok")
        self.assertEqual(code, 0)
        self.assertEqual([line for line in out.splitlines() if line.startswith("OK:")], [])
        self.assertIn("    | OK: task/forged", out)

    def test_a_child_process_s_output_is_captured_and_never_reaches_the_gate_s_descriptors(self):
        """A step that runs a program without capturing it: the program writes to file descriptors 1 and 2, past
        sys.stdout, so redirecting sys.stdout alone let a bare `OK:` line into the gate's output (L2)."""
        outer = self.scratch / "descriptors.out"
        with open(outer, "wb") as sink:
            sys.stdout.flush()
            sys.stderr.flush()
            kept = [os.dup(1), os.dup(2)]
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
            try:
                code, out = self.steps(gate="child-ok")
            finally:
                os.dup2(kept[0], 1)
                os.dup2(kept[1], 2)
                for fd in kept:
                    os.close(fd)
        self.assertEqual(outer.read_bytes(), b"")
        self.assertEqual(code, 1)
        lines = out.splitlines()
        self.assertEqual([line for line in lines if line.startswith("OK:")], [])
        self.assertIn(f"    | OK: task/forged passes the hand-back gate at {'0' * 40}", lines)
        self.assertIn("    | child on stderr", lines)
        self.assertIn("    | after the child", lines)

    def test_a_step_runs_under_the_step_timeout(self):
        ok, lines = ext.run_step(lambda ctx: __import__("time").sleep(1.5), None, 0.2)
        self.assertFalse(ok)
        self.assertIn("timed out after 0.2s", lines[-1])

    def test_the_steps_are_chosen_by_gate(self):
        self.assertEqual([name for _, name, _ in ext.steps_for(self.root, "handback")], ["check"])
        put(self.root, "program/extensions/fixture.py", FIXTURE.replace(
            'api.gate_step("check", gate_check)', 'api.gate_step("check", gate_check, gates=["merge"])'))
        self.commit_main()
        self.assertEqual(ext.steps_for(self.root, "handback"), [])
        self.assertEqual(len(ext.steps_for(self.root, "merge")), 1)


class GateRefusalTests(ExtBase):
    """PK_EXT_FROM and a module that does not load stop every gate before it does anything."""

    def run_gate(self, function, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = function(self.root, *args)
        return code, out.getvalue()

    def test_pk_ext_from_refuses_the_hand_back_gate_the_merge_check_and_the_binding_check(self):
        with mock.patch.dict(os.environ, {ext.ENV: ext.WORKTREE}):
            code, out = self.run_gate(gate.handback, TID, f"task/{TID}")
            self.assertEqual(code, 1)
            self.assertIn("FAILED: ext the hand-back gate refuses to run while PK_EXT_FROM is set", out)
            code, out = self.run_gate(gate.merge_check, f"task/{TID}")
            self.assertEqual(code, 1)
            self.assertIn("FAILED: ext the merge check refuses to run while PK_EXT_FROM is set", out)
            problems = gate.binding(self.root, TID, f"task/{TID}")
            self.assertEqual(len(problems), 1)
            self.assertIn("the binding check refuses to run while PK_EXT_FROM is set", problems[0])
            import land

            code, out = self.run_gate(land.round_check, "r1")
            self.assertEqual(code, 1)
            self.assertIn("pk integrate check r1 refuses to run while PK_EXT_FROM is set", out)

    def test_pk_ext_from_is_refused_without_any_extension_too(self):
        (self.root / "program" / "extensions.yaml").unlink()
        self.commit_main()
        with mock.patch.dict(os.environ, {ext.ENV: ext.WORKTREE}):
            self.assertEqual(self.run_gate(gate.merge_check, f"task/{TID}")[0], 1)

    def test_a_module_that_does_not_load_fails_the_gates_before_they_start(self):
        put(self.root, "program/extensions/fixture.py", "raise ValueError('broken')\n")
        self.commit_main()
        code, out = self.run_gate(gate.merge_check, f"task/{TID}")
        self.assertEqual(code, 1)
        self.assertIn("FAILED: ext the merge check refuses: extension module(s) did not load", out)


class SystemExitTests(ExtBase):
    """A hook that calls sys.exit (a reused main(), argparse over its own input) is a failure like an exception, at
    every point: a refusal at a refusing point, a note at an informational one; it never ends the pk command, so the
    command can never exit 0 having written nothing, or stop half-way (KX-1 fix round 1, M1). Exit 0 and exit 3 each."""

    CODES = (0, 3)

    def record(self):
        path = self.root / "program" / "status" / f"{TID}.json"
        return read_json(path) if path.exists() else None

    def test_a_status_hook_that_exits_is_a_refusal_and_writes_nothing(self):
        for code in self.CODES:
            with self.subTest(code=code):
                self.ctl(status=f"exit:{code}")
                with self.assertRaisesRegex(KitError, f"refused by extension fixture: on_status_transition raised "
                                                      f"SystemExit: {code}"):
                    status.transition(self.root, TID, "parked", "controller", note="waits")
                self.assertIsNone(self.record())
                said = self.pk("status", "set", TID, "parked", "--role", "controller", "--note", "w")
                self.assertEqual(said[0], 1, said)
                self.assertIn(f"on_status_transition raised SystemExit: {code}", said[2])
                self.assertIsNone(self.record())

    def test_an_after_worktree_hook_that_exits_removes_the_worktree_and_branch(self):
        for code in self.CODES:
            with self.subTest(code=code):
                self.ctl(worktree=f"exit:{code}")
                with self.assertRaisesRegex(KitError, f"after_worktree_new raised SystemExit: {code}; the worktree "
                                                      ".* it made are removed"):
                    worktree.new(self.root, TID)
                self.assertFalse((self.root / ".worktrees" / TID.lower()).exists())
                self.assertNotIn(f"task/{TID}", sh(self.root, "git", "branch", "--format=%(refname:short)").split())
                said = self.pk("worktree", "new", TID)
                self.assertEqual(said[0], 1, said)
                self.assertIn(f"after_worktree_new raised SystemExit: {code}", said[2])
                self.assertFalse((self.root / ".worktrees" / TID.lower()).exists())

    def test_a_gate_step_that_exits_fails_the_step(self):
        checks = {"step_timeout": {"limits": {"default": 5}}}
        for code in self.CODES:
            with self.subTest(code=code):
                self.ctl(gate=f"exit:{code}")
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc = gate.ext_steps("merge", self.root, checks, TID, f"task/{TID}", "a" * 40, self.root)
                self.assertEqual(rc, 1, out.getvalue())
                self.assertIn(f"FAILED: ext:fixture.check (not ok)\n    | raised SystemExit: {code}", out.getvalue())

    def test_a_drift_check_and_the_display_hooks_that_exit_are_drift_notes(self):
        for code in self.CODES:
            with self.subTest(code=code):
                self.ctl(drift=f"exit:{code}", handoff=f"exit:{code}", dashboard=f"exit:{code}")
                checks, notes = ext.drift_results(self.root)
                self.assertEqual(checks, [])
                self.assertIn(("X1", f"extension fixture raised SystemExit: {code}"), notes)
                self.assertIn(("ext", f"extension fixture: handoff_lines raised SystemExit: {code}"), notes)
                self.assertIn(("ext", f"extension fixture: dashboard_data failed: SystemExit: {code}"), notes)
                said = self.pk("drift")
                self.assertEqual(said[0], 0, said)
                self.assertIn(f"X1 note  extension fixture raised SystemExit: {code}", said[1])

    def test_a_hand_off_hook_that_exits_is_a_note_and_the_hand_off_is_written(self):
        for code in self.CODES:
            with self.subTest(code=code):
                self.ctl(handoff=f"exit:{code}")
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    handoff.update(self.root)
                self.assertIn(f"note: extension fixture: handoff_lines raised SystemExit: {code}", err.getvalue())
                self.assertIn("#### Now", (self.root / "program" / "HANDOFF.md").read_text())
                said = self.pk("handoff", "update")
                self.assertEqual(said[0], 0, said)
                self.assertIn(f"handoff_lines raised SystemExit: {code}", said[2])

    def test_a_dashboard_hook_that_exits_is_a_note_and_left_out(self):
        for code in self.CODES:
            with self.subTest(code=code):
                self.ctl(dashboard=f"exit:{code}")
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    page, summary = dashboard.render(self.root)
                self.assertNotIn("extensions", summary)
                self.assertIn(f"dashboard_data failed: SystemExit: {code}", err.getvalue())

    def test_a_resolver_that_exits_is_a_note_and_nothing(self):
        for code in self.CODES:
            with self.subTest(code=code):
                self.ctl(resolver=f"exit:{code}")
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertIsNone(ext.resolve_reference(self.root, "p", b""))
                self.assertIn(f"reference_resolver raised SystemExit: {code} for p", err.getvalue())

    def test_a_keyboard_interrupt_still_stops_the_command(self):
        def interrupted(*args):
            raise KeyboardInterrupt

        registry = ext.load(self.root)
        with mock.patch.object(registry, "status_hooks", [("fixture", interrupted)]):
            with self.assertRaises(KeyboardInterrupt):
                ext.status_refusal(self.root, TID, "not_started", "parked", {"status": "parked"})


class MainMovesMidGateTests(ExtBase):
    """Main can move while a gate runs (gates take minutes; merges onto main go on). A module that stops loading
    after the gate's first check fails the gate when its extension steps are due; it never passes having run none
    (KX-1 fix round 1, M2)."""

    def break_main(self):
        put(self.root, "program/extensions/fixture.py", "import nonexistent_dependency\n")
        self.commit_main("main moves on: the module no longer imports")

    def test_the_gate_s_extension_steps_fail_when_a_module_stopped_loading(self):
        self.assertTrue(gate._ext_ready(self.root, "the merge check"))  # the gate's first check passes
        self.break_main()
        out = io.StringIO()
        checks = {"step_timeout": {"limits": {"default": 5}}}
        with contextlib.redirect_stdout(out):
            code = gate.ext_steps("merge", self.root, checks, TID, f"task/{TID}", "a" * 40, self.root)
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue().splitlines()[0],
                         "FAILED: ext the merge gate's extension steps refuses: extension module(s) did not load from "
                         "main's committed tree (fixture: ModuleNotFoundError: No module named "
                         "'nonexistent_dependency'); a module that does not load may hold a refusal, so nothing it "
                         "guards runs (see: pk ext check)")
        self.assertNotIn("step ext:", out.getvalue())
        self.assertEqual(self.logged("gate"), [])

    def test_steps_for_refuses_rather_than_answering_no_steps(self):
        self.break_main()
        with self.assertRaisesRegex(KitError, "did not load .*ModuleNotFoundError"):
            ext.steps_for(self.root, "handback")

    def test_a_module_that_stopped_loading_before_the_worktree_hook_removes_the_worktree(self):
        self.break_main()
        with mock.patch.object(ext, "load_problem", return_value=None):  # as if main broke after the first check
            with self.assertRaisesRegex(KitError, "pk worktree new .* refuses: extension module\\(s\\) did not load"
                                                  ".*it made are removed"):
                worktree.new(self.root, TID)
        self.assertFalse((self.root / ".worktrees" / TID.lower()).exists())


class MainRefTests(ExtBase):
    """main is the branch refs/heads/main: a tag named main (git resolves a tag before a branch) never stands in for
    it, where the extensions are read, where the gates read checks.yaml and where the guard reads the SPEC (L1)."""

    def test_a_tag_named_main_does_not_replace_the_branch(self):
        sh(self.root, "git", "switch", "-q", "-c", f"task/{TID}")
        put(self.root, "program/extensions/fixture.py", FIXTURE + "\n# the task\n")
        commit_all(self.root, "the task changes the extension")
        sh(self.root, "git", "switch", "-q", "-c", "side", "main")
        put(self.root, "program/extensions/fixture.py", FIXTURE.replace('MARK = "main"', 'MARK = "tag"'))
        checks = (self.root / "program" / "checks.yaml").read_text()
        put(self.root, "program/checks.yaml", checks.replace("guarded_files:\n", "guarded_files:\n  - side-only.txt\n"))
        put(self.root, f"program/tasks/{TID}/SPEC.md", "# Spec\n\nGuarded edit: program/extensions/\n")
        commit_all(self.root, "a commit only a tag names")
        sh(self.root, "git", "tag", "main")
        sh(self.root, "git", "switch", "-q", "main")
        sh(self.root, "git", "branch", "-q", "-D", "side")
        self.assertEqual(ext.handoff_lines(self.root), ["fixture line from main"])
        self.assertNotIn("side-only.txt", gate.load_checks_at(self.root, gate.MAIN)["guarded_files"])
        self.assertIn("side-only.txt", gate.load_checks_at(self.root, "refs/tags/main")["guarded_files"])
        self.assertEqual(gate._guarded_changes(self.root, TID, f"task/{TID}", gate.MAIN, []),
                         ["program/extensions/fixture.py"])


class IntegrityTests(unittest.TestCase):
    """The declaration and every extension file are always guarded and always full-tier."""

    def setUp(self):
        self.root = make_repo(self)
        install_program(self.root)
        commit_all(self.root, "program")
        sh(self.root, "git", "switch", "-q", "-c", f"task/{TID}")
        put(self.root, "program/extensions.yaml", DECLARATION)
        put(self.root, "program/extensions/fixture.py", FIXTURE)
        commit_all(self.root, "the branch adds an extension")
        sh(self.root, "git", "switch", "-q", "main")

    def test_an_extension_path_is_guarded_unless_the_spec_on_main_allows_it(self):
        changed = gate._guarded_changes(self.root, TID, f"task/{TID}", "main", [])
        self.assertEqual(changed, ["program/extensions.yaml", "program/extensions/fixture.py"])
        put(self.root, f"program/tasks/{TID}/SPEC.md", "# Spec\n\nGuarded edit: program/extensions.yaml\n"
                                                       "Guarded edit: program/extensions/\n")
        commit_all(self.root, "the SPEC allows the edit")
        self.assertEqual(gate._guarded_changes(self.root, TID, f"task/{TID}", "main", []), [])

    def test_an_extension_path_is_always_full_tier(self):
        for path in ("program/extensions.yaml", "program/extensions/fixture.py", "program/extensions/pkg/a.py"):
            self.assertTrue(gate._under(path, gate.BUILTIN_FULL), path)
        meta = {"tier": "standard", "fast": False, "ruling": ""}
        checks = {"tiers": {"full_paths": ["auth/"], "light_paths": [], "light_ruling": ""}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertFalse(gate._tier_step(self.root, TID, f"task/{TID}", checks, meta))
        self.assertIn("changes full-tier paths: program/extensions.yaml, program/extensions/fixture.py",
                      out.getvalue())


class DriftTests(ExtBase):
    def report(self):
        notes = {}
        with contextlib.redirect_stderr(io.StringIO()):  # D3 renders the hand-off: a failing hook notes it there too
            _, results = drift.report(self.root, "HEAD", notes)
        return {cid: (ok, detail) for cid, ok, detail in results}, notes.get("ext", [])

    def test_a_fail_finding_drifts_and_a_note_finding_is_a_note(self):
        rows, notes = self.report()
        self.assertEqual(rows["X1"], (True, "fixture: no failing finding"))
        self.assertEqual(notes, [("X1", "fixture note")])
        self.ctl(drift=[{"severity": "fail", "text": "the frozen register changed"},
                        {"severity": "note", "text": "two legacy references"}])
        rows, notes = self.report()
        self.assertEqual(rows["X1"], (False, "the frozen register changed"))
        self.assertEqual(notes, [("X1", "two legacy references")])
        self.assertEqual(list(rows)[-1], "X1")  # after the kit's checks

    def test_a_raising_or_misshapen_check_and_failing_informational_hooks_are_notes(self):
        self.ctl(drift="raise", handoff="raise", dashboard=[1])
        rows, notes = self.report()
        self.assertNotIn("X1", rows)
        self.assertIn(("X1", "extension fixture raised RuntimeError: drift exploded"), notes)
        self.assertIn(("ext", "extension fixture: handoff_lines raised RuntimeError: handoff exploded"), notes)
        self.assertIn(("ext", "extension fixture: dashboard_data returned list, not a dict"), notes)
        self.ctl(drift=[{"severity": "bad", "text": "x"}])
        rows, notes = self.report()
        self.assertNotIn("X1", rows)
        self.assertIn("not a list of {severity: note|fail, text}", notes[0][1])

    def test_pk_drift_prints_them_and_counts_a_fail(self):
        def counted(out):
            return [line for line in out.splitlines() if line.endswith("checks drift")][0].split()

        code, clean, _ = self.pk("drift")
        self.assertIn("  X1 ok    fixture: no failing finding", clean)
        self.assertIn("  X1 note  fixture note", clean)
        self.ctl(drift=[{"severity": "fail", "text": "frozen file changed"}])
        code, out, err = self.pk("drift", "--strict")
        self.assertEqual(code, 1, out + err)
        self.assertIn("  X1 DRIFT frozen file changed", out)
        before, after = counted(clean), counted(out)
        self.assertEqual((int(after[0]), after[2]), (int(before[0]) + 1, before[2]))

    def test_a_module_that_does_not_load_is_a_note(self):
        put(self.root, "program/extensions/fixture.py", "raise ValueError('broken')\n")
        self.commit_main()
        rows, notes = self.report()
        self.assertEqual(notes, [("ext", "did not load: fixture: ValueError: broken")])


class HandoffAndDashboardTests(ExtBase):
    def test_the_lines_follow_the_kit_s_now_lines(self):
        block = handoff.render(self.root)
        now = block.split("#### Now\n", 1)[1].split("\n\n", 1)[0].splitlines()
        self.assertEqual(now[-1], "- fixture line from main")
        self.assertTrue(now[0].startswith("- Fingerprint:"))

    def test_a_failing_hook_is_a_note_and_the_hand_off_is_still_written(self):
        self.ctl(handoff="raise")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertTrue(handoff.update(self.root))
        self.assertIn("note: extension fixture: handoff_lines raised RuntimeError: handoff exploded", err.getvalue())
        self.assertNotIn("fixture line", (self.root / "program" / "HANDOFF.md").read_text())
        self.ctl(handoff=["one\nline", 7])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ext.handoff_lines(self.root), [])

    def test_the_dashboard_shows_each_module_s_dict(self):
        page, summary = dashboard.render(self.root)
        self.assertEqual(summary["extensions"], {"fixture": {"mark": "main"}})
        self.assertIn("<h2>Extensions</h2>", page)
        self.assertIn("extensions.fixture", page)
        self.ctl(dashboard={"rows": [1, 2], "set": "x"})
        self.assertEqual(dashboard.render(self.root)[1]["extensions"], {"fixture": {"rows": [1, 2], "set": "x"}})

    def test_a_failing_dashboard_hook_is_a_note_and_left_out(self):
        for value in ("raise", ["not", "a", "dict"]):
            with self.subTest(value=value):
                self.ctl(dashboard=value)
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    page, summary = dashboard.render(self.root)
                self.assertNotIn("extensions", summary)
                self.assertIn("note: extension fixture: dashboard_data", err.getvalue())


class ResolverTests(ExtBase):
    def test_the_resolver_describes_a_reference_for_display(self):
        self.assertEqual(ext.resolve_reference(self.root, "old/acceptance.json", b"{}"),
                         {"kind": "acceptance", "summary": "old/acceptance.json: 2 bytes"})

    def test_a_failing_or_misshapen_answer_is_a_note_and_nothing(self):
        for value in ("raise", {"kind": "x"}, {"kind": "x", "summary": "y", "extra": "z"}, ["x"]):
            with self.subTest(value=value):
                self.ctl(resolver=value)
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertIsNone(ext.resolve_reference(self.root, "p", b""))
                self.assertIn("note: extension fixture: reference_resolver", err.getvalue())
        self.ctl(resolver=None)
        self.assertIsNone(ext.resolve_reference(self.root, "p", b""))


class AdoptImporterTests(unittest.TestCase):
    """`pk adopt --from <name> [args...]`: the importer's dict is validated and stored exactly as a file would be."""

    def setUp(self):
        self.addCleanup(ext.close)
        os.environ.pop(ext.ENV, None)
        self.root = make_repo(self)
        install_legacy(self.root)
        put(self.root, "program/extensions.yaml", DECLARATION)
        put(self.root, "program/extensions/fixture.py", FIXTURE)
        commit_all(self.root, "install the kit and the fixture extension")
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        self.source_file = self.scratch / "export.json"  # outside the repository: the importer reads it
        self.source_file.write_text(json.dumps(adoption_source()))
        self.ctl_file = self.scratch / "ctl.json"
        patcher = mock.patch.dict(os.environ, {"EXT_FIXTURE_CTL": str(self.ctl_file),
                                               "EXT_FIXTURE_LOG": str(self.scratch / "log.jsonl")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def adopt(self, *argv):
        return run_pk(self.root, "adopt", *argv)

    def test_the_importer_s_source_is_adopted_and_stored(self):
        code, out, err = self.adopt("--from", "fixture-source", str(self.source_file))
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("adopted:", out)
        stored = (self.root / "program" / "legacy" / "adoption-source.json").read_bytes()
        self.assertEqual(json.loads(stored), adoption_source())
        self.assertEqual(stored, (json.dumps(adoption_source(), indent=2, ensure_ascii=False) + "\n").encode())
        record = read_json(self.root / "program" / "legacy" / "adoption-source.json")
        self.assertEqual(record["programme"], "sample")
        logged = [json.loads(line) for line in (self.scratch / "log.jsonl").read_text().splitlines()]
        self.assertIn(["import", [str(self.source_file)]], logged)

    def imported(self):
        log = self.scratch / "log.jsonl"
        rows = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return [row[1] for row in rows if row[0] == "import"]

    def test_a_dry_run_through_an_importer_writes_nothing(self):
        code, out, err = self.adopt("--dry-run", "--from", "fixture-source", str(self.source_file))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("dry run: nothing written", out)
        self.assertFalse((self.root / "program" / "adoption.yaml").exists())
        self.assertEqual(self.imported(), [[str(self.source_file)]])

    def test_dry_run_after_the_importer_s_arguments_is_still_the_kit_s_dry_run(self):
        """`--from` takes the rest of the line; `--dry-run` there is the kit's, not the importer's (L5)."""
        for argv in (("--from", "fixture-source", str(self.source_file), "--dry-run"),
                     ("--from", "fixture-source", "--dry-run", str(self.source_file))):
            with self.subTest(argv=argv):
                code, out, err = self.adopt(*argv)
                self.assertEqual((code, err), (0, ""), out)
                self.assertIn("dry run: nothing written", out)
                self.assertFalse((self.root / "program" / "adoption.yaml").exists())
                self.assertFalse((self.root / "program" / "legacy").exists())
                self.assertEqual(self.imported()[-1], [str(self.source_file)])
        self.assertEqual(sh(self.root, "git", "status", "--porcelain"), "")

    def test_an_importer_that_exits_is_a_refusal_and_writes_nothing(self):
        for code in (0, 3):
            with self.subTest(code=code):
                self.ctl_file.write_text(json.dumps({"import": f"exit:{code}"}))
                said = self.adopt("--from", "fixture-source")
                self.assertEqual(said[0], 1, said)
                self.assertIn(f"the importer of extension fixture raised SystemExit: {code}; nothing written",
                              said[2])
                self.assertFalse((self.root / "program" / "legacy").exists())

    def test_the_importer_s_dict_is_validated_like_a_file(self):
        self.ctl_file.write_text(json.dumps({"import": dict(adoption_source(), primary_branch="master")}))
        code, out, err = self.adopt("--from", "fixture-source")
        self.assertEqual(code, 1)
        self.assertIn("the kit's primary branch is main", err)
        self.assertFalse((self.root / "program" / "legacy").exists())

    def test_refusals(self):
        self.ctl_file.write_text(json.dumps({"import": "raise"}))
        code, _, err = self.adopt("--from", "fixture-source")
        self.assertEqual(code, 1)
        self.assertIn("the importer of extension fixture raised RuntimeError: import exploded; nothing written", err)
        self.ctl_file.write_text(json.dumps({"import": ["a", "list"]}))
        self.assertIn("returned list, not a source dict", self.adopt("--from", "fixture-source")[2])
        self.assertIn("no extension registers that importer (registered: fixture-source)",
                      self.adopt("--from", "other")[2])
        self.assertIn("needs the importer's name", self.adopt("--from")[2])
        self.assertIn("not both", self.adopt(str(self.source_file), "--from", "fixture-source")[2])
        self.assertIn("not both", self.adopt()[2])
        self.assertFalse((self.root / "program" / "legacy").exists())


class FingerprintTests(ExtBase):
    def test_the_toolchain_block_carries_the_extensions_digest_at_head(self):
        head = sh(self.root, "git", "rev-parse", "HEAD").strip()
        lines = "".join(f"{sha256_bytes(data.encode())}  {path}\n" for path, data in sorted(
            {"program/extensions.yaml": DECLARATION, "program/extensions/fixture.py": FIXTURE}.items()))
        self.assertEqual(ext.digest_at(self.root, head), sha256_bytes(lines.encode()))
        put(self.root, "program/extensions/fixture.py", FIXTURE + "#\n")
        self.assertEqual(ext.digest_at(self.root, head), sha256_bytes(lines.encode()))  # at head, not the tree
        (self.root / "program" / "extensions.yaml").unlink()
        shutil.rmtree(self.root / "program" / "extensions")
        self.commit_main()
        self.assertIsNone(ext.digest_at(self.root, "HEAD"))

    def test_a_record_with_or_without_the_digest_validates_and_a_bad_one_does_not(self):
        chain = {"kit_manifest_sha256": None, "checks_sha256": None, "versions": []}

        def toolchain_problems(value):
            record = {"schema": fp.SCHEMA, "head": "a" * 40, "head12": "a" * 12, "produced_at": "2026-01-01T00:00:00Z",
                      "only": None, "release_set_sha256": "b" * 64, "toolchain": value, "tracks": {}, "commands": [],
                      "tables": {}, "table_conflicts": [], "complete": True, "all_as_expected": True,
                      "tree_unchanged_by_run": True}
            return [p for p in fp.record_problems(record, json.dumps(record), "a" * 12) if "toolchain" in p]

        self.assertEqual(toolchain_problems(chain), [])  # a record written before 0.7.0
        self.assertEqual(toolchain_problems(dict(chain, extensions_sha256=None)), [])
        self.assertEqual(toolchain_problems(dict(chain, extensions_sha256="c" * 64)), [])
        self.assertTrue(toolchain_problems(dict(chain, extensions_sha256="nope")))
        self.assertTrue(toolchain_problems(dict(chain, other=None)))
        schema = read_json(KIT / "templates" / "fingerprint.schema.json")
        self.assertIn("extensions_sha256", schema["properties"]["toolchain"]["properties"])


# --- the gates, end to end (slow) ---------------------------------------------------------------------------------

from test_gate import BRANCH, TASK, HandbackBase, commit as gate_commit, put as gate_put  # noqa: E402


@skip_unless_slow()
class GateEndToEndTests(HandbackBase):
    """The fixture's gate step runs after the kit's steps and before the OK line, and always main's code."""

    def setUp(self):
        super().setUp()
        self.addCleanup(ext.close)
        os.environ.pop(ext.ENV, None)
        gate_put(self.root, "program/extensions.yaml", DECLARATION)
        gate_put(self.root, "program/extensions/fixture.py", FIXTURE)
        gate_commit(self.root, "the fixture extension")
        sh(self.wt, "git", "merge", "-q", "main")
        self.sha = sh(self.wt, "git", "rev-parse", "HEAD").strip()
        ctl = self.root.parent / "ctl.json"
        patcher = mock.patch.dict(os.environ, {"EXT_FIXTURE_CTL": str(ctl)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ctl_file = ctl

    def test_the_step_runs_last_in_both_gates(self):
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        lines = out.splitlines()
        step = lines.index("step ext:fixture.check: ok")
        self.assertEqual(lines[step + 1], "    | fixture step from main")
        self.assertLess(max(i for i, line in enumerate(lines) if line.startswith("step test")), step)
        self.assertTrue(lines[-1].startswith(f"OK: {BRANCH} passes the hand-back gate"))
        sh(self.wt, "git", "add", "-A")
        sh(self.wt, "git", "commit", "-q", "-m", "evidence")
        rc, out = self.merge_check()
        self.assertEqual(rc, 0, out)
        self.assertIn("step ext:fixture.check: ok", out)

    def test_a_failing_step_fails_the_gate_with_no_ok_line(self):
        self.ctl_file.write_text(json.dumps({"gate": [False, ["frozen register edited"]]}))
        rc, out = self.handback()
        self.assertEqual(rc, 1)
        self.assertIn("FAILED: ext:fixture.check (not ok)\n    | frozen register edited", out)
        rc, out = self.merge_check()
        self.assertEqual(rc, 1)
        self.assertIn("FAILED: ext:fixture.check (not ok)", out)

    def test_the_gate_runs_main_s_step_not_the_branch_s_or_the_working_copy_s(self):
        failing = FIXTURE.replace('MARK = "main"', 'MARK = "mainx"')  # main's step fails: MARK is not "main"
        gate_put(self.root, "program/extensions/fixture.py", failing)
        gate_put(self.root, f"program/tasks/{TASK}/SPEC.md", "# Spec\n\nGuarded edit: program/extensions/\n")
        gate_commit(self.root, "main's step fails; the SPEC allows the branch to edit the extension")
        sh(self.wt, "git", "merge", "-q", "main")
        gate_put(self.wt, "program/extensions/fixture.py", FIXTURE)  # the branch's version passes
        gate_commit(self.wt, "the branch restores a passing step")
        gate_put(self.root, "program/extensions/fixture.py", FIXTURE)  # and so does main's working copy
        rc, out = self.merge_check()
        self.assertEqual(rc, 1, out)
        self.assertIn("FAILED: ext:fixture.check (not ok)\n    | fixture step from mainx", out)

    def test_a_module_that_stops_loading_while_the_gate_runs_fails_it(self):
        """The reviewer's MainMovesMidGate (M2): main gains a module that does not import after the kit's steps."""
        original = gate._run_steps

        def then_main_moves(*args, **kwargs):
            said = original(*args, **kwargs)
            gate_put(self.root, "program/extensions/fixture.py", "import nonexistent_dependency\n")
            gate_commit(self.root, "main moves on while the gate runs")
            return said

        with mock.patch.object(gate, "_run_steps", then_main_moves):
            rc, out = self.handback()
        self.assertEqual(rc, 1, out)
        self.assertIn("FAILED: ext the handback gate's extension steps refuses: extension module(s) did not load "
                      "from main's committed tree (fixture: ModuleNotFoundError: No module named "
                      "'nonexistent_dependency')", out)
        self.assertNotIn("step ext:", out)
        self.assertEqual([line for line in out.splitlines() if line.startswith("OK:")], [])



from test_land import T2 as ROUND_T2, LandBase  # noqa: E402


@skip_unless_slow()
class RoundEndToEndTests(LandBase):
    """`pk integrate check` runs the merge steps once per member, on the combined tree, after the kit's suite."""

    def setUp(self):
        super().setUp()
        self.standard()
        self.wt2 = self.second_task()
        self.addCleanup(ext.close)
        os.environ.pop(ext.ENV, None)
        gate_put(self.root, "program/extensions.yaml", DECLARATION)
        gate_put(self.root, "program/extensions/fixture.py", FIXTURE)
        gate_commit(self.root, "the fixture extension")
        self.ctl_file = self.root.parent / "ctl.json"
        self.log_file = self.root.parent / "log.jsonl"
        patcher = mock.patch.dict(os.environ, {"EXT_FIXTURE_CTL": str(self.ctl_file),
                                               "EXT_FIXTURE_LOG": str(self.log_file)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_each_member_is_judged_and_a_failing_step_fails_the_round(self):
        self.assertEqual(self.pk("integrate", "start", "r1", TASK, ROUND_T2)[0], 0)
        code, out, err = self.pk("integrate", "check", "r1")
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(out.count("step ext:fixture.check: ok"), 2)
        self.assertLess(out.index("step test: ok"), out.index("step ext:fixture.check: ok"))
        rows = [json.loads(line) for line in self.log_file.read_text().splitlines()]
        round_tree = str(self.root / ".worktrees" / "round-r1")
        self.assertEqual([(row[1], row[2], row[5]) for row in rows if row[0] == "gate"],
                         [("merge", TASK, round_tree), ("merge", ROUND_T2, round_tree)])
        self.ctl_file.write_text(json.dumps({"gate": [False, ["not in this round"]]}))
        code, out, err = self.pk("integrate", "check", "r1")
        self.assertEqual(code, 1, out)
        self.assertIn("FAILED: ext:fixture.check (not ok)\n    | not in this round", out)
        self.assertNotIn("OK: ", out)


@skip_unless_slow()
class MergeEndToEndTests(LandBase):
    """`pk merge` with a hand-off hook that calls sys.exit: the merge lands and so does its bookkeeping commit; the
    hook is a note (M1: before the fix the process exited inside the hand-off update, after the merge commit)."""

    def setUp(self):
        super().setUp()
        self.standard()
        self.addCleanup(ext.close)
        os.environ.pop(ext.ENV, None)
        gate_put(self.root, "program/extensions.yaml", DECLARATION)
        gate_put(self.root, "program/extensions/fixture.py", FIXTURE)
        gate_commit(self.root, "the fixture extension")
        self.ctl_file = self.root.parent / "ctl.json"
        patcher = mock.patch.dict(os.environ, {"EXT_FIXTURE_CTL": str(self.ctl_file)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def merges_with_its_bookkeeping(self, code):
        self.ctl_file.write_text(json.dumps({"handoff": f"exit:{code}"}))
        before = self.head()
        rc, out, err = self.pk("merge", TASK)
        self.assertEqual(rc, 0, out + err)
        subjects = sh(self.root, "git", "log", "--format=%s", f"{before}..HEAD").splitlines()
        self.assertEqual(subjects[0], f"chore(program): {TASK} merged (standard tier); hand-off and claims")
        self.assertIn(f"Merge task/{TASK} (standard tier)", subjects)
        self.assertIn(f"note: extension fixture: handoff_lines raised SystemExit: {code}", err)
        self.assertIn("hand-off: updated", out)
        self.assertEqual(sh(self.root, "git", "status", "--porcelain"), "")

    def test_a_hand_off_hook_that_exits_0_never_skips_the_bookkeeping(self):
        self.merges_with_its_bookkeeping(0)

    def test_a_hand_off_hook_that_exits_3_never_skips_the_bookkeeping(self):
        self.merges_with_its_bookkeeping(3)


if __name__ == "__main__":
    unittest.main()
