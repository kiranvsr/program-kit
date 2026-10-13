import _paths  # noqa: F401  (must come first: puts kit/scripts on sys.path)
import re
import unittest
from pathlib import Path

import evidence
import miniyaml
from _tokens import FORBIDDEN
from common import read_json, read_yaml

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
NEW_TEMPLATES = ("discovery.md", "product.md", "SPEC.md", "ARCHITECTURE.md", "adr.md", "roadmap.md",
                 "task_SPEC.md", "task_PLAN.md", "freeze_review.md", "signoff.md", "COMMON.md",
                 "task_FAST_BRIEF.md", "CONTRACT.md")
BANNED = re.compile("|".join([*map(re.escape, FORBIDDEN), "lorem ipsum", "https?://"]), re.I)
TASK_SPEC_HEADINGS = ["Summary", "Tier", "Scope In", "Scope Out", "Dependencies", "Consumed Contracts",
                      "Security And Tenancy", "Failure Modes And Recovery", "Observability", "Module Placement",
                      "Assumptions Checked", "Decided (do not reopen)", "Stop and report when", "Closes"]
TASK_PLAN_HEADINGS = ["Status And PR Boundary", "Work Breakdown", "Exact Commands", "Definition Of Done",
                      "Gate Evidence", "Rollout And Rollback", "Risks"]


def headings(name: str) -> list[str]:
    text = (TEMPLATES / name).read_text(encoding="utf-8")
    return [line[3:].strip() for line in text.splitlines() if line.startswith("## ")]


class TemplateTests(unittest.TestCase):
    def test_templates_have_required_headings(self):
        self.assertEqual(headings("task_SPEC.md"), TASK_SPEC_HEADINGS)
        self.assertEqual(headings("task_PLAN.md"), TASK_PLAN_HEADINGS)

    def test_every_new_template_exists_and_is_clean(self):
        for name in NEW_TEMPLATES:
            text = (TEMPLATES / name).read_text(encoding="utf-8")
            self.assertTrue(text.strip(), name)
            self.assertIsNone(BANNED.search(text), name)
            self.assertTrue(text.startswith("# "), name)
            self.assertGreaterEqual(len(headings(name)), 3, name)

    def test_exact_commands_section_explains_the_line_format_and_lists_no_command(self):
        text = (TEMPLATES / "task_PLAN.md").read_text(encoding="utf-8")
        section = text.split("## Exact Commands", 1)[1].split("\n## ", 1)[0]
        self.assertIn("dollar sign", section)
        self.assertIn("# expect", section)
        self.assertIn("without a shell", section)
        # An unedited template must hold zero commands, so the gate fails honestly instead of running a sample.
        self.assertEqual([line for line in text.splitlines() if line.startswith("$ ")], [])

    def test_the_fast_brief_template_has_the_sections_the_gate_requires_and_lists_no_command(self):
        import gate
        text = (TEMPLATES / "task_FAST_BRIEF.md").read_text(encoding="utf-8")
        for heading in (*gate.FAST_BRIEF_HEADINGS, gate.PLAN_SECTION):
            self.assertIn(heading + "\n", text)
        self.assertEqual(gate.parse_plan(text), [])  # an unedited template runs nothing
        self.assertRegex(text, r"(?m)^Tier: standard$")
        self.assertIn("never used for the full tier", text)

    def test_the_task_spec_template_states_a_tier_the_gate_can_read_and_defaults_to_full(self):
        import gate
        lines = (TEMPLATES / "task_SPEC.md").read_text(encoding="utf-8").splitlines()
        self.assertEqual([m.group(1) for m in map(gate._TIER_LINE.match, lines) if m], ["full"])

    def test_the_common_rules_template_maps_every_model_class_and_stays_small(self):
        text = (TEMPLATES / "COMMON.md").read_text(encoding="utf-8")
        self.assertEqual(headings("COMMON.md"), ["Run", "Constraints", "Models", "Report contract",
                                                 "Known failures on main"])
        for row in ("| A |", "| B |", "| C |", "| D |"):
            self.assertIn(row, text)
        self.assertLess(len(text.encode("utf-8")), 3000)

    def test_the_hand_off_template_seeds_the_known_hazards(self):
        self.assertIn("Known hazards", headings("HANDOFF.md"))
        self.assertIn("git rev-parse --show-toplevel", (TEMPLATES / "HANDOFF.md").read_text(encoding="utf-8"))

    def test_lens_sections_in_freeze_review(self):
        self.assertEqual(headings("freeze_review.md")[:5],
                         ["Architect", "Engineering", "Program", "Project", "Delivery"])


FLOW = re.compile(r"[\[{](?![\]}])")  # a flow collection with something inside; [] and {} are the empty literals


def yaml_templates() -> list[Path]:
    return sorted(TEMPLATES.glob("*.yaml"))


def example_blocks(text: str) -> list[str]:
    """The runs of consecutive example comment lines of a YAML file, uncommented."""
    blocks, current = [], []
    for line in text.splitlines():
        if line.startswith("#   "):
            current.append(line[4:])
        else:
            if current:
                blocks.append("\n".join(current) + "\n")
            current = []
    if current:
        blocks.append("\n".join(current) + "\n")
    return blocks


