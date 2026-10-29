"""The shipped playbooks, role briefs and rules: structure, links, command names and neutrality."""
import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import re
import unittest
from pathlib import Path

import pk
from _tokens import FORBIDDEN

KIT = Path(__file__).resolve().parent.parent
PLAYBOOK_DIR = KIT / "playbooks"
BRIEF_DIR = KIT / "briefs"
RULES_DIR = KIT / "rules"
RUBRIC_DIR = KIT / "rubrics"

PLAYBOOKS = ["00_START.md", "01_discover.md", "02_define_product.md", "03_specify.md", "04_architect.md",
             "05_freeze_review.md", "06_register.md", "07_task_specs.md", "08_execute.md", "09_handoff.md",
             "10_change.md"]
LENS_PLAYBOOKS = PLAYBOOKS[1:7]  # stages 1 to 6 are interview stages and name the lenses that are active
BRIEFS = ["controller.md", "implementer.md", "qa.md", "spec_qa.md"]
RULES = ["RULES.md", "RULINGS_SEED.md"]  # one canonical rules file, and the seed rulings that point to it
RULE_PARTS = ["Part 1. Hard rules", "Part 2. Working rules", "Part 3. Evidence honesty",
              "Part 4. The controller's rules"]

PLAYBOOK_SECTIONS = ["Purpose", "Inputs", "Steps", "Outputs", "Done when", "Next playbook"]
BRIEF_SECTIONS = ["Role", "Read first", "What you do", "Needs context", "Report shape", "Never"]
# The first playbook file each playbook's "Next playbook" section names (00 lists them all, 01 first).
EXPECTED_NEXT = {name: PLAYBOOKS[i + 1] for i, name in enumerate(PLAYBOOKS[:9])}
EXPECTED_NEXT["09_handoff.md"] = "08_execute.md"
EXPECTED_NEXT["10_change.md"] = "08_execute.md"
NEEDS_CONTEXT = ["a new dependency", "a security boundary with no precedent", "a change to user scope",
                 "two conflicting rulings"]
HANDBACK_LINE = "`OK: <branch> passes the hand-back gate at <sha>`"
MERGE_LINE = "`OK: <branch> merges into main at <sha>`"
ROUND_LINE = "`OK: round/<NAME> passes the combined check at <sha>`"


# FORBIDDEN comes from _tokens (assembled from fragments); these further names are fragments too, so this file
# does not itself contain a forbidden token.
OTHER_NAMES = ["co" + "pilot", "ll" + "ama", "mist" + "ral", "deep" + "seek", "qw" + "en", "x" + "ai\\b"]
FORBIDDEN_RE = re.compile("|".join([*map(re.escape, FORBIDDEN), *OTHER_NAMES]), re.I)
URL = re.compile(r"[a-z][a-z0-9+.-]*" + ":" + "//|www\\.", re.I)  # no URL written literally here
PLAYBOOK_FILE = re.compile(r"\b\d\d_[a-z_]+\.md\b")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def sections(text: str) -> dict:
    """Level-2 headings mapped to their body text."""
    found, name, body = {}, None, []
    for line in text.splitlines():
        if line.startswith("## "):
            if name is not None:
                found[name] = "\n".join(body)
            name, body = line[3:].strip(), []
        elif name is not None:
            body.append(line)
    if name is not None:
        found[name] = "\n".join(body)
    return found


def rule_part(number: int) -> str:
    """The text of one part of the canonical rules file."""
    found = sections(read(RULES_DIR / "RULES.md"))
    return next(body for name, body in found.items() if name.startswith(RULE_PARTS[number - 1]))


HARD_RULE_LINES = (
    "1. Never fabricate evidence, test results, CI results, gate passes or human approvals.",
    "6. Only QA sets a task `done`, and only through `pk status set <ID> done --role qa`, after `pk evidence accept` "
    "appended an acceptance of the latest results record.",
    "11. Never rewrite history: no rebase, no reset, no force-push. Merge main into your branch with a merge commit.",
    "14. Never push, deploy or apply infrastructure unless the user explicitly tells you to.",
    "18. If an action is denied, stop and report it to the user. Never route around a denial, and never ask another "
    "agent to do what was denied.",
    "21. Accepted branches merge into main one at a time.",
    "22. Never force-remove a worktree that holds work.",
    "23. A ruling is never rewritten, renumbered or deleted.",
)


