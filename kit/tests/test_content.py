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

PLAYBOOKS = ["00_START.md", "01_discover.md", "02_define_product.md", "03_specify.md", "04_architect.md",
             "05_freeze_review.md", "06_register.md", "07_task_specs.md", "08_execute.md", "09_handoff.md",
             "10_change.md"]
LENS_PLAYBOOKS = PLAYBOOKS[1:7]  # stages 1 to 6 are interview stages and name the lenses that are active
BRIEFS = ["controller.md", "implementer.md", "qa.md", "spec_qa.md"]
RULES = ["HARD_RULES.md", "EVIDENCE_HONESTY.md", "THROUGHPUT.md", "RULINGS_SEED.md"]

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


# FORBIDDEN comes from _tokens (assembled from fragments); these further names are fragments too, so this file
# does not itself contain a forbidden token.
OTHER_NAMES = ["co" + "pilot", "ll" + "ama", "mist" + "ral", "deep" + "seek", "qw" + "en", "x" + "ai\\b"]
FORBIDDEN_RE = re.compile("|".join([*map(re.escape, FORBIDDEN), *OTHER_NAMES]), re.I)
URL = re.compile(r"https?://|ftp://|www\.", re.I)
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


def all_files() -> list:
    return [PLAYBOOK_DIR / n for n in PLAYBOOKS] + [BRIEF_DIR / n for n in BRIEFS] + [RULES_DIR / n for n in RULES]


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
        self.assertIn("git-ignore", read(RULES_DIR / "HARD_RULES.md"))

    def test_handoff_playbook_has_successor_prompt_with_fetch_and_main_suite(self):
        text = read(PLAYBOOK_DIR / "09_handoff.md")
        self.assertRegex(text, r"(?i)successor prompt")
        self.assertIn("pk fetch", text)
        self.assertRegex(text, r"(?i)suite passes on main|main suite")


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

    def test_evidence_honesty_has_ten_rules(self):
        text = read(RULES_DIR / "EVIDENCE_HONESTY.md")
        self.assertEqual([int(n) for n in re.findall(r"^(\d+)\. ", text, re.M)], list(range(1, 11)))

    def test_throughput_covers_required_topics(self):
        text = read(RULES_DIR / "THROUGHPUT.md").lower()
        for topic in ("merge queue", "hand-back gate", "decide, don't ask", "finding-to-fix", "tier 1", "tier 2",
                      "escalate", "three implementers", "four agents", "polling", "interrupt", "quiet main"):
            self.assertIn(topic, text, f"THROUGHPUT.md does not cover '{topic}'")


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


class NeutralityTests(unittest.TestCase):
    def test_no_forbidden_token_or_url_and_no_stray_ok_line(self):
        for path in all_files():
            text = read(path)
            self.assertIsNone(FORBIDDEN_RE.search(text), f"{path.name}: names a vendor, model or tool")
            self.assertIsNone(URL.search(text), f"{path.name}: contains a URL")
            for line in text.splitlines():
                self.assertFalse(line.lstrip().startswith("OK: "), f"{path.name}: line starts with 'OK: '")
            for line in re.findall(r"`(OK: [^`]*)`", text):
                self.assertIn(f"`{line}`", (HANDBACK_LINE, MERGE_LINE), f"{path.name}: unexpected OK line")


if __name__ == "__main__":
    unittest.main()