class YamlTemplateTests(unittest.TestCase):
    """Finding 10: every YAML shape a template teaches is block style, and the kit's own reader accepts it."""

    def test_every_yaml_template_parses_with_the_kit_reader(self):
        self.assertGreaterEqual(len(yaml_templates()), 6)
        for path in yaml_templates():
            with self.subTest(path.name):
                read_yaml(path)

    def test_every_example_in_a_template_comment_parses(self):
        found = 0
        for path in yaml_templates():
            for block in example_blocks(path.read_text(encoding="utf-8")):
                found += 1
                with self.subTest(path.name, block=block[:40]):
                    self.assertIsNotNone(miniyaml.loads(block))
        self.assertGreaterEqual(found, 6)  # checks, environments, register, traceability (x4), gates

    def test_templates_that_teach_commands_and_environments_carry_a_parsed_example(self):
        checks = example_blocks((TEMPLATES / "checks.yaml").read_text(encoding="utf-8"))
        self.assertEqual([miniyaml.loads(block) for block in checks], [{"test": [["go", "test", "./..."]]}])
        environments = example_blocks((TEMPLATES / "environments.yaml").read_text(encoding="utf-8"))
        entry = miniyaml.loads(environments[0])["environments"]["dev"]
        self.assertEqual(sorted(entry), ["exists", "notes", "owner"])
        register = example_blocks((TEMPLATES / "register.yaml").read_text(encoding="utf-8"))
        self.assertEqual(miniyaml.loads(register[0])["phases"][0]["id"], "P1")

    def test_no_template_comment_teaches_a_flow_collection(self):
        for path in yaml_templates():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if line.lstrip().startswith("#"):
                    self.assertIsNone(FLOW.search(line), f"{path.name}:{number}: {line}")

    def test_the_register_template_holds_no_placeholder_task(self):
        data = miniyaml.loads((TEMPLATES / "register.yaml").read_text(encoding="utf-8"))
        self.assertEqual(data, {"phases": []})


class ResultsSchemaTests(unittest.TestCase):
    """The planted-bug row fields the gate, the binding check and `done` require are required by the schema too."""

    def row_schema(self) -> dict:
        return read_json(TEMPLATES / "results.schema.json")["properties"]["planted_bugs"]["items"]

    def test_the_required_row_fields_are_the_codes_own_list(self):
        self.assertEqual(self.row_schema()["required"], [*evidence.ROW_KEYS, *evidence.PLANTED_KEYS])
        self.assertEqual(evidence.PLANTED_KEYS, ("path", "find", "replace", "build_argv", "test_argv"))

    def test_every_required_row_field_is_described_and_nothing_else_is(self):
        items = self.row_schema()
        self.assertEqual(sorted(items["properties"]), sorted(items["required"]))

    def test_a_row_without_a_required_field_is_what_the_gate_refuses(self):
        row = {"id": "flip", "builds": True, "killed": True, "kill_reason": "test failed",
               "test_output_sha256": "0" * 64, "path": "calc.py", "find": "a + b", "replace": "a - b",
               "build_argv": ["make"], "test_argv": ["make", "test"]}
        self.assertEqual(sorted(row), sorted(self.row_schema()["required"]))
        own = dict(row, id=evidence.SELF_TEST_ID, builds=False, killed=False, kill_reason="build failed",
                   test_output_sha256="", find="", replace=evidence.BROKEN_LINE)
        digest = "0" * 64
        record = {"task_id": "T-1", "head": "a" * 40, "main": "b" * 40, "merge_base": "b" * 40,
                  "produced_at": "2026-01-01T00:00:00Z", "inputs": {"calc.py": digest},
                  "commands": [{"label": "unit", "argv": ["make", "test"], "exit": 0, "expected_exit": 0,
                                "as_expected": True, "derived": {}, "output_sha256": digest, "output_tail": ""}],
                  "planted_bugs": [row, own], "planted_bugs_all_killed": True, "all_commands_as_expected": True,
                  "tree_unchanged_by_run": True, "blocked_by": [],
                  "deviations": []}
        self.assertEqual(evidence.gate_problems(record, "T-1"), [])
        for key in self.row_schema()["required"]:
            with self.subTest(key=key):
                stripped = dict(record, planted_bugs=[{k: v for k, v in row.items() if k != key}, own])
                self.assertTrue(evidence.gate_problems(stripped, "T-1"), key)


class HandoffTemplateTests(unittest.TestCase):
    """Lesser 22: the template holds the maintained sections playbook 09 names, outside the generated block."""

    SECTIONS = ["How the program works", "Environment setup", "Known hazards", "Next steps", "Where the rules live"]
    SEEDED = ("Known hazards", "Where the rules live")  # these start with the lines every program needs

    def test_maintained_sections_exist_outside_the_generated_block(self):
        text = (TEMPLATES / "HANDOFF.md").read_text(encoding="utf-8")
        start, end = "<!-- generated:start -->", "<!-- generated:end -->"
        outside = text[:text.index(start)] + text[text.index(end) + len(end):]
        self.assertEqual([line[3:].strip() for line in outside.splitlines() if line.startswith("## ")],
                         self.SECTIONS)
        self.assertEqual(text[text.index(start) + len(start):text.index(end)].strip(), "")

    def test_each_section_is_empty_or_seeded_and_playbook_09_names_it(self):
        text = (TEMPLATES / "HANDOFF.md").read_text(encoding="utf-8")
        playbook = (TEMPLATES.parent / "playbooks" / "09_handoff.md").read_text(encoding="utf-8").lower()
        for heading in self.SECTIONS:
            self.assertIn(heading.lower(), playbook, heading)
            body = text.split(f"## {heading}", 1)[1].split("\n## ", 1)[0].split("<!--", 1)[0]
            self.assertEqual(bool(body.strip()), heading in self.SEEDED, heading)


if __name__ == "__main__":
    unittest.main()
