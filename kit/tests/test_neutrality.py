"""The kit names no vendor, model or originating project, and ships no URL (apart from a short exempt list).

Scope: every file under the repository root except the directories .git, .worktrees, .superpowers and
__pycache__ (and *.pyc and .DS_Store files). Rules, with nothing exempt from the first:
- no forbidden token (see _tokens.py), matched case-insensitively as a substring, in the text of every file and in
  every relative path (each directory and file name); a hyphen inside a name also matches a space or underscore;
- no URL of any scheme in any case (a scheme name, a colon and two slashes) outside docs/FAQ.md and the test files listed in URL_EXEMPT_FILES;
- every scanned file is valid UTF-8 and holds no zero-width or invisible character (U+00AD, U+034F, U+200B to
  U+200F, U+2060, U+FEFF), in its text or in its path.
"""
import _paths  # noqa: F401  (must come first: puts kit/scripts on sys.path)
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from _tokens import DISTINCTIVE, FORBIDDEN, ORIGIN, SHORT

ROOT = Path(__file__).resolve().parent.parent.parent
SKIP_ROOT_DIRS = {".git", ".worktrees", ".superpowers"}  # only at the repository root
SKIP_DIRS = {"__pycache__"}  # at any depth
SKIP_NAMES = {".DS_Store"}
SKIP_SUFFIXES = {".pyc"}
URL_EXEMPT_FILES = (
    "docs/FAQ.md",  # may carry links
    "kit/tests/test_fetch.py",  # the fetch tests need loopback and example URLs
)
ZERO_WIDTH = "".join(chr(code) for code in (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF))  # none written literally
INVISIBLE_CODES = (0x00AD, 0x034F, 0x200E, 0x200F)  # soft hyphen, combining grapheme joiner, direction marks
INVISIBLE_CHARS = ZERO_WIDTH + "".join(chr(code) for code in INVISIBLE_CODES)

TOKEN = re.compile("|".join(re.escape(t).replace(r"\-", "[-_ ]") for t in FORBIDDEN), re.IGNORECASE)
URL = re.compile(r"[a-z][a-z0-9+.-]*://", re.IGNORECASE)
INVISIBLE = re.compile("[" + INVISIBLE_CHARS + "]")


def _walk(root: Path):
    """(directory, kept sub-directory names, kept file names) over the scanned tree, in name order."""
    for current, dirs, names in os.walk(root):
        base = Path(current)
        at_root = base == root
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not (at_root and d in SKIP_ROOT_DIRS))
        yield base, list(dirs), sorted(n for n in names if n not in SKIP_NAMES and Path(n).suffix not in SKIP_SUFFIXES)


def scanned_files(root: Path) -> list:
    return [base / name for base, _, names in _walk(root) for name in names]


def path_problems(root: Path) -> list:
    """Every directory and file name of the scanned tree is held to the token and invisible-character rules."""
    out = []
    for base, dirs, names in _walk(root):
        for name in dirs + names:
            rel = (base / name).relative_to(root).as_posix()
            for match in TOKEN.finditer(name):
                out.append(f"{rel}: forbidden token {match.group(0)!r} in the path")
            if INVISIBLE.search(name):
                out.append(f"{rel}: invisible character in the path")
    return out


def problems(root: Path) -> list:
    out = path_problems(root)
    for path in scanned_files(root):
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_bytes().decode("utf-8")  # strict: no replacement characters
        except UnicodeDecodeError as exc:
            out.append(f"{rel}: not valid UTF-8 (byte {exc.start})")
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for match in TOKEN.finditer(line):
                out.append(f"{rel}:{number}: forbidden token {match.group(0)!r}")
            if URL.search(line) and rel not in URL_EXEMPT_FILES:
                out.append(f"{rel}:{number}: URL")
            if INVISIBLE.search(line):
                out.append(f"{rel}:{number}: zero-width or invisible character")
    return out


