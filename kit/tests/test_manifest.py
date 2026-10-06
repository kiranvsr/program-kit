import _paths  # noqa: F401  (must come first: puts kit/scripts on sys.path)
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path

import manifest
import pk
from common import KitError

REPO_KIT = Path(__file__).resolve().parent.parent


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.kit = Path(self._tmp.name).resolve() / "kit"
        (self.kit / "sub").mkdir(parents=True)
        (self.kit / "b.txt").write_text("b\n")
        (self.kit / "a.txt").write_text("a\n")
        (self.kit / "sub" / "c.txt").write_text("c\n")

    def test_write_lists_sorted_posix_paths_and_skips_noise(self):
        (self.kit / "__pycache__").mkdir()
        (self.kit / "__pycache__" / "x.cpython-311.pyc").write_bytes(b"x")
        (self.kit / "sub" / "y.pyc").write_bytes(b"y")
        (self.kit / ".DS_Store").write_bytes(b"z")
        path = manifest.write(self.kit)
        self.assertEqual(path, self.kit / "MANIFEST.sha256")
        lines = path.read_text().splitlines()
        self.assertEqual([line.split("  ", 1)[1] for line in lines], ["a.txt", "b.txt", "sub/c.txt"])
        self.assertTrue(all(len(line.split("  ", 1)[0]) == 64 for line in lines))
        self.assertEqual(manifest.check(self.kit), [])

    def test_check_reports_modified_missing_added(self):
        manifest.write(self.kit)
        (self.kit / "a.txt").write_text("changed\n")
        (self.kit / "b.txt").unlink()
        (self.kit / "new.txt").write_text("n\n")
        self.assertEqual(manifest.check(self.kit), ["added: new.txt", "missing: b.txt", "modified: a.txt"])

    def test_check_without_manifest_or_malformed_raises(self):
        with self.assertRaises(KitError):
            manifest.check(self.kit)
        (self.kit / "MANIFEST.sha256").write_text("not a manifest line\n")
        with self.assertRaises(KitError):
            manifest.check(self.kit)

    def test_symlink_is_an_error(self):
        os.symlink("a.txt", self.kit / "link")
        with self.assertRaises(KitError):
            manifest.write(self.kit)

    def test_cli_write_and_check(self):
        def run(*argv):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = pk.main(list(argv))
            return rc, out.getvalue(), err.getvalue()

        from unittest import mock
        with mock.patch.object(manifest, "KIT_DIR", self.kit):
            self.assertEqual(run("manifest", "write"), (0, "manifest: 3 files\n", ""))
            self.assertEqual(run("manifest", "check"), (0, "manifest: clean\n", ""))
            (self.kit / "a.txt").write_text("changed\n")
            (self.kit / "z.txt").write_text("z\n")
            rc, out, err = run("manifest", "check")
        self.assertEqual((rc, out), (1, ""))
        self.assertEqual(err.splitlines(), ["error: added: z.txt", "error: modified: a.txt"])

    def test_committed_manifest_is_current(self):
        self.assertEqual(manifest.check(REPO_KIT), [])


if __name__ == "__main__":
    unittest.main()