RUBRICS = {"tenancy-security.md": ("TS", 9), "failure-modes.md": ("FM", 8), "challenger.md": ("CH", 4)}
DROPPED_LENSES = ("zoom-out", "comprehension", "plant-rigour", "readiness", "synthesizer")
LENS_CLASSES = ("SAME", "NEW-HIGH", "NEW-LOW", "FALSE", "REJECT")


def all_files() -> list:
    return ([PLAYBOOK_DIR / n for n in PLAYBOOKS] + [BRIEF_DIR / n for n in BRIEFS] + [RULES_DIR / n for n in RULES]
            + [BRIEF_DIR / "lens.md", RUBRIC_DIR / "README.md"] + [RUBRIC_DIR / n for n in RUBRICS])


def command_parsers() -> dict:
    """{command: (parser, {action: parser})} from the real argument parser."""
    import argparse

    top = pk.build_parser()
    found = {}
    for action in top._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, parser in action.choices.items():
                nested = {}
                for inner in parser._actions:
                    if isinstance(inner, argparse._SubParsersAction):
                        nested.update(inner.choices)
                found[name] = (parser, nested)
    return found


def options_of(parser) -> set:
    return {option for action in parser._actions for option in action.option_strings}


class PlaybookTests(unittest.TestCase):
    def test_every_playbook_has_required_sections_and_next_link(self):
        for name in PLAYBOOKS:
            path = PLAYBOOK_DIR / name
            self.assertTrue(path.is_file(), f"{name} is missing")
            text = read(path)
            found = sections(text)
            wanted = list(PLAYBOOK_SECTIONS)
            if name in LENS_PLAYBOOKS:
                wanted.insert(2, "Lenses active")
            for heading in wanted:
                self.assertIn(heading, found, f"{name}: missing section '{heading}'")
            self.assertEqual(name in LENS_PLAYBOOKS, "Lenses active" in found,
                             f"{name}: 'Lenses active' belongs to stages 1 to 6 only")
            steps = [line for line in found["Steps"].splitlines() if re.match(r"\d+\. ", line)]
            self.assertGreaterEqual(len(steps), 3, f"{name}: Steps must be a numbered list")
            self.assertIn("`pk ", found["Steps"], f"{name}: Steps must name the pk command it runs")
            self.assertLessEqual(len(text.splitlines()), 120, f"{name}: over 120 lines")
            links = PLAYBOOK_FILE.findall(found["Next playbook"])
            self.assertTrue(links, f"{name}: Next playbook names no playbook file")
            for link in links:
                self.assertTrue((PLAYBOOK_DIR / link).is_file(), f"{name}: Next playbook {link} does not exist")
            self.assertEqual(links[0], EXPECTED_NEXT[name], f"{name}: Next playbook should name {EXPECTED_NEXT[name]} first")

    def test_start_playbook_names_every_other_playbook(self):
        text = read(PLAYBOOK_DIR / PLAYBOOKS[0])
        for name in PLAYBOOKS[1:]:
            self.assertIn(name, text, f"00_START.md does not name {name}")
        self.assertIn("pk interview next", text)
        self.assertIn("pk handoff check", text)

    def test_execute_playbook_states_the_gate_lines_and_ignored_build_output_rule(self):
        text = read(PLAYBOOK_DIR / "08_execute.md")
        self.assertIn(HANDBACK_LINE, text)
        self.assertIn(MERGE_LINE, text)
        self.assertRegex(text, r"(?i)git-ignore")
        self.assertIn("pk gate binding", text)
        self.assertIn("Guarded edit:", text)
        self.assertIn("## Exact Commands", text)
        self.assertIn("git-ignore", rule_part(1))

    def test_handoff_playbook_has_successor_prompt_with_fetch_and_main_suite(self):
        text = read(PLAYBOOK_DIR / "09_handoff.md")
        self.assertRegex(text, r"(?i)successor prompt")
        self.assertIn("pk fetch", text)
        self.assertRegex(text, r"(?i)suite passes on main|main suite")


