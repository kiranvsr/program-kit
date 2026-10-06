import _paths  # noqa: F401  (must come first: puts kit/scripts on sys.path)
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pk
from common import KitError, read_json, read_yaml

KIT = Path(__file__).resolve().parent.parent
TEMPLATE_FILES = ["BRIEF.md", "checks.yaml", "environments.yaml", "CLAIM_BOUNDARIES.md", "ledger.yaml",
                  "HANDOFF.md", "controller-state.json", "rulings.md",
                  "register.yaml"]


class PkTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        (self.tmp / ".git").mkdir()  # pin the project root to the temp dir
        old = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, old)

    def run_pk(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = pk.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_init_creates_program_tree_and_refuses_twice(self):
        rc, out, err = self.run_pk("init")
        self.assertEqual((rc, err), (0, ""))
        for name in TEMPLATE_FILES:
            self.assertEqual((self.tmp / "program" / name).read_bytes(), (KIT / "templates" / name).read_bytes(), name)
        self.assertEqual(read_json(self.tmp / "program" / "controller-state.json"),
                         {"stage": {}, "queued": {}, "parked": {}, "followups": []})
        handoff = (self.tmp / "program" / "HANDOFF.md").read_text()
        self.assertIn("<!-- generated:start -->", handoff)
        self.assertIn("<!-- generated:end -->", handoff)
        headings = (self.tmp / "program" / "CLAIM_BOUNDARIES.md").read_text()
        for heading in ("Claimable in development", "Unclaimable until the named environment exists",
                        "Never claimed"):
            self.assertIn(heading, headings)
        (self.tmp / "program" / "BRIEF.md").write_text("mine\n")
        rc, out, err = self.run_pk("init")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("error: ") and "exists" in err and err.count("\n") == 1, err)
        self.assertEqual((self.tmp / "program" / "BRIEF.md").read_text(), "mine\n")

    def test_yaml_templates_have_the_spec_shapes(self):
        checks = read_yaml(KIT / "templates" / "checks.yaml")
        self.assertEqual(sorted(checks), sorted(["build", "test", "scan", "compile_check", "extra_gate",
                                                 "guarded_files"]))
        for key, value in checks.items():
            self.assertEqual(value, [], key)
        self.assertEqual(read_yaml(KIT / "templates" / "environments.yaml"), {"environments": {}})
        self.assertEqual(read_yaml(KIT / "templates" / "ledger.yaml"), {"sources": {}, "entries": []})

    def test_init_write_failure_is_one_line_and_leaves_nothing(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        self.tmp.chmod(0o555)
        self.addCleanup(self.tmp.chmod, 0o755)
        rc, out, err = self.run_pk("init")
        self.tmp.chmod(0o755)
        self.assertEqual((rc, out), (1, ""))
        self.assertTrue(err.startswith("error: ") and err.count("\n") == 1, err)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), [".git"])

    def test_init_leaves_no_staging_directory(self):
        self.run_pk("init")
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), [".git", "program"])

    def test_init_with_preset_copies_checks(self):
        if not (KIT / "presets").is_dir():
            self.skipTest("kit/presets not created yet (Task 12)")
        preset = KIT / "presets" / "checks.python.yaml"
        rc, _, err = self.run_pk("init", "--preset", "python")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual((self.tmp / "program" / "checks.yaml").read_bytes(), preset.read_bytes())

    def test_init_with_missing_preset_file_writes_nothing(self):
        with mock.patch.object(pk, "KIT_DIR", self.tmp / "fakekit"):
            (self.tmp / "fakekit").mkdir()
            rc, out, err = self.run_pk("init", "--preset", "go")
        self.assertEqual(rc, 1)
        self.assertTrue(err.startswith("error: "), err)
        self.assertFalse((self.tmp / "program").exists())

    def test_init_rejects_unknown_preset(self):
        rc, _, err = self.run_pk("init", "--preset", "cobol")
        self.assertEqual(rc, 1)
        self.assertTrue(err.startswith("error: ") and err.count("\n") == 1, err)
        self.assertFalse((self.tmp / "program").exists())

    def test_doctor_ok_after_init(self):
        self.run_pk("init")
        rc, out, err = self.run_pk("doctor")
        self.assertEqual((rc, out, err), (0, "doctor: ok\n", ""))

    def test_doctor_reports_missing_program_dir(self):
        rc, out, err = self.run_pk("doctor")
        self.assertEqual((rc, out), (1, ""))
        self.assertTrue(err.startswith("error: ") and "program/" in err and err.count("\n") == 1, err)

    def test_doctor_reports_unparseable_checks_yaml(self):
        self.run_pk("init")
        (self.tmp / "program" / "checks.yaml").write_text("a: [1, 2]\n")
        rc, out, err = self.run_pk("doctor")
        self.assertEqual((rc, out), (1, ""))
        self.assertTrue(err.startswith("error: ") and "checks.yaml" in err and err.count("\n") == 1, err)

    def test_doctor_reports_old_python(self):
        self.run_pk("init")
        with mock.patch.object(pk.sys, "version_info", (3, 10, 9, "final", 0)):
            rc, out, err = self.run_pk("doctor")
        self.assertEqual((rc, out), (1, ""))
        self.assertTrue(err.startswith("error: ") and "3.11" in err, err)

    def test_doctor_reports_missing_git(self):
        self.run_pk("init")
        with mock.patch.object(pk.shutil, "which", return_value=None):
            rc, out, err = self.run_pk("doctor")
        self.assertEqual((rc, out), (1, ""))
        self.assertTrue(err.startswith("error: ") and "git" in err, err)

    def test_unknown_command_exits_1(self):
        rc, out, err = self.run_pk("frobnicate")
        self.assertEqual((rc, out), (1, ""))
        self.assertTrue(err.startswith("error: ") and err.count("\n") == 1, err)

    def test_no_command_exits_1(self):
        rc, out, err = self.run_pk()
        self.assertEqual((rc, out), (1, ""))
        self.assertTrue(err.startswith("error: ") and err.count("\n") == 1, err)

    def test_kiterror_is_one_line_on_stderr(self):
        def boom(args):
            raise KitError("boom")

        with mock.patch.dict(pk.COMMANDS), mock.patch.dict(pk.CONFIGURERS):
            pk.register("boom", boom, help="always fails")
            rc, out, err = self.run_pk("boom")
        self.assertEqual((rc, out, err), (1, "", "error: boom\n"))

    def test_register_adds_command_with_its_own_arguments(self):
        def hello(args):
            print(f"hello {args.who}")
            return 0

        with mock.patch.dict(pk.COMMANDS), mock.patch.dict(pk.CONFIGURERS):
            pk.register("hello", hello, help="say hello", configure=lambda p: p.add_argument("who"))
            rc, out, err = self.run_pk("hello", "world")
        self.assertEqual((rc, out, err), (0, "hello world\n", ""))
        self.assertNotIn("hello", pk.COMMANDS)


if __name__ == "__main__":
    unittest.main()
