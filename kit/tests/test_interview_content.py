"""The shipped interview content: coverage map, lens charters and seed prompts."""
import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import re
import unittest
from pathlib import Path

import interview
from _tokens import FORBIDDEN
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
STAGE_ARTIFACTS = {
    1: ["discovery.md", "environments.yaml", "CLAIM_BOUNDARIES.md"], 2: ["product.md"], 3: ["SPEC.md"],
    4: ["ARCHITECTURE.md", "gates.yaml"], 5: ["freeze/review.md", "freeze/signoff.md"],
    6: ["roadmap.md", "register.yaml"], 7: [],
}

# Pins: a deliberate content change must change these in the same commit.
OWNERS = {
    "T1.1": "program",
    "T1.2": "program",
    "T1.3": "architect",
    "T1.4": "program",
    "T1.5": "project",
    "T1.6": "delivery",
    "T1.7": "engineering",
    "T1.8": "program",
    "T1.9": "program",
    "T2.1": "program",
    "T2.2": "program",
    "T2.3": "delivery",
    "T2.4": "delivery",
    "T3.1": "architect",
    "T3.2": "architect",
    "T3.3": "architect",
    "T3.4": "architect",
    "T3.5": "architect",
    "T4.1": "architect",
    "T4.2": "architect",
    "T4.3": "architect",
    "T4.4": "delivery",
    "T4.5": "architect",
    "T4.6": "engineering",
    "T4.7": "engineering",
    "T6.1": "project",
    "T6.2": "project",
    "T6.3": "project",
}
PROMPT_COUNTS = {
    "T1.1": 4,
    "T1.2": 4,
    "T1.3": 4,
    "T1.4": 3,
    "T1.5": 5,
    "T1.6": 4,
    "T1.7": 4,
    "T1.8": 4,
    "T1.9": 3,
    "T2.1": 1,
    "T2.2": 3,
    "T2.3": 2,
    "T2.4": 3,
    "T3.1": 3,
    "T3.2": 5,
    "T3.3": 2,
    "T3.4": 10,
    "T3.5": 3,
    "T4.1": 1,
    "T4.2": 3,
    "T4.3": 1,
    "T4.4": 2,
    "T4.5": 2,
    "T4.6": 1,
    "T4.7": 2,
    "T6.1": 3,
    "T6.2": 2,
    "T6.3": 1,
}
NEEDS = {
    "T1.1": ["outcome", "beneficiary", "measures", "cost_of_not_building"],
    "T1.2": ["user_kinds", "dependent_systems", "decider_acceptor_payer", "conflict_priority"],
    "T1.3": ["system_owners", "system_health", "must_stay_unchanged", "prior_documents"],
    "T1.4": ["exclusions", "reduced_first_release", "deferred_items"],
    "T1.5": ["dates", "budget", "mandated_technology", "forbidden_technology", "organisational_constraints"],
    "T1.6": ["environment_inventory", "environment_operators", "missing_environment_owner"],
    "T1.7": ["team_and_split", "reviewer_merger", "on_call", "conventions", "response_time"],
    "T1.8": ["top_risk", "prior_failures", "unverified_assumptions", "spike_decision"],
    "T1.9": ["regulations", "contractual_commitments", "compliance_owner"],
    "T2.1": ["persona_goals", "persona_frequency", "persona_expertise", "persona_context", "persona_frustration"],
    "T2.2": ["journey_ids", "journey_flow", "failure_paths", "walking_skeleton_journey"],
    "T2.3": ["surface_items_decided", "surface_later_releases_named", "surface_exclusion_reasons"],
    "T2.4": ["release_mode", "pilot_users", "later_release_entry"],
    "T3.1": ["journey_step_coverage", "requirement_ids", "rules", "inputs_outputs", "validation", "errors"],
    "T3.2": ["entities_ownership", "volumes_growth", "sensitivity_class", "retention_residency", "source_of_truth"],
    "T3.3": ["integration_interface", "integration_limits", "integration_owner", "sandbox_availability", "down_behaviour"],
    "T3.4": ["scale_latency", "availability_recovery", "security_tenancy", "observability_cost", "accessibility"],
    "T3.5": ["failure_modes", "user_visible_behaviour", "operator_action"],
    "T4.1": ["candidate_approaches", "recommendation", "user_choice"],
    "T4.2": ["component_ids", "responsibility_interface", "component_data", "owner_role", "build_buy_reuse"],
    "T4.3": ["authn_authz", "tenancy_enforcement", "config_secrets", "observability_policy", "migration_versioning_errors"],
    "T4.4": ["runtime_per_environment", "merge_to_production_path", "rollback", "migration_ordering"],
    "T4.5": ["adr_options_decision", "consequences_reversibility", "licences_checked"],
    "T4.6": ["gate_per_claim", "gate_evidence", "gate_environment", "blocked_gates"],
    "T4.7": ["repo_branching_review", "definition_of_done", "test_expectations", "merge_gating_checks", "agent_roles"],
    "T6.1": ["walking_skeleton", "phase_order", "milestone_gates"],
    "T6.2": ["implementer_count", "task_size", "consultation_frequency", "reporting"],
    "T6.3": ["parked_tasks", "parking_owner", "parking_date"],
}


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
            self.assertTrue(2 <= len(topic["needs"]) <= 6, f"{topic_id}: {topic['needs']}")
            self.assertEqual(len(set(topic["needs"])), len(topic["needs"]), topic_id)
            for tag in topic["needs"]:
                self.assertRegex(tag, r"^[a-z][a-z0-9_]*$", topic_id)

    def test_the_ruling_that_every_kind_of_entry_counts_for_a_tag_is_written_down(self):
        phrase = "an unanswered question and an unconfirmed assumption"
        for path in (COVERAGE, ROOT / "kit" / "playbooks" / "01_discover.md", ROOT / "docs" / "DESIGN.md"):
            self.assertIn(phrase, " ".join(path.read_text(encoding="utf-8").split()), path.name)
        lines = COVERAGE.read_text(encoding="utf-8").splitlines()
        beside = lines[lines.index("condition_tags:") - 1]  # a comment line, directly above the key
        self.assertTrue(beside.startswith("# ") and phrase in beside, beside)

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

    def test_owners_in_the_map_are_exactly_the_five_lenses(self):
        owners = {t["owner"] for t in all_topics(load_map()).values()}
        self.assertEqual(owners, LENS_IDS)


class PinnedContentTest(unittest.TestCase):
    """Exact shapes pinned in this file, so a content change is a deliberate, reviewed edit."""

    def test_owner_per_topic(self):
        self.assertEqual({k: t["owner"] for k, t in all_topics(load_map()).items()}, OWNERS)

    def test_needs_per_topic(self):
        self.assertEqual({k: t["needs"] for k, t in all_topics(load_map()).items()}, NEEDS)

    def test_prompt_count_per_topic(self):
        counts = {k: len(v["prompts"]) for k, v in load_seeds().items()}
        self.assertEqual(counts, PROMPT_COUNTS)
        self.assertEqual(sum(counts.values()), 85)  # 84 bullets in the spec plus the T2.3 lead-in line

    def test_artifacts_per_stage(self):
        self.assertEqual({n: s["artifacts"] for n, s in load_map()["stages"].items()}, STAGE_ARTIFACTS)


class ForbiddenTokensTest(unittest.TestCase):
    def test_no_provider_names_in_interview_content(self):
        pattern = re.compile("|".join(map(re.escape, FORBIDDEN)), re.I)
        for path in [COVERAGE, SEEDS, *LENS_DIR.glob("*.md")]:
            self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")), path.name)


if __name__ == "__main__":
    unittest.main()