class ReviewRecordTests(unittest.TestCase):
    """Wherever the gate's frozen-file check is described, the spec-QA record is part of it and must be on main."""

    MISSING = "`FAILED: review program/tasks/<ID>/review.md is missing on main`"

    def test_the_documents_that_describe_the_check_say_the_record_must_be_on_main(self):
        for path in (PLAYBOOK_DIR / "08_execute.md", BRIEF_DIR / "implementer.md", BRIEF_DIR / "controller.md",
                     KIT.parent / "README.md"):
            with self.subTest(path.name):
                self.assertIn(self.MISSING, read(path))

    def test_the_example_dispatches_with_the_record(self):
        example = KIT.parent / "examples" / "tiny-service"
        self.assertTrue((example / "tasks" / "P1-M01-S01-T01" / "review.md").is_file())
        loop = sections(read(example / "README.md"))["The loop, command by command"]
        self.assertIn("copy the task's SPEC, PLAN and spec-QA `review.md`", loop)
        self.assertIn("commit the four together to main", loop)

    def test_the_task_spec_playbook_says_a_dispatch_without_the_record_fails_both_gates(self):
        text = read(PLAYBOOK_DIR / "07_task_specs.md")
        self.assertIn("review.md", sections(text)["Done when"])
        self.assertIn("fails the hand-back gate and the merge check", text)

    def test_the_merge_check_is_described_with_the_review_record_everywhere(self):
        execute = sections(read(PLAYBOOK_DIR / "08_execute.md"))["Steps"]
        merge = execute[execute.index("11. Land"):]
        self.assertIn("SPEC, PLAN and spec-QA `review.md` are main's", merge)
        readme = read(KIT.parent / "README.md")
        self.assertIn("the task's SPEC, PLAN and spec-QA `review.md` on main and unchanged", readme)
        self.assertIn("commits its SPEC, PLAN, spec-QA `review.md` and status file to main together", readme)


class KnownLimitsTests(unittest.TestCase):
    """The FAQ says in plain words what the kit does not do."""

    def test_the_faq_has_a_known_limits_answer_covering_the_five_limits(self):
        answer = sections(read(KIT.parent / "docs" / "FAQ.md")).get("What are the known limits?")
        self.assertIsNotNone(answer)
        for needle in ("binds", "merge check", "not locked", "controller error", "install.sh", "kit/tests",
                       "Python 3.11", "3.14", "not signed"):
            self.assertIn(needle, answer)
        self.assertLess(len(answer.split()), 330)  # a short answer


class BriefTests(unittest.TestCase):
    def test_briefs_name_the_four_needs_context_cases(self):
        for name in BRIEFS:
            path = BRIEF_DIR / name
            self.assertTrue(path.is_file(), f"{name} is missing")
            text = read(path)
            found = sections(text)
            for case in NEEDS_CONTEXT:
                self.assertIn(case, found.get("Needs context", ""), f"{name}: Needs context lacks '{case}'")
            for heading in BRIEF_SECTIONS:
                self.assertIn(heading, found, f"{name}: missing section '{heading}'")
            self.assertLessEqual(len(text.splitlines()), 100, f"{name}: over 100 lines")
            self.assertLessEqual(len(found["Report shape"].strip().splitlines()), 25 + 6,
                                 f"{name}: report shape is not short")
            self.assertRegex(text, r"(?i)under 25 lines", f"{name}: report limit not stated")