class NeutralityTests(unittest.TestCase):
    def test_the_repository_is_neutral(self):
        found = problems(ROOT)
        self.assertEqual(found, [], "\n".join(found))

    def test_the_scan_reaches_the_whole_tree(self):
        rels = {p.relative_to(ROOT).as_posix() for p in scanned_files(ROOT)}
        for expected in ("README.md", "LICENSE", "install.sh", "kit/playbooks/00_START.md",
                         "kit/tests/test_neutrality.py", "kit/templates/status.schema.json", "kit/scripts/pk.py",
                         "docs/DESIGN.md"):
            self.assertIn(expected, rels)
        self.assertFalse([r for r in rels if "__pycache__" in r or r.startswith((".git/", ".worktrees/"))])

    def test_the_url_exempt_files_exist(self):
        for rel in URL_EXEMPT_FILES:
            self.assertTrue((ROOT / rel).is_file(), f"{rel} is listed in URL_EXEMPT_FILES but does not exist")


class ScanBehaviourTests(unittest.TestCase):
    """The scanner itself, on a private tree: it must catch what it claims to catch."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def put(self, rel: str, text) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))

    def test_every_token_is_caught_in_every_scanned_place(self):
        for token in FORBIDDEN:
            for rel in ("README.md", "LICENSE", "install.sh", "kit/playbooks/a.md", "kit/tests/t.py",
                        "examples/x/y.md", "docs/DESIGN.md", "docs/FAQ.md", "docs/superpowers/s.md"):
                with self.subTest(token=token, rel=rel):
                    self.put(rel, f"a line\nuses {token} here\n")
                    found = problems(self.root)
                    self.assertEqual(len(found), 1, found)
                    self.assertIn(f"{rel}:2:", found[0])
                    (self.root / rel).unlink()

    def test_tokens_match_in_any_case(self):
        for token in FORBIDDEN:
            for variant in (token.lower(), token.upper()):
                with self.subTest(variant=variant):
                    self.put("README.md", f"x {variant} y\n")
                    self.assertEqual(len(problems(self.root)), 1)

    def test_distinctive_names_match_inside_words_and_glued_forms(self):
        for token in DISTINCTIVE:
            for text in (f"x{token.lower()}y", f"{token}s", f"v_{token}", f"{token}2", f"1{token}"):
                with self.subTest(text=text):
                    self.put("README.md", text + "\n")
                    self.assertEqual(len(problems(self.root)), 1)

    def test_short_names_are_caught_glued_to_anything(self):
        for token in SHORT:
            for caught in (f"{token}", f"{token.lower()}-like", f"9{token}", f"{token}_2", f"({token.upper()})",
                           f"{token}2", f"x{token}", f"{token}s", f"{token.lower()}y", f"a{token.upper()}b"):
                with self.subTest(caught=caught):
                    self.put("README.md", caught + "\n")
                    self.assertEqual(len(problems(self.root)), 1)

    def test_the_project_name_is_caught_with_a_space_or_underscore_for_its_hyphen(self):
        first, second = (ORIGIN.split("-")[0], ORIGIN.split("-")[1])
        for separator in ("-", " ", "_"):
            for text in (f"the {first}{separator}{second} repo", f"{first.upper()}{separator}{second.upper()}",
                         f"x{first}{separator}{second}y"):
                with self.subTest(text=text):
                    self.put("README.md", text + "\n")
                    self.assertEqual(len(problems(self.root)), 1)
        self.put("README.md", f"{first} {second[:3]} and {first}{second}\n")  # no separator: not the name
        self.assertEqual(problems(self.root), [])
        self.put("README.md", f"{first}, {second}\n")
        self.assertEqual(problems(self.root), [])

    def test_a_name_that_appears_only_in_a_path_is_caught(self):
        for token in FORBIDDEN:
            for rel in (f"{token}.md", f"kit/{token}/a.md", f"kit/x{token.lower()}y/b/c.txt", f"docs/{token}"):
                with self.subTest(token=token, rel=rel):
                    self.put(rel, "clean\n")
                    found = problems(self.root)
                    self.assertEqual(len(found), 1, found)
                    self.assertIn("in the path", found[0])
                    shutil.rmtree(self.root)
                    self.root.mkdir()

    def test_a_forbidden_directory_name_is_caught_even_when_empty(self):
        (self.root / "kit" / DISTINCTIVE[1]).mkdir(parents=True)
        found = problems(self.root)
        self.assertEqual(len(found), 1, found)
        self.assertIn(f"kit/{DISTINCTIVE[1]}: ", found[0])

    def test_an_invisible_character_in_a_path_is_caught(self):
        for char in INVISIBLE_CHARS:
            with self.subTest(char=hex(ord(char))):
                self.put(f"kit/a{char}b.md", "clean\n")
                found = problems(self.root)
                self.assertEqual(len(found), 1, found)
                self.assertIn("invisible character in the path", found[0])
                (self.root / f"kit/a{char}b.md").unlink()

    def test_the_skipped_directories_are_not_scanned_for_path_names_either(self):
        token = DISTINCTIVE[0]
        self.put(f".worktrees/{token}/a.md", "x\n")
        self.put(f"kit/scripts/__pycache__/{token}.txt", "x\n")
        self.put(f"kit/{token}.pyc", "x\n")
        self.assertEqual(problems(self.root), [])

    def test_a_scheme_is_caught_in_any_case_except_in_the_exempt_files(self):
        for scheme in ("https", "http", "HTTPS", "Ftp", "git+ssh", "s3"):
            for rel in ("README.md", "install.sh", "kit/templates/a.json", "kit/scripts/a.py", "docs/DESIGN.md",
                        "examples/e/a.md", "LICENSE"):
                with self.subTest(scheme=scheme, rel=rel):
                    self.put(rel, f"see {scheme}" + "://example.test/x\n")
                    self.assertEqual(len(problems(self.root)), 1)
                    (self.root / rel).unlink()
        self.put("docs/FAQ.md", "see https" + "://example.test/x and HTTP" + "://example.test/y\n")
        self.put("kit/tests/test_fetch.py", "url = 'http" + "://127.0.0.1:1/x'\n")
        self.assertEqual(problems(self.root), [])

    def test_a_url_in_a_test_file_that_is_not_listed_is_caught(self):
        self.put("kit/tests/test_other.py", "url = 'http" + "://127.0.0.1:1/x'\n")
        self.assertEqual(len(problems(self.root)), 1)

    def test_a_file_at_the_root_is_scanned(self):
        self.put("NOTICE", f"by {DISTINCTIVE[0]}\n")
        self.put(".gitignore", f"{DISTINCTIVE[1]}\n")
        self.assertEqual(len(problems(self.root)), 2)

    def test_a_file_that_is_not_utf8_is_a_problem_not_decoded_with_replacement(self):
        self.put("kit/data.bin", b"\xff\xfe\x00 ok\n")
        self.put("kit/almost.md", b"caf\xe9 \x80\n")
        found = problems(self.root)
        self.assertEqual(len(found), 2, found)
        self.assertTrue(all("not valid UTF-8" in line for line in found), found)

    def test_a_zero_width_or_invisible_character_is_a_problem(self):
        self.assertEqual(len(INVISIBLE_CHARS), 9)
        for code in (0x00AD, 0x034F, 0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2060, 0xFEFF):
            self.assertIn(chr(code), INVISIBLE_CHARS)
        for char in INVISIBLE_CHARS:
            with self.subTest(char=hex(ord(char))):
                self.put("docs/DESIGN.md", f"a{char}b\n")
                found = problems(self.root)
                self.assertEqual(len(found), 1, found)
                self.assertIn("invisible", found[0])
        self.put("docs/DESIGN.md", "plain text\n")
        self.assertEqual(problems(self.root), [])

    def test_an_invisible_character_inside_a_name_does_not_hide_it(self):
        for char in INVISIBLE_CHARS:
            name = DISTINCTIVE[0][:3] + char + DISTINCTIVE[0][3:]
            with self.subTest(char=hex(ord(char))):
                self.put("README.md", f"x {name} y\n")
                found = problems(self.root)
                self.assertEqual(len(found), 1, found)  # the character itself is the problem
                self.assertIn("invisible", found[0])

    def test_vcs_worktree_and_cache_directories_are_skipped_only_where_named(self):
        token = DISTINCTIVE[0]
        for rel in (".git/x", ".worktrees/t/a.md", ".superpowers/sdd/r.md", "kit/scripts/__pycache__/a.txt",
                    "kit/.DS_Store", "kit/scripts/a.pyc"):
            self.put(rel, f"{token}\n")
        self.assertEqual(problems(self.root), [])
        self.put("kit/.git/x", f"{token}\n")  # only the root-level ones are skipped
        self.put("docs/superpowers/a.md", f"{token}\n")  # no longer a skipped tree
        self.assertEqual(len(problems(self.root)), 2)


if __name__ == "__main__":
    unittest.main()
