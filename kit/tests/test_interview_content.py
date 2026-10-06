"""The shipped interview content: coverage map, lens charters and seed prompts."""
import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import re
import unittest
from pathlib import Path

import interview
import miniyaml

KIT = Path(__file__).resolve().parent.parent
ROOT = KIT.parent
COVERAGE = KIT / "interview" / "coverage.yaml"
SEEDS = KIT / "interview" / "seeds.yaml"
LENS_DIR = KIT / "interview" / "lenses"

LENS_IDS = {"architect", "engineering", "program", "project", "delivery"}
CHARTERS = {"architect": "architect.md", "engineering": "engineering_manager.md", "program": "program_manager.md",
            "project": "project_manager.md", "delivery": "delivery_manager.md"}
SECTIONS = ["Owns", "Must leave knowing", "Writes", "Challenges", "Nominating a question", "Sign-off line"]
STAGE_TOPIC_COUNTS = {1: 9, 2: 4, 3: 5, 4: 7, 5: 0, 6: 3, 7: 0}


def load_map() -> dict:
    return interview.load_coverage(ROOT, path=COVERAGE)


def all_topics(coverage: dict) -> dict:
    return {topic_id: topic for stage in coverage["stages"].values() for topic_id, topic in stage["topics"].items()}


def load_seeds() -> dict:
    data = miniyaml.loads(SEEDS.read_text(encoding="utf-8"))
    return data["topics"]


class CoverageContentTest(unittest.TestCase):
    def test_coverage_has_28_topics_with_valid_owner_and_contributors(self):
        coverage = load_map()
        self.assertEqual(list(coverage["stages"]), [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual({n: len(s["topics"]) for n, s in coverage["stages"].items()}, STAGE_TOPIC_COUNTS)
        topics = all_topics(coverage)
        self.assertEqual(len(topics), 28)
        for topic_id, topic in topics.items():
            stage = int(topic_id[1:].split(".")[0])
            self.assertIn(topic_id, coverage["stages"][stage]["topics"], topic_id)
            self.assertIn(topic["owner"], LENS_IDS, topic_id)
            self.assertTrue(set(topic["contributes"]) <= LENS_IDS, topic_id)
            self.assertNotIn(topic["owner"], topic["contributes"], topic_id)
            self.assertEqual(len(set(topic["contributes"])), len(topic["contributes"]), topic_id)
            self.assertTrue(topic["title"].strip(), topic_id)
        self.assertEqual({t["owner"] for t in topics.values()} | {l for t in topics.values() for l in t["contributes"]},
                         LENS_IDS)
        for number, stage in coverage["stages"].items():
            self.assertTrue(stage["name"].strip(), number)
            self.assertTrue(stage["artifacts"] or number == 7, number)
            self.assertEqual(len(set(stage["artifacts"])), len(stage["artifacts"]), number)
            for artifact in stage["artifacts"]:
                self.assertFalse(artifact.startswith(("/", "program/")), artifact)
        self.assertEqual(interview.LENSES, tuple(sorted(LENS_IDS, key=interview.LENSES.index)))

    def test_every_topic_has_closes_when_and_needs(self):
        for topic_id, topic in all_topics(load_map()).items():
            self.assertTrue(topic["closes_when"].strip(), topic_id)
            self.assertTrue(2 <= len(topic["needs"]) <= 5, f"{topic_id}: {topic['needs']}")
            self.assertEqual(len(set(topic["needs"])), len(topic["needs"]), topic_id)
            for tag in topic["needs"]:
                self.assertRegex(tag, r"^[a-z][a-z0-9_]*$", topic_id)

    def test_conditions_reference_known_tags(self):
        data = miniyaml.loads(COVERAGE.read_text(encoding="utf-8"))
        known = data["condition_tags"]
        self.assertIsInstance(known, list)
        self.assertTrue(known)
        self.assertEqual(len(set(known)), len(known))
        conditional = {}
        for topic_id, topic in all_topics(load_map()).items():
            if topic["condition"] is None:
                continue
            conditional[topic_id] = topic["condition"]
            used = set(re.findall(r"has:([A-Za-z0-9_.-]+)", topic["condition"]))
            self.assertTrue(used, topic_id)
            self.assertTrue(used <= set(known), f"{topic_id}: {sorted(used - set(known))}")
            self.assertFalse(interview.evaluate_condition(topic["condition"], set()), topic_id)
            self.assertTrue(interview.evaluate_condition(topic["condition"], {sorted(used)[0]}), topic_id)
        self.assertEqual(set(conditional), {"T1.9", "T3.3"})
        self.assertEqual(set(known), {"regulated", "personal_data", "payments", "government", "healthcare",
                                      "external_customers", "external_system"})
        for tag in known:  # each documented tag is used by some condition
            self.assertTrue(any(f"has:{tag}" in c for c in conditional.values()), tag)


class SeedsContentTest(unittest.TestCase):
    def test_seeds_cover_every_topic(self):
        seeds = load_seeds()
        topics = all_topics(load_map())
        self.assertEqual(set(seeds), set(topics))
        for topic_id, seed in seeds.items():
            self.assertEqual(set(seed), {"required", "condition", "prompts"}, topic_id)
            self.assertEqual(seed["required"], topics[topic_id]["required"], topic_id)
            self.assertEqual(seed["condition"], topics[topic_id]["condition"], topic_id)
            self.assertIsInstance(seed["prompts"], list, topic_id)
            self.assertTrue(seed["prompts"], topic_id)
            for prompt in seed["prompts"]:
                self.assertIsInstance(prompt, str, topic_id)
                self.assertTrue(prompt.strip(), topic_id)
            self.assertEqual(len(set(seed["prompts"])), len(seed["prompts"]), topic_id)

    def test_optional_topics_are_the_conditional_ones(self):
        seeds = load_seeds()
        self.assertEqual({t for t, s in seeds.items() if not s["required"]}, {"T1.9", "T3.3"})
        self.assertEqual({t for t, s in seeds.items() if s["condition"] is not None}, {"T1.9", "T3.3"})


class LensCharterTest(unittest.TestCase):
    def test_each_lens_charter_has_required_sections(self):
        self.assertEqual({p.name for p in LENS_DIR.glob("*.md")}, set(CHARTERS.values()))
        for lens, filename in CHARTERS.items():
            lines = (LENS_DIR / filename).read_text(encoding="utf-8").splitlines()
            self.assertTrue(lines[0].startswith("# ") and lines[0][2:].strip(), filename)
            self.assertIn(f"id: {lens}", [line.strip() for line in lines[:4]], filename)
            headings = [line[3:].strip() for line in lines if line.startswith("## ")]
            self.assertEqual(headings, SECTIONS, filename)
            text = "\n".join(lines)
            for position, section in enumerate(SECTIONS):
                start = text.index(f"## {section}")
                end = text.index(f"## {SECTIONS[position + 1]}") if position + 1 < len(SECTIONS) else len(text)
                body = text[start + len(section) + 3:end].strip()
                self.assertTrue(body, f"{filename}: {section} is empty")

    def test_charters_name_only_known_lenses(self):
        coverage = load_map()
        owners = {t["owner"] for t in all_topics(coverage).values()}
        self.assertEqual(owners, LENS_IDS)


class ForbiddenTokensTest(unittest.TestCase):
    def test_no_provider_names_in_interview_content(self):
        pattern = re.compile(r"claude|anthropic|openai|gpt|codex|gemini|grok|ent-harness|fable|opus|sonnet", re.I)
        for path in [COVERAGE, SEEDS, *LENS_DIR.glob("*.md")]:
            self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")), path.name)


if __name__ == "__main__":
    unittest.main()