class ZeroContextReviewTests(unittest.TestCase):
    """0.5.0: the lens and challenger reviews a full-tier spec gets, and how spec QA rules on them."""

    def test_the_rubrics_ship_with_numbered_items_and_the_zero_context_rule(self):
        self.assertEqual(sorted(p.name for p in RUBRIC_DIR.iterdir() if p.suffix == ".md"),
                         sorted(["README.md", *RUBRICS]))
        readme = read(RUBRIC_DIR / "README.md")
        for name, (prefix, count) in RUBRICS.items():
            text = read(RUBRIC_DIR / name)
            ids = re.findall(rf"^- ({prefix}-\d+) ", text, re.M)
            self.assertEqual(ids, [f"{prefix}-{n}" for n in range(1, count + 1)], name)
            self.assertIn(name, readme)
            self.assertLess(len(text.encode("utf-8")), 2000, f"{name}: a rubric is short")
        for needle in ("zero-context", "goldfish", "SPEC.md", "PLAN.md", "CONTRACT.md", "one rubric",
                       "no rulings", "no controller summary"):
            self.assertIn(needle, readme)
        for dropped in DROPPED_LENSES:
            self.assertFalse((RUBRIC_DIR / f"{dropped}.md").exists(), dropped)

    def test_the_lens_brief_reads_in_full_writes_finding_lines_and_never_writes_the_repository(self):
        text = read(BRIEF_DIR / "lens.md")
        found = sections(text)
        for heading in ("Role", "Read first", "What you do", "Report shape", "Never"):
            self.assertIn(heading, found, heading)
        self.assertLessEqual(len(text.splitlines()), 60)
        self.assertRegex(text, r"(?i)under 25 lines")
        self.assertIn("in full", found["Read first"])
        self.assertIn("`ID | severity | anchor | finding | exact fix`", text)
        self.assertIn("rubric item", text)
        self.assertIn("scratch folder", text)
        self.assertRegex(found["Never"], r"(?i)never write, edit, stage or commit anything in the repository")
        for unread in ("rulings", "HANDOFF", "review.md", "controller-state.json", "git history"):
            self.assertIn(unread, found["Read first"], unread)

    def test_playbook_07_runs_the_lenses_and_the_challenger_on_the_full_tier_before_spec_qa(self):
        steps = sections(read(PLAYBOOK_DIR / "07_task_specs.md"))["Steps"]
        step = next(line for line in steps.splitlines() if line.startswith("6a. "))
        self.assertLess(steps.index("6a. "), steps.index("7. Start a spec-QA agent"))
        self.assertLess(steps.index("6. "), steps.index("6a. "))
        for needle in ("full tier only", "hands back", "before spec QA", "in parallel", "tenancy-security",
                       "class A", "failure-modes", "class C", "challenger", "before any", "beside the author's",
                       "kit/briefs/lens.md", "kit/rubrics/"):
            self.assertIn(needle, step, needle)
        self.assertIn("phase 2", steps[steps.index("7. Start a spec-QA agent"):])

    def test_playbook_08_runs_pk_drift_after_each_merge_batch(self):
        steps = sections(read(PLAYBOOK_DIR / "08_execute.md"))["Steps"]
        self.assertRegex(steps, r"After each merge batch[^\n]*`pk drift`")

    def test_spec_qa_reviews_blind_then_rules_on_every_lens_row_with_no_synthesizer_or_judge(self):
        text = read(BRIEF_DIR / "spec_qa.md")
        work = sections(text)["What you do"]
        self.assertLess(work.index("Phase 1"), work.index("Phase 2"))
        self.assertIn("blind", work)
        for cls in LENS_CLASSES:
            self.assertIn(f"`{cls}`", work, cls)
        self.assertIn("every lens row", work)
        self.assertIn("review.md", work)
        self.assertRegex(work, r"(?i)no synthesizer and no separate judge")
        self.assertIn("Never fix the spec yourself.", text)  # the accepted rows become findings; the author fixes
        self.assertRegex(sections(text)["Never"], r"(?i)never read a lens report before your phase 1 findings")


