import _paths  # noqa: F401  (must come first: puts kit/scripts on sys.path)
import json
import re
import tempfile
import unittest
from pathlib import Path

import common
from common import KitError


class CommonTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()

    def test_find_root_walks_up_to_program_dir(self):
        (self.tmp / "x" / "program").mkdir(parents=True)
        deep = self.tmp / "x" / "a" / "b"
        deep.mkdir(parents=True)
        self.assertEqual(common.find_root(deep), self.tmp / "x")

    def test_find_root_accepts_git_dir_and_prefers_nearest(self):
        (self.tmp / "outer" / "program").mkdir(parents=True)
        (self.tmp / "outer" / "inner" / ".git").mkdir(parents=True)
        self.assertEqual(common.find_root(self.tmp / "outer" / "inner"), self.tmp / "outer" / "inner")

    def test_find_root_raises_outside_any_project(self):
        with self.assertRaises(KitError):
            common.find_root(self.tmp)

    def test_write_yaml_is_atomic_and_round_trips(self):
        target = self.tmp / "sub" / "f.yaml"
        data = {"a": 1, "b": ["x", {"c": None}]}
        common.write_yaml(target, data)
        common.write_yaml(target, data)
        self.assertEqual(common.read_yaml(target), data)
        self.assertEqual([p.name for p in target.parent.iterdir()], ["f.yaml"])

    def test_write_failure_leaves_no_temp_file_and_keeps_old_content(self):
        target = self.tmp / "f.yaml"
        common.write_yaml(target, {"ok": 1})
        with self.assertRaises(KitError):
            common.write_yaml(target, {"bad": object()})
        self.assertEqual(common.read_yaml(target), {"ok": 1})
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["f.yaml"])

    def test_read_yaml_errors_name_the_file(self):
        bad = self.tmp / "bad.yaml"
        bad.write_text("a: 1\nb: [2]\n")
        with self.assertRaisesRegex(KitError, r"bad\.yaml.*line 2"):
            common.read_yaml(bad)
        with self.assertRaisesRegex(KitError, "missing.yaml"):
            common.read_yaml(self.tmp / "missing.yaml")

    def test_write_json_is_atomic_indented_with_trailing_newline(self):
        target = self.tmp / "f.json"
        common.write_json(target, {"a": [1, 2], "b": {"c": "d"}})
        text = target.read_text()
        self.assertEqual(text, json.dumps({"a": [1, 2], "b": {"c": "d"}}, indent=2) + "\n")
        self.assertEqual(common.read_json(target), {"a": [1, 2], "b": {"c": "d"}})
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["f.json"])

    def test_read_json_errors_are_kit_errors_naming_the_file(self):
        bad = self.tmp / "bad.json"
        bad.write_text("{nope")
        with self.assertRaisesRegex(KitError, r"bad\.json"):
            common.read_json(bad)
        with self.assertRaisesRegex(KitError, "missing.json"):
            common.read_json(self.tmp / "missing.json")

    def test_sha256_helpers_agree(self):
        f = self.tmp / "f.bin"
        f.write_bytes(b"hello")
        expected = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        self.assertEqual(common.sha256_bytes(b"hello"), expected)
        self.assertEqual(common.sha256_file(f), expected)

    def test_utc_now_format(self):
        self.assertRegex(common.utc_now(), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_run_argv_never_uses_a_shell(self):
        code, out = common.run_argv(["echo", "$HOME"], self.tmp)
        self.assertEqual((code, out.strip()), (0, "$HOME"))

    def test_run_argv_combines_streams_and_reports_exit_code(self):
        code, out = common.run_argv(["python3", "-c", "import sys; print('o'); print('e', file=sys.stderr); sys.exit(3)"], self.tmp)
        self.assertEqual(code, 3)
        self.assertIn("o", out)
        self.assertIn("e", out)

    def test_run_argv_env_cwd_and_timeout(self):
        code, out = common.run_argv(["python3", "-c", "import os; print(os.getcwd()); print(os.environ['KIT_X'])"],
                                    self.tmp, env={"KIT_X": "yes", "PATH": __import__("os").environ["PATH"]})
        self.assertEqual(code, 0)
        self.assertEqual(out.split(), [str(self.tmp), "yes"])
        code, out = common.run_argv(["python3", "-c", "import time; time.sleep(5)"], self.tmp, timeout=1)
        self.assertEqual(code, 124)
        self.assertIn("timed out", out)

    def test_run_argv_missing_executable(self):
        code, out = common.run_argv(["definitely-not-a-real-binary-xyz"], self.tmp)
        self.assertEqual(code, 127)
        self.assertIn("definitely-not-a-real-binary-xyz", out)

    def test_git_returns_stripped_stdout(self):
        common.git(self.tmp, "init", "-q")
        self.assertEqual(common.git(self.tmp, "rev-parse", "--is-inside-work-tree"), "true")

    def test_git_raises_kiterror_with_stderr(self):
        common.git(self.tmp, "init", "-q")
        with self.assertRaises(KitError) as ctx:
            common.git(self.tmp, "rev-parse", "nope")
        # "ambiguous argument" appears only in git's stderr, not in the echoed args
        self.assertRegex(str(ctx.exception), r"fatal: .*(ambiguous argument|unknown revision)")
        self.assertNotIn("\n", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
