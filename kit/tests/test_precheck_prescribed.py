"""0.6.0 (KF-42): `pk spec precheck` without a stand-in or a task branch checks each plant row's find against the
SPEC's `## Prescribed Code` blocks of its file; with the code at hand the checks run as before."""
import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import shutil
import tempfile
import unittest
from pathlib import Path

import precheck as sp
from test_precheck import A_TEST, PLAN, PLANTS, SPEC, git, write

PRESCRIBED = '''
## Prescribed Code

Lines named by a plant row are written verbatim.

### P-1 `pkg/src/a.ts` (new)

```ts
export const A = 1;
export const B = 2;
```

### P-2 The helpers (`pkg/src/`)

`c.ts`:

```ts
export const C = 1;
```

Inside `helper`, after the guard:

```ts
export const D = 4;
```

### P-3 Wiring

`src/e.ts`, after the imports:

```ts
export const E = 5;
```

## Decided (do not reopen)

```ts
export const A = 1;
```
'''
EXTRA = '''
MORE = [
    ("C1", "pkg/src/c.ts", "export const C = 1;\\n", "", "pkg", "src/a.test.ts", "checks a", "case:A one"),
    ("D1", "pkg/src/c.ts", "export const D = 4;\\n", "", "pkg", "src/a.test.ts", "checks a", "case:A one"),
    ("E1", "pkg/src/e.ts", "export const E = 5;\\n", "", "pkg", "src/a.test.ts", "checks a", "case:A one"),
    ("M1", "pkg/src/b.ts", "export const C = 1;\\n", "", "pkg", "src/a.test.ts", "checks a", "case:A one"),
    ("M2", "pkg/src/b.ts", "export const Q = 1;\\n", "", "pkg", "src/a.test.ts", "checks a", "case:A one"),
]
'''
EXTRA_ROWS = "".join(f"| {pid} | `pkg/src/x.ts` | `x` | `y` | \"checks a\" | `case:A one` | killed |\n"
                     for pid in ("C1", "D1", "E1", "M1", "M2"))


class PrescribedTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="precheck-prescribed-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        self.repo = tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        write(self.repo / "pkg/src/b.ts", "export const C = 1;\n")  # main holds b.ts, not a.ts
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "main")
        self.spec = tmp / "SPEC-X"
        write(self.spec / "tools/plants.py", PLANTS)
        write(self.spec / "PLAN.md", PLAN)
        write(self.spec / "SPEC.md", SPEC + PRESCRIBED)

    def run_check(self):
        notes = []
        return [str(f) for f in sp.precheck(self.spec, self.repo, notes=notes)], notes

    def test_finds_that_occur_once_in_their_files_prescribed_code_are_clean(self):
        found, notes = self.run_check()
        self.assertEqual(found, [])
        self.assertTrue(any("checked against the SPEC's Prescribed Code" in n for n in notes), notes)
        self.assertFalse(any("were skipped" in n for n in notes), notes)

    def test_a_find_missing_or_twice_in_the_prescribed_code_is_a_finding(self):
        (self.spec / "SPEC.md").write_text(SPEC + PRESCRIBED.replace("export const B = 2;\n", "export const B = 3;\n"
                                                                     "export const A = 1;\n"))
        found, _ = self.run_check()
        self.assertEqual(sorted(line.split(" ", 2)[2] for line in found), [
            "G1 find occurs 2 times in the prescribed code of pkg/src/a.ts (SPEC.md ## Prescribed Code; must be once)",
            "P1 find occurs 2 times in the prescribed code of pkg/src/a.ts (SPEC.md ## Prescribed Code; must be once)",
            "P2 find occurs 0 times in the prescribed code of pkg/src/a.ts (SPEC.md ## Prescribed Code; must be once)",
        ])
        self.assertTrue(all(line.startswith("PC-1 plants.py:") for line in found), found)

    def test_lead_in_paths_resolve_against_the_heading_and_files_without_a_block_are_named_once(self):
        write(self.spec / "tools/plants.py", PLANTS + EXTRA + "ALL = ALL + MORE\n")
        write(self.spec / "PLAN.md", PLAN + EXTRA_ROWS)
        found, notes = self.run_check()
        # C1 and D1 (c.ts under the P-2 folder, D1 by the block after a lead-in naming no file), E1 (a relative
        # lead-in matched by suffix) and M1 (b.ts has no block) are fine or not checkable; M2's file has no block
        self.assertEqual(found, [], found)
        self.assertEqual([n for n in notes if "not checkable" in n],
                         ["not checkable: pkg/src/b.ts has no block in the SPEC's Prescribed Code (M1, M2)"])

    def test_a_find_from_main_counts_for_a_file_whose_block_lacks_it(self):
        extra = EXTRA.replace('"pkg/src/b.ts", "export const C = 1;', '"pkg/src/c.ts", "export const K = 9;')
        write(self.spec / "tools/plants.py", PLANTS + extra + "ALL = ALL + MORE\n")
        write(self.spec / "PLAN.md", PLAN + EXTRA_ROWS)
        found, _ = self.run_check()
        self.assertEqual([line.split(" ", 2)[2] for line in found], [
            "M1 find occurs 0 times in the prescribed code of pkg/src/c.ts (SPEC.md ## Prescribed Code; must be once)"])
        write(self.repo / "pkg/src/c.ts", "export const K = 9;\n")  # an existing line of main's file
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "c on main")
        self.assertEqual(self.run_check()[0], [])

    def test_with_a_branch_the_branch_is_read_as_before(self):
        git(self.repo, "checkout", "-q", "-b", "task/SPEC-X")
        write(self.repo / "pkg/src/a.ts", "export const A = 1;\n")  # B is gone from the code, still prescribed
        write(self.repo / "pkg/src/a.test.ts", A_TEST)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "task")
        git(self.repo, "checkout", "-q", "main")
        found, notes = self.run_check()
        self.assertTrue(any("P2 find occurs 0 times in pkg/src/a.ts (must be once)" in f for f in found), found)
        self.assertFalse(any("Prescribed Code" in n for n in notes), notes)

    def test_a_spec_without_the_section_keeps_the_note(self):
        (self.spec / "SPEC.md").write_text(SPEC)
        found, notes = self.run_check()
        self.assertEqual(found, [])
        self.assertTrue(any("no standin.patch and no task branch" in n and "were skipped" in n for n in notes), notes)

    def test_the_blocks_are_read_by_file(self):
        blocks = [b for b in sp.prescribed_blocks(SPEC + PRESCRIBED) if not b["inline"]]
        self.assertEqual([(b["file"], b["resolved"]) for b in blocks],
                         [("pkg/src/a.ts", True), ("pkg/src/c.ts", True), ("pkg/src/c.ts", True),
                          ("src/e.ts", False)])
        self.assertEqual(blocks[2]["text"], "export const D = 4;\n")
        inline = [(b["file"], b["text"]) for b in sp.prescribed_blocks(SPEC + PRESCRIBED) if b["inline"]]
        self.assertEqual(inline, [("pkg/src/c.ts", "helper\n")])  # the spans after a lead-in path, in its file

    def test_a_line_prescribed_inline_in_the_prose_counts_once(self):
        spec = SPEC + PRESCRIBED.replace("Inside `helper`, after the guard:", "Then `export const F = 6;` after it:")
        (self.spec / "SPEC.md").write_text(spec)
        extra = EXTRA.replace('"export const D = 4;', '"export const F = 6;')
        write(self.spec / "tools/plants.py", PLANTS + extra + "ALL = ALL + MORE\n")
        write(self.spec / "PLAN.md", PLAN + EXTRA_ROWS)
        self.assertEqual(self.run_check()[0], [])


if __name__ == "__main__":
    unittest.main()