class RulesTests(unittest.TestCase):
    def test_rules_files_exist(self):
        for name in RULES:
            self.assertTrue((RULES_DIR / name).is_file(), f"{name} is missing")

    def test_rulings_seed_numbered_k1_to_kn_without_gaps(self):
        text = read(RULES_DIR / "RULINGS_SEED.md")
        numbers = [int(m.group(1)) for m in re.finditer(r"^## K(\d+)\. \S", text, re.M)]
        self.assertGreaterEqual(len(numbers), 20)
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)))
        blocks = re.split(r"^## K\d+\. ", text, flags=re.M)[1:]
        for number, block in zip(numbers, blocks):
            for label in ("**Rule.**", "**Why.**", "**How to apply.**"):
                self.assertIn(label, block, f"K{number}: missing {label}")
        titles = {int(m.group(1)): m.group(2) for m in re.finditer(r"^## K(\d+)\. (.+)$", text, re.M)}

        def block_titled(fragment: str) -> str:
            hits = [n for n, title in titles.items() if fragment in title.lower()]
            self.assertEqual(len(hits), 1, f"expected exactly one ruling titled with '{fragment}'")
            return blocks[hits[0] - 1]

        self.assertIn("program/HANDOFF.md", block_titled("controller documents"))
        self.assertRegex(block_titled("nothing else merges"), r"(?i)quiet main")

    def test_there_is_one_rules_file_with_four_parts_and_nothing_restates_it(self):
        self.assertEqual(sorted(p.name for p in RULES_DIR.iterdir()), sorted(RULES))
        found = [name for name in sections(read(RULES_DIR / "RULES.md")) if name.startswith("Part ")]
        self.assertEqual([name.split(" (")[0] for name in found], RULE_PARTS)

    def test_the_hard_rules_keep_their_numbers_and_their_text(self):
        text = rule_part(1)
        numbers = [int(n) for n in re.findall(r"^(\d+)\. ", text, re.M)]
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)))
        self.assertGreaterEqual(len(numbers), 23)
        for line in HARD_RULE_LINES:  # the rules that must never change, word for word
            self.assertIn(line, text)

    def test_evidence_honesty_has_twelve_rules(self):
        text = rule_part(3)
        self.assertEqual([int(n) for n in re.findall(r"^(\d+)\. ", text, re.M)], list(range(1, 13)))
        self.assertIn("the per-task latest.json is the only mutable evidence file", text)  # rule 11
        self.assertIn("A record binds only its inputs, never a derived output", text)  # rule 12

    def test_the_working_rules_run_w1_to_w12_and_w12_is_the_fast_default_rule(self):
        text = rule_part(2)
        self.assertEqual([int(n) for n in re.findall(r"^- \*\*W(\d+)\. ", text, re.M)], list(range(1, 13)))
        w12 = re.search(r"^- \*\*W12\. Fast default, slow set in the release set\.\*\*(.*)$", text, re.M)
        self.assertTrue(w12, "W12 missing or retitled")
        for phrase in ("`test` step", "environment variable", "program/release-set.yaml", "pk fingerprint"):
            self.assertIn(phrase, w12.group(1))

    def test_the_execute_playbook_points_the_evidence_step_to_w12_and_the_fingerprint(self):
        lines = read(PLAYBOOK_DIR / "08_execute.md").splitlines()
        at = next(i for i, ln in enumerate(lines) if ln.startswith("5. Evidence"))
        self.assertTrue(lines[at + 1].startswith("   - "), "the paragraph is a sub-point of the evidence step")
        self.assertIn("W12", lines[at + 1])
        self.assertIn("`pk fingerprint run`", lines[at + 1])
        self.assertTrue(lines[at + 2].startswith("6. Hand-back gate"))

    def test_throughput_covers_required_topics(self):
        text = (rule_part(2) + rule_part(4)).lower()
        for topic in ("merge queue", "hand-back gate", "decide, don't ask", "finding-to-fix", "class a", "class d",
                      "escalate", "three implementers", "four agents", "polling", "interrupt", "quiet main",
                      "verification tiers", "fast path", "integration round", "contract first", "return contract",
                      "always name the model", "three resumes", "verify, don't relay", "batch", "budget",
                      "never forces"):
            self.assertIn(topic, text, f"RULES.md parts 2 and 4 do not cover '{topic}'")


