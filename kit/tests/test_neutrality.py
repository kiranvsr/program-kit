"""The kit names no vendor, model or originating project, and ships no URL (apart from the FAQ's links).

Scope: kit/, examples/ (when present), docs/ except docs/superpowers/ (the design history, which names the
originating project), README.md and install.sh. A forbidden token is a case-sensitive whole word. A URL is
http:// or https://; kit/tests/ is exempt from the URL rule (the fetch tests use loopback addresses) and docs/FAQ.md
may carry links. Nothing is exempt from the token rule.
"""
import _paths  # noqa: F401  (must come first: puts kit/scripts on sys.path)
import re
import tempfile
import unittest
from pathlib import Path

from _tokens import FORBIDDEN

ROOT = Path(__file__).resolve().parent.parent.parent
SCAN_DIRS = ("kit", "examples", "docs")
SCAN_FILES = ("README.md", "install.sh")
SKIP_DIRS = {"__pycache__", ".git"}
SKIP_NAMES = {".DS_Store"}
SKIP_TREES = ("docs/superpowers",)
URL_EXEMPT_FILES = ("docs/FAQ.md",)
URL_EXEMPT_TREES = ("kit/tests",)

TOKEN = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(t) for t in FORBIDDEN) + r")(?!\w)")
URL = re.compile(r"https?://")


def under(rel: str, trees: tuple) -> bool:
    return any(rel == tree or rel.startswith(tree + "/") for tree in trees)


def scanned_files(root: Path) -> list:
    found = [root / name for name in SCAN_FILES if (root / name).is_file()]
    for name in SCAN_DIRS:
        base = root / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            rel = path.relative_to(root).as_posix()
            parts = set(path.relative_to(root).parts)
            if not path.is_file() or parts & SKIP_DIRS or path.name in SKIP_NAMES or path.suffix == ".pyc":
                continue
            if under(rel, SKIP_TREES):
                continue
            found.append(path)
    return found


def problems(root: Path) -> list:
    out = []
    for path in scanned_files(root):
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), 1):
            for match in TOKEN.finditer(line):
                out.append(f"{rel}:{number}: forbidden token {match.group(0)!r}")
            if URL.search(line) and rel not in URL_EXEMPT_FILES and not under(rel, URL_EXEMPT_TREES):
                out.append(f"{rel}:{number}: URL")
    return out


class NeutralityTests(unittest.TestCase):
    def test_the_repository_is_neutral(self):
        found = problems(ROOT)
        self.assertEqual(found, [], "\n".join(found))

    def test_the_scan_reaches_the_shipped_content(self):
        rels = {p.relative_to(ROOT).as_posix() for p in scanned_files(ROOT)}
        for expected in ("README.md", "install.sh", "kit/playbooks/00_START.md", "kit/tests/test_neutrality.py",
                         "kit/templates/status.schema.json", "kit/scripts/pk.py"):
            self.assertIn(expected, rels)
        self.assertFalse([r for r in rels if r.startswith("docs/superpowers")])
        self.assertFalse([r for r in rels if "__pycache__" in r])


class ScanBehaviourTests(unittest.TestCase):
    """The scanner itself, on a private tree: it must catch what it claims to catch."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def put(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_every_token_is_caught_in_every_scanned_place(self):
        for token in FORBIDDEN:
            for rel in ("README.md", "install.sh", "kit/playbooks/a.md", "kit/tests/t.py", "examples/x/y.md",
                        "docs/DESIGN.md", "docs/FAQ.md"):
                with self.subTest(token=token, rel=rel):
                    self.put(rel, f"a line\nuses {token} here\n")
                    found = problems(self.root)
                    self.assertEqual(len(found), 1, found)
                    self.assertIn(f"{rel}:2:", found[0])
                    (self.root / rel).unlink()

    def test_tokens_are_case_sensitive_whole_words(self):
        first = FORBIDDEN[0]
        self.put("README.md", f"{first.lower()} and {first.upper()} and {first}s and x{first} and {first}-like\n")
        self.assertEqual(len(problems(self.root)), 1)  # only "<first>-like": a hyphen ends the word
        self.put("README.md", f"{first.lower()} {first.upper()} {first}s x{first} {first}x\n")
        self.assertEqual(problems(self.root), [])

    def test_a_url_is_caught_except_in_the_faq_and_the_tests(self):
        for rel in ("README.md", "install.sh", "kit/templates/a.json", "kit/scripts/a.py", "docs/DESIGN.md",
                    "examples/e/a.md"):
            with self.subTest(rel=rel):
                self.put(rel, "see https://example.test/x\n")
                self.assertEqual(len(problems(self.root)), 1)
                (self.root / rel).unlink()
        self.put("docs/FAQ.md", "see https://example.test/x and http://example.test/y\n")
        self.put("kit/tests/t.py", "url = 'http://127.0.0.1:1/x'\n")
        self.assertEqual(problems(self.root), [])

    def test_the_design_history_and_caches_are_skipped(self):
        first = FORBIDDEN[0]
        self.put("docs/superpowers/specs/s.md", f"{first} https://example.test\n")
        self.put("kit/scripts/__pycache__/a.txt", f"{first}\n")
        self.put("kit/.DS_Store", f"{first}\n")
        self.assertEqual(problems(self.root), [])
        self.put("docs/superpowers.md", f"{first}\n")  # a sibling that merely shares the prefix is scanned
        self.assertEqual(len(problems(self.root)), 1)


if __name__ == "__main__":
    unittest.main()
