import _paths  # noqa: F401  (must come first: puts kit/scripts on sys.path)
import re
import unittest
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
NEW_TEMPLATES = ("discovery.md", "product.md", "SPEC.md", "ARCHITECTURE.md", "adr.md", "roadmap.md",
                 "task_SPEC.md", "task_PLAN.md", "freeze_review.md", "signoff.md")
FORBIDDEN = re.compile(r"claude|anthropic|openai|gpt|codex|gemini|grok|ent-harness|fable|opus|sonnet|lorem ipsum|"
                       r"https?://", re.I)
TASK_SPEC_HEADINGS = ["Summary", "Scope In", "Scope Out", "Dependencies", "Consumed Contracts",
                      "Security And Tenancy", "Failure Modes And Recovery", "Observability", "Module Placement",
                      "Resolved Questions", "Closes"]
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
            self.assertIsNone(FORBIDDEN.search(text), name)
            self.assertTrue(text.startswith("# "), name)
            self.assertGreaterEqual(len(headings(name)), 3, name)

    def test_exact_commands_section_explains_the_line_format(self):
        text = (TEMPLATES / "task_PLAN.md").read_text(encoding="utf-8")
        section = text.split("## Exact Commands", 1)[1].split("\n## ", 1)[0]
        self.assertIn("$ ", section)
        self.assertIn("# expect", section)
        self.assertIn("without a shell", section)
        self.assertEqual(len([l for l in section.splitlines() if l.startswith("$ ")]), 1)

    def test_lens_sections_in_freeze_review(self):
        self.assertEqual(headings("freeze_review.md")[:5],
                         ["Architect", "Engineering", "Program", "Project", "Delivery"])


if __name__ == "__main__":
    unittest.main()