def rules_bytes(parts) -> int:
    """The size of the canonical rules file up to the end of the last part a role reads."""
    text = read(RULES_DIR / "RULES.md")
    marks = [text.index(f"## {title}") for title in RULE_PARTS] + [len(text)]
    return len(text[:marks[max(parts)]].encode("utf-8"))


def size(*paths) -> int:
    return sum(len((KIT / path).read_bytes()) for path in paths)


# What each role reads at start (kit files; program/COMMON.md counted as its template), in the row order of
# the table in 09_handoff.md.
READING = {
    "Controller or successor": lambda: rules_bytes((1, 2, 3, 4)) + size(
        "briefs/controller.md", "playbooks/00_START.md", "playbooks/08_execute.md", "templates/COMMON.md"),
    "Implementer, full or standard tier": lambda: rules_bytes((1, 2, 3)) + size(
        "briefs/implementer.md", "templates/COMMON.md"),
    "Implementer, light tier": lambda: rules_bytes((1, 2)) + size("briefs/implementer.md", "templates/COMMON.md"),
    "QA": lambda: rules_bytes((1, 2, 3)) + size("briefs/qa.md", "templates/COMMON.md"),
    "Spec QA": lambda: rules_bytes((1, 2)) + size("briefs/spec_qa.md", "templates/COMMON.md"),
}
# What the same roles had to read in 0.2.0: three rule files (14.1 KB) and, for the controller, all four briefs,
# the start and execute playbooks and the hand-off playbook. The 0.3.0 sets must stay under these.
READING_0_2_KB = {"Controller or successor": 54.7, "Implementer, full or standard tier": 19.6,
                  "Implementer, light tier": 19.6, "QA": 20.5, "Spec QA": 17.9}
# QA's ceiling is 20.5, not the 20.0 of 0.2.0: working rule W12 added a paragraph to part 2, which QA reads.


class RuleFilePointerTests(unittest.TestCase):
    """Every `kit/rules/<FILE>.md` a shipped document or script points to is a file of the kit."""

    def test_no_document_points_to_a_rules_file_that_is_gone(self):
        repo = KIT.parent
        texts = [path for folder in (KIT, repo / "docs", repo / "examples") for path in sorted(folder.rglob("*"))
                 if path.suffix in (".md", ".py", ".yaml", ".json") and "tests" not in path.parts
                 and "__pycache__" not in path.parts]
        texts += [repo / "README.md", repo / "CHANGELOG.md"]
        self.assertGreater(len(texts), 60)
        named = {}
        for path in texts:
            for name in re.findall(r"kit/rules/([A-Za-z_]+\.md)", read(path)):
                named.setdefault(name, path)
        self.assertEqual(sorted(named), sorted(RULES))  # both are pointed at, and nothing else is
        for name, path in named.items():
            self.assertTrue((RULES_DIR / name).is_file(), f"{path} points to kit/rules/{name}, which does not exist")


class ReadingSizeTests(unittest.TestCase):
    """A role reads only what its role needs, the hand-off playbook says which files and how big, and it is true."""

    def rows(self) -> dict:
        text = read(PLAYBOOK_DIR / "09_handoff.md")
        found = {}
        for line in text.splitlines():
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if line.startswith("|") and len(cells) == 3 and cells[0] in READING:
                found[cells[0]] = (cells[1], float(cells[2]))
        return found

    def test_the_table_names_every_role_and_its_sizes_are_the_real_ones(self):
        rows = self.rows()
        self.assertEqual(list(rows), list(READING))
        for role, (_, stated) in rows.items():
            actual = READING[role]() / 1000
            self.assertAlmostEqual(stated, actual, delta=0.5, msg=f"{role}: the table says {stated} KB, it is {actual:.1f}")

    def test_every_role_reads_less_than_it_did_and_no_role_reads_another_roles_brief(self):
        for role, (files, stated) in self.rows().items():
            self.assertLess(stated, READING_0_2_KB[role], role)
            briefs = re.findall(r"kit/briefs/(\w+)\.md", files)
            self.assertEqual(len(briefs), 1, f"{role} reads {briefs}")
            self.assertIn("program/COMMON.md", files)
        for name in ("implementer.md", "qa.md", "spec_qa.md"):
            read_first = sections(read(BRIEF_DIR / name))["Read first"]
            self.assertNotIn("HANDOFF", read_first, name)
            self.assertNotRegex(read_first, r"(?<!cites, by number, )`program/rulings\.md`", name)
        self.assertIn("Do not read the other briefs", read(BRIEF_DIR / "controller.md"))

    def test_the_successor_prompt_points_to_the_rules_instead_of_restating_them(self):
        prompt = read(PLAYBOOK_DIR / "09_handoff.md").split("```")[1]
        self.assertIn("kit/rules/RULES.md", prompt)
        self.assertLess(len(prompt.splitlines()), 30)
        for restated in ("never fabricate", "only QA sets done", "never rewrite history"):
            self.assertNotIn(restated, prompt)


class StageSevenTests(unittest.TestCase):
    def test_stage_7_is_per_task_and_never_approved(self):
        for path in all_files():
            text = read(path)
            self.assertNotIn("all stages approved", text, f"{path.name}: stage 7 is never approved")
            self.assertNotIn("stage-7.json", text, f"{path.name}: there is no stage 7 approval")
            self.assertNotRegex(text, r"interview (approve|challenge|report|amend) 7", path.name)
        start = read(PLAYBOOK_DIR / "00_START.md")
        self.assertRegex(start, r"(?i)stage 7 is never approved")
        self.assertRegex(read(PLAYBOOK_DIR / "07_task_specs.md"), r"(?i)once per task")


class LedgerExampleTests(unittest.TestCase):
    def test_discover_playbook_ledger_example_is_valid(self):
        import interview
        import miniyaml
        import tempfile
        import shutil

        text = read(PLAYBOOK_DIR / "01_discover.md")
        block = re.search(r"```\n(sources:\n.*?)```", text, re.S)
        self.assertIsNotNone(block, "01_discover.md has no ledger example")
        root = Path(tempfile.mkdtemp())
        try:
            (root / "program").mkdir()
            (root / "program" / "ledger.yaml").write_text(block.group(1), encoding="utf-8")
            miniyaml.loads(block.group(1))
            self.assertEqual(interview.ledger_validate(root), [])
        finally:
            shutil.rmtree(root)


class CommandTests(unittest.TestCase):
    def test_every_pk_command_mentioned_exists(self):
        parsers = command_parsers()
        known = set(pk.COMMANDS)
        mention = re.compile(r"\bpk ([a-z][a-z-]*)(?: ([a-z][a-z-]*))?([^`\n]*)")
        seen = 0
        for path in all_files():
            for match in mention.finditer(read(path)):
                command, word, rest = match.groups()
                where = f"{path.name}: pk {command}"
                self.assertIn(command, known, f"{where}: not a pk command")
                seen += 1
                if command not in parsers:
                    continue
                parser, actions = parsers[command]
                valid = options_of(parser)
                if actions and word:
                    self.assertIn(word, actions, f"{where} {word}: not an action of pk {command}")
                    valid |= options_of(actions[word])
                for flag in re.findall(r"(?<![\w-])(--[a-z][a-z-]*)", f" {word or ''}{rest}"):
                    self.assertIn(flag, valid, f"{where}: unknown flag {flag}")
        self.assertGreater(seen, 40)

    def test_every_pk_command_the_readme_names_exists(self):
        parsers = command_parsers()
        known = set(pk.COMMANDS)
        mention = re.compile(r"\bpk ([a-z][a-z-]*)(?: ([a-z][a-z-]*))?([^`\n]*)")
        text = read(KIT.parent / "README.md")
        seen = 0
        for command, word, rest in mention.findall(text):
            self.assertIn(command, known, f"README.md: pk {command}: not a pk command")
            seen += 1
            if command not in parsers:
                continue
            parser, actions = parsers[command]
            valid = options_of(parser)
            if actions and word:
                self.assertIn(word, actions, f"README.md: pk {command} {word}: not an action")
                valid |= options_of(actions[word])
            for flag in re.findall(r"(?<![\w-])(--[a-z][a-z-]*)", f" {word or ''}{rest}"):
                self.assertIn(flag, valid, f"README.md: pk {command}: unknown flag {flag}")
        self.assertGreater(seen, 5)


class NeutralityTests(unittest.TestCase):
    def test_no_forbidden_token_or_url_and_no_stray_ok_line(self):
        for path in all_files():
            text = read(path)
            self.assertIsNone(FORBIDDEN_RE.search(text), f"{path.name}: names a vendor, model or tool")
            self.assertIsNone(URL.search(text), f"{path.name}: contains a URL")
            for line in text.splitlines():
                self.assertFalse(line.lstrip().startswith("OK: "), f"{path.name}: line starts with 'OK: '")
            for line in re.findall(r"`(OK: [^`]*)`", text):
                self.assertIn(f"`{line}`", (HANDBACK_LINE, MERGE_LINE, ROUND_LINE), f"{path.name}: unexpected OK line")


YAML_FENCE = re.compile(r"^```ya?ml\n(.*?)^```", re.M | re.S)
CODE_SPAN = re.compile(r"`([^`\n]+)`")
FLOW_IN_TEXT = re.compile(r"(?:^|[\s:(])[\[{](?![\]}])")
OWNED_PLAYBOOKS = ("06_register.md", "07_task_specs.md", "09_handoff.md")


class TaughtYamlTests(unittest.TestCase):
    """Finding 10: nothing the kit teaches is YAML its own reader rejects."""

    def test_yaml_fences_in_owned_playbooks_parse_with_the_kit_reader(self):
        import miniyaml
        for name in OWNED_PLAYBOOKS:
            for block in YAML_FENCE.findall(read(PLAYBOOK_DIR / name)):
                with self.subTest(name, block=block[:40]):
                    miniyaml.loads(block)

    def test_no_inline_example_teaches_a_flow_collection(self):
        files = [PLAYBOOK_DIR / name for name in OWNED_PLAYBOOKS]
        files += [KIT.parent / "README.md", KIT.parent / "docs" / "FAQ.md"]
        for path in files:
            for span in CODE_SPAN.findall(read(path)):
                self.assertIsNone(FLOW_IN_TEXT.search(span), f"{path.name}: `{span}`")

    def test_the_register_playbook_teaches_blocked_by_as_a_block_list(self):
        step = [line for line in read(PLAYBOOK_DIR / "06_register.md").splitlines() if "blocked_by" in line][0]
        self.assertNotIn("[", step)
        self.assertIn("- <environment>", step)


# The documented slow set (docs/DESIGN.md section 13): only these modules gate cases behind KIT_TESTS_SLOW.
SLOW_MODULES = ["test_end_to_end.py", "test_evidence.py", "test_gate.py", "test_install.py", "test_land.py",
                "test_rejudge.py", "test_tiers.py"]
SLOW_IMPORT = re.compile(r"^(?:from _slow import|import _slow\b)", re.M)


class SlowGateTests(unittest.TestCase):
    def test_the_modules_importing_the_slow_gate_are_the_documented_slow_set(self):
        found = sorted(p.name for p in (KIT / "tests").glob("test_*.py") if SLOW_IMPORT.search(read(p)))
        self.assertEqual(found, sorted(SLOW_MODULES))

    def test_design_names_every_gated_module_and_the_variable(self):
        design = read(KIT.parent / "docs" / "DESIGN.md")
        self.assertIn("KIT_TESTS_SLOW=1", design)
        for name in SLOW_MODULES:
            self.assertIn(name[:-3], design, name)


if __name__ == "__main__":
    unittest.main()
