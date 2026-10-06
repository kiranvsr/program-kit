import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import interview
import pk
from common import KitError, read_json, read_yaml, sha256_file, write_yaml

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "coverage_min.yaml"
SOURCES = {"S1": {"kind": "user", "ref": "message 1", "at": "2026-10-01T10:00:00Z"}}


def entry(ident, topics=("T1.1",), tags=(), **extra):
    kind = {"F": "fact", "A": "assumption", "D": "decision", "Q": "question"}.get(ident[0], "fact")
    base = {"id": ident, "kind": kind, "topics": list(topics), "tags": list(tags), "text": f"text of {ident}",
            "source": "S1", "at": "2026-10-01T10:00:00Z"}
    if kind == "assumption":
        base["confirmed"] = False
    if kind == "decision":
        base["ruling"] = None
    if kind == "question":
        base.update(lens="program", blocking=False)
    base.update(extra)
    return base


# closes T1.1 (measure + beneficiary)
CLOSING = [entry("F-1", tags=["measure"]), entry("F-2", tags=["beneficiary"])]


class InterviewBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "program").mkdir()
        (self.root / "kit" / "interview").mkdir(parents=True)
        shutil.copyfile(FIXTURE, self.root / "kit" / "interview" / "coverage.yaml")
        self.put_ledger([])

    def put_ledger(self, entries, sources=None):
        write_yaml(self.root / "program" / "ledger.yaml",
                   {"sources": dict(SOURCES if sources is None else sources), "entries": entries})

    def write(self, rel, text="content\n"):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def challenge(self, stage):
        """The (empty) challenge report of `stage`; stages 2 and later cannot be approved without one."""
        return interview.challenge_init(self.root, stage)

    def states(self, stage=1):
        return interview.topic_states(self.root, stage)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("interview.find_root", return_value=self.root):
            code = pk.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class TopicStateTests(InterviewBase):
    def test_topic_closes_when_all_needs_tags_present(self):
        self.assertEqual(self.states()["T1.1"], {"state": "open", "entries": [], "missing": ["measure", "beneficiary"]})
        self.put_ledger([entry("F-1", tags=["measure"])])
        self.assertEqual(self.states()["T1.1"], {"state": "open", "entries": ["F-1"], "missing": ["beneficiary"]})
        self.put_ledger([entry("F-1", tags=["measure"]), entry("D-1", tags=["beneficiary"])])
        self.assertEqual(self.states()["T1.1"], {"state": "closed", "entries": ["F-1", "D-1"], "missing": []})
        # a question never closes a topic, and entries for other topics do not count
        self.put_ledger([entry("F-1", tags=["measure"]), entry("Q-1", tags=["beneficiary"]),
                         entry("F-2", topics=["T2.1"], tags=["beneficiary"])])
        self.assertEqual(self.states()["T1.1"]["missing"], ["beneficiary"])

    def test_empty_needs_closes_on_any_fact_assumption_or_decision(self):
        self.assertEqual(self.states(2)["T2.1"]["state"], "open")
        self.put_ledger([entry("Q-1", topics=["T2.1"])])
        self.assertEqual(self.states(2)["T2.1"]["state"], "open")
        self.put_ledger([entry("F-1", topics=["T2.1"])])
        self.assertEqual(self.states(2)["T2.1"]["state"], "closed")

    def test_assumption_counts_only_when_confirmed(self):
        self.put_ledger([entry("F-1", tags=["measure"]), entry("A-1", tags=["beneficiary"])])
        self.assertEqual(self.states()["T1.1"]["state"], "open")
        self.assertEqual(self.states()["T1.1"]["missing"], ["beneficiary"])
        self.put_ledger([entry("F-1", tags=["measure"]), entry("A-1", tags=["beneficiary"], confirmed=True)])
        self.assertEqual(self.states()["T1.1"]["state"], "closed")

    def test_conditional_topic_not_applicable_without_tag(self):
        self.assertEqual(self.states()["T1.9"]["state"], "not_applicable")
        self.put_ledger([entry("F-1", topics=["T1.1"], tags=["payments"])])  # the tag may sit on any entry
        self.assertEqual(self.states()["T1.9"]["state"], "open")
        self.put_ledger([entry("F-1", tags=["regulated"]), entry("F-2", topics=["T1.9"], tags=["regulation"])])
        self.assertEqual(self.states()["T1.9"]["state"], "closed")
        # not applicable wins over everything else
        self.put_ledger([entry("D-1", topics=["T1.9"], tags=["deferred"], owner="pm", due="2026-11-01")])
        self.assertEqual(self.states()["T1.9"]["state"], "not_applicable")

    def test_deferred_topic_requires_owner_and_due(self):
        self.put_ledger([entry("D-1", tags=["deferred"], owner="pm")])
        self.assertEqual(self.states()["T1.1"]["state"], "open")
        self.assertEqual(interview.ledger_validate(self.root), ["D-1: deferred entry needs a non-empty owner and due"])
        self.put_ledger([entry("D-1", tags=["deferred"], owner="pm", due="2026-11-01")])
        self.assertEqual(self.states()["T1.1"]["state"], "deferred")
        self.assertEqual(interview.ledger_validate(self.root), [])
        # a deferred entry on one topic does not defer another
        self.assertEqual(self.states(2)["T2.1"]["state"], "open")


class ConditionTests(unittest.TestCase):
    def test_condition_evaluator(self):
        ev = interview.evaluate_condition
        tags = {"a", "b"}
        self.assertTrue(ev("has:a", tags))
        self.assertFalse(ev("has:c", tags))
        self.assertTrue(ev("has:a and has:b", tags))
        self.assertFalse(ev("has:a and has:c", tags))
        self.assertTrue(ev("has:c or has:b", tags))
        self.assertTrue(ev("not has:c", tags))
        self.assertTrue(ev("has:a and not has:c or has:c", tags))
        self.assertFalse(ev("not (has:a or has:c)", tags))
        self.assertTrue(ev(None, tags))
        for bad in ("has:a and", "a", "has:a has:b", "(has:a", "has:", "__import__('os')"):
            with self.assertRaises(KitError, msg=bad):
                ev(bad, tags)


class CoverageTests(InterviewBase):
    def test_load_coverage_default_and_explicit_path(self):
        coverage = interview.load_coverage(self.root)
        self.assertEqual(list(coverage["stages"]), [1, 2])
        self.assertEqual(coverage["stages"][1]["artifacts"], ["discovery.md"])
        self.assertEqual(list(coverage["stages"][1]["topics"]), ["T1.1", "T1.9"])
        other = self.root / "other.yaml"
        other.write_text("stages:\n  3:\n    name: x\n    budget: 1\n    topics: {}\n", encoding="utf-8")
        self.assertEqual(list(interview.load_coverage(self.root, other)["stages"]), [3])

    def test_load_coverage_rejects_bad_maps(self):
        other = self.root / "bad.yaml"
        for text, needle in (
                ("stages: []\n", "stages"),
                ("stages:\n  1:\n    name: x\n    budget: 1\n    topics:\n      T1.1:\n        title: a\n        owner: nobody\n",
                 "owner"),
                ("stages:\n  1:\n    name: x\n    budget: 1\n    topics:\n      T1.1:\n        title: a\n        owner: program\n"
                 "        condition: has:a and\n", "condition"),
                ("stages:\n  1:\n    name: x\n    budget: 1\n    topics:\n      T1.1:\n        title: a\n        owner: program\n"
                 "  2:\n    name: y\n    budget: 1\n    topics:\n      T1.1:\n        title: a\n        owner: program\n",
                 "duplicate")):
            other.write_text(text, encoding="utf-8")
            with self.assertRaises(KitError, msg=text) as ctx:
                interview.load_coverage(self.root, other)
            self.assertIn(needle, str(ctx.exception))
        with self.assertRaises(KitError):
            interview.load_coverage(self.root, self.root / "missing.yaml")


class StoppingRuleTests(InterviewBase):
    def test_stopping_rule_lists_blocking_question_and_open_required(self):
        self.put_ledger([entry("F-1", tags=["measure"]), entry("Q-3", tags=["beneficiary"], blocking=True),
                         entry("Q-4", blocking=False), entry("Q-5", topics=["T2.1"], blocking=True)])
        self.assertEqual(interview.stopping_rule(self.root, 1), [
            "T1.1: required topic open (missing: beneficiary)", "Q-3: blocking question unanswered"])
        # answered by a later fact listing it, or by the flag
        self.put_ledger([entry("F-1", tags=["measure"]), entry("Q-3", blocking=True),
                         entry("F-2", tags=["beneficiary"], answers=["Q-3"])])
        self.assertEqual(interview.stopping_rule(self.root, 1), [])
        self.put_ledger(CLOSING + [entry("Q-3", blocking=True, answered=True)])
        self.assertEqual(interview.stopping_rule(self.root, 1), [])
        # an answer that comes before the question does not count
        self.put_ledger(CLOSING + [entry("F-3", answers=["Q-3"]), entry("Q-3", blocking=True)])
        self.assertEqual(interview.stopping_rule(self.root, 1), ["Q-3: blocking question unanswered"])

    def test_deferred_and_optional_topics_do_not_block(self):
        self.put_ledger(CLOSING)
        self.assertEqual(interview.stopping_rule(self.root, 1), [])
        self.put_ledger([entry("D-1", tags=["deferred"], owner="pm", due="2026-11-01")])
        self.assertEqual(interview.stopping_rule(self.root, 1), [])

    def test_unresolved_challenge_findings_block(self):
        self.put_ledger(CLOSING)
        path = interview.challenge_init(self.root, 1)
        self.assertEqual(path, self.root / "program" / "reports" / "stage-1-challenge.md")
        self.assertEqual(interview.stopping_rule(self.root, 1), [])  # an empty table has no findings
        text = path.read_text(encoding="utf-8")
        for lens in interview.LENSES:
            self.assertIn(f"## {lens}\n", text)
        self.assertEqual(text.count("| # | Finding | Severity | Resolution |"), 5)
        rows = "| 1 | no target | high |  |\n| 2 | no owner | low | answered in F-9 |\n| 3 | a | b | |\n"
        path.write_text(text.rstrip("\n") + "\n" + rows, encoding="utf-8")
        self.assertEqual(interview.stopping_rule(self.root, 1), ["stage-1-challenge.md: 2 findings unresolved"])
        with self.assertRaises(KitError):
            interview.challenge_init(self.root, 1)  # never overwrites

    def test_missing_challenge_report_blocks_from_stage_2(self):
        self.put_ledger(CLOSING + [entry("F-3", topics=["T2.1"])])
        self.assertEqual(interview.stopping_rule(self.root, 1), [])  # stage 1 has no challenge round
        self.assertEqual(interview.stopping_rule(self.root, 2), ["stage-2-challenge.md: missing"])
        self.challenge(2)
        self.assertEqual(interview.stopping_rule(self.root, 2), [])

    def test_invalid_ledger_is_listed_first_and_blocks_approval(self):
        artifact = self.write("program/discovery.md")
        self.put_ledger(CLOSING + [entry("F-3", source="S9"), entry("Q-9", blocking=True)])
        expected = ["F-3: source S9 is not in sources", "Q-9: blocking question unanswered"]
        self.assertEqual(interview.stopping_rule(self.root, 1), expected)
        with self.assertRaises(KitError) as ctx:
            interview.approve(self.root, 1, [artifact])
        self.assertEqual(str(ctx.exception), "; ".join(expected))
        self.assertFalse((self.root / "program" / "approvals").exists())

    def test_only_facts_decisions_and_confirmed_assumptions_answer_a_question(self):
        base = CLOSING + [entry("Q-3", blocking=True)]
        for answerer, answered in ((entry("F-3", answers=["Q-3"]), True), (entry("D-3", answers=["Q-3"]), True),
                                   (entry("A-3", answers=["Q-3"], confirmed=True), True),
                                   (entry("A-3", answers=["Q-3"]), False),
                                   (entry("Q-4", answers=["Q-3"]), False)):
            self.put_ledger(base + [answerer])
            self.assertEqual(interview.stopping_rule(self.root, 1),
                             [] if answered else ["Q-3: blocking question unanswered"], answerer["id"])

    def test_unconfirmed_assumption_blocks_stage_5(self):
        self.put_ledger([entry("A-4", topics=["T2.1"]), entry("A-5", topics=["T2.1"], confirmed=True)])
        for stage in (4, 5, 6):
            self.challenge(stage)
        self.assertEqual(interview.stopping_rule(self.root, 4), [])
        with mock.patch("trace.validate", return_value=[]):
            self.assertEqual(interview.stopping_rule(self.root, 5), ["A-4: assumption unconfirmed at stage 5"])
            self.assertEqual(interview.stopping_rule(self.root, 6), ["A-4: assumption unconfirmed at stage 6"])

    def test_stage_5_and_6_include_trace_problems(self):
        self.challenge(5)
        with mock.patch("trace.validate", return_value=["REQ-001: names no component"]) as validate:
            self.assertEqual(interview.stopping_rule(self.root, 5), ["REQ-001: names no component"])
            validate.assert_called_once_with(self.root, 5)
            self.assertEqual(interview.stopping_rule(self.root, 4), ["stage-4-challenge.md: missing"])

    def test_stage_7_includes_register_problems_when_register_exists(self):
        self.challenge(7)
        self.assertEqual(interview.stopping_rule(self.root, 7), ["program/register.yaml: missing"])
        write_yaml(self.root / "program" / "register.yaml", {"phases": [{"id": "P1", "title": "a", "milestones": []}]})
        with mock.patch("register.validate", return_value=["P1: has no milestones"]):
            self.assertEqual(interview.stopping_rule(self.root, 7), ["P1: has no milestones"])


class ApprovalTests(InterviewBase):
    def approve_one(self):
        self.put_ledger(CLOSING)
        artifact = self.write("program/discovery.md", "v1\n")
        interview.approve(self.root, 1, [artifact])
        return artifact

    def test_approve_refuses_then_writes_hashes(self):
        artifact = self.write("program/discovery.md", "v1\n")
        with self.assertRaises(KitError) as ctx:
            interview.approve(self.root, 1, [artifact])
        self.assertEqual(str(ctx.exception), "T1.1: required topic open (missing: measure, beneficiary)")
        self.assertFalse((self.root / "program" / "approvals").exists())
        self.put_ledger(CLOSING)
        path = interview.approve(self.root, 1, [artifact])
        self.assertEqual(path, self.root / "program" / "approvals" / "stage-1.json")
        record = read_json(path)
        self.assertEqual(sorted(record), ["artifacts", "at", "ledger_sha256", "stage"])
        self.assertEqual(record["stage"], 1)
        self.assertRegex(record["at"], r"\A\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")
        self.assertEqual(record["artifacts"], {"program/discovery.md": sha256_file(artifact)})
        self.assertEqual(record["ledger_sha256"], sha256_file(self.root / "program" / "ledger.yaml"))

    def test_approve_rejects_bad_artifacts(self):
        self.put_ledger(CLOSING)
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "x.md").write_text("x", encoding="utf-8")
        with self.assertRaises(KitError) as ctx:
            interview.approve(self.root, 1, [outside / "x.md"])
        self.assertIn("outside", str(ctx.exception))
        with self.assertRaises(KitError):
            interview.approve(self.root, 1, [self.root / "program" / "missing.md"])
        self.assertFalse((self.root / "program" / "approvals").exists())
        self.write("program/discovery.md")
        path = interview.approve(self.root, 1, [Path("program/discovery.md")])  # relative paths are root-relative
        self.assertEqual(list(read_json(path)["artifacts"]), ["program/discovery.md"])

    def test_next_stage_detects_modified_artifact(self):
        self.assertEqual(interview.next_stage(self.root), 1)
        artifact = self.approve_one()
        self.assertEqual(interview.next_stage(self.root), 2)
        artifact.write_text("v2\n", encoding="utf-8")
        self.assertEqual(interview.next_stage(self.root), 1)
        artifact.write_text("v1\n", encoding="utf-8")
        self.assertEqual(interview.next_stage(self.root), 2)
        artifact.unlink()
        self.assertEqual(interview.next_stage(self.root), 1)

    def test_ledger_changes_only_matter_after_the_freeze(self):
        artifact = self.approve_one()
        # stages 1 to 4 compare artifact hashes only
        self.put_ledger(CLOSING + [entry("F-9", tags=["extra"])])
        self.assertEqual(interview.next_stage(self.root), 2)
        product = self.write("program/product.md")
        self.challenge(2)
        stage2 = CLOSING + [entry("F-9"), entry("F-10", topics=["T2.1"])]
        self.put_ledger(stage2)
        interview.approve(self.root, 2, [product])
        # entries added while working on stage 3 do not make stage 2 stale
        self.put_ledger(stage2 + [entry("F-11")])
        self.assertEqual(interview.next_stage(self.root), 3)
        for stage in (3, 4):
            self.challenge(stage)
            interview.approve(self.root, stage, [product])
        self.put_ledger(stage2 + [entry("F-11"), entry("F-12")])
        self.assertEqual(interview.next_stage(self.root), 5)
        self.challenge(5)
        with mock.patch("trace.validate", return_value=[]):
            interview.approve(self.root, 5, [product])
            self.assertEqual(interview.next_stage(self.root), 6)
            self.put_ledger(stage2 + [entry("F-11"), entry("F-12"), entry("F-14")])  # a change after the freeze
            self.assertEqual(interview.next_stage(self.root), 5)
            self.put_ledger(stage2 + [entry("F-11"), entry("F-12")])
            self.challenge(6)
            interview.approve(self.root, 6, [product])
            self.challenge(7)
            write_yaml(self.root / "program" / "register.yaml", {"phases": []})
            with mock.patch("register.validate", return_value=[]):
                interview.approve(self.root, 7, [product])
        self.assertEqual(interview.next_stage(self.root), 8)
        # a ledger change after the freeze makes the highest frozen approval stale, and only that one
        self.put_ledger(stage2 + [entry("F-11"), entry("F-12"), entry("F-13")])
        self.assertEqual(interview.next_stage(self.root), 7)
        # an artifact change is caught at any stage
        self.put_ledger(stage2 + [entry("F-11"), entry("F-12")])
        self.assertEqual(interview.next_stage(self.root), 8)
        artifact.write_text("changed\n", encoding="utf-8")
        self.assertEqual(interview.next_stage(self.root), 1)

    def test_amend_rehashes_and_reports_changed_paths(self):
        artifact = self.approve_one()
        other = self.write("program/CLAIM_BOUNDARIES.md", "c1\n")
        interview.approve(self.root, 1, [artifact, other])
        self.assertEqual(interview.amend(self.root, 1), [])
        before = read_json(self.root / "program" / "approvals" / "stage-1.json")
        artifact.write_text("v2\n", encoding="utf-8")
        self.assertEqual(interview.amend(self.root, 1), ["program/discovery.md"])
        after = read_json(self.root / "program" / "approvals" / "stage-1.json")
        self.assertEqual(after["artifacts"]["program/discovery.md"], sha256_file(artifact))
        self.assertEqual(after["artifacts"]["program/CLAIM_BOUNDARIES.md"],
                         before["artifacts"]["program/CLAIM_BOUNDARIES.md"])
        self.assertEqual(interview.next_stage(self.root), 2)
        self.put_ledger(CLOSING + [entry("F-9")])
        other.write_text("c2\n", encoding="utf-8")
        self.assertEqual(interview.amend(self.root, 1), ["program/CLAIM_BOUNDARIES.md", "program/ledger.yaml"])
        with self.assertRaises(KitError):
            interview.amend(self.root, 2)  # nothing approved to amend


class LedgerValidateTests(InterviewBase):
    def test_ledger_refuses_question_without_lens_and_entry_without_source(self):
        bad_question = entry("Q-1")
        del bad_question["lens"]
        no_source = entry("F-1")
        del no_source["source"]
        self.put_ledger([bad_question, no_source, entry("F-2", source="S9")])
        problems = interview.ledger_validate(self.root)
        self.assertEqual(problems, ["Q-1: question needs a lens (one of architect, engineering, program, project, delivery)",
                                    "F-1: source is missing", "F-2: source S9 is not in sources"])

    def test_ledger_validate_other_checks(self):
        no_at = entry("F-3")
        del no_at["at"]
        self.put_ledger([
            entry("F-1"), entry("F-1"), entry("X-1"), entry("F-4", kind="decision"), entry("F-5", topics=[]),
            entry("F-6", topics=["T9.9"]), no_at, entry("A-1", confirmed="yes"), entry("D-1", ruling="7"),
            entry("Q-1", lens="boss"), entry("Q-2", blocking="no"), entry("F-7", topics=["T1.1", "T2.1"])])
        problems = interview.ledger_validate(self.root)
        self.assertEqual(problems, [
            "F-1: duplicate id",
            "X-1: id must look like F-<n>, A-<n>, D-<n> or Q-<n>",
            "F-4: id prefix does not match kind decision",
            "F-5: topics must be a non-empty list",
            "F-6: topic T9.9 is not in the coverage map",
            "F-3: at is missing",
            "A-1: confirmed must be true or false",
            "D-1: ruling must be an integer or null",
            "Q-1: question needs a lens (one of architect, engineering, program, project, delivery)",
            "Q-2: blocking must be true or false"])
        self.put_ledger([entry("F-1", kind="rumour")])
        self.assertEqual(interview.ledger_validate(self.root),
                         ["F-1: kind must be fact, assumption, decision or question"])

    def test_ledger_validate_field_types_and_sources(self):
        no_tags, no_text, str_tags = entry("F-1"), entry("F-2"), entry("F-3")
        del no_tags["tags"]
        str_tags["tags"] = "a"
        no_text["text"] = " "
        sources = {"S1": {"kind": "user", "ref": "m", "at": "t"}, "S2": {"kind": "guess", "ref": "m", "at": "t"},
                   "S3": {"kind": "user", "ref": 5}, "S4": "x"}
        self.put_ledger([no_tags, no_text, str_tags, entry("Q-1"),
                         entry("F-4", answers=["Q-1", "Q-7", "F-1"]), entry("F-5", answers="Q-1"),
                         entry("Q-2", answered="yes")], sources=sources)
        self.assertEqual(interview.ledger_validate(self.root), [
            "S2: source kind must be user, document or inference",
            "S3: source ref must be a string", "S3: source at must be a string",
            "S4: source must be a mapping",
            "F-1: tags must be a list of strings", "F-2: text must be a non-empty string",
            "F-3: tags must be a list of strings",
            "F-4: answers Q-7 which is not a question in the ledger",
            "F-4: answers F-1 which is not a question in the ledger",
            "F-5: answers must be a list of question ids",
            "Q-2: answered must be true or false"])

    def test_wrong_shape_raises(self):
        for data in ([], {"sources": {}, "entries": {}}, {"sources": [], "entries": []},
                     {"sources": {}, "entries": ["F-1"]}):
            write_yaml(self.root / "program" / "ledger.yaml", data)
            with self.assertRaises(KitError, msg=data):
                interview.ledger_validate(self.root)

    def test_missing_ledger_is_empty(self):
        (self.root / "program" / "ledger.yaml").unlink()
        self.assertEqual(interview.ledger_validate(self.root), [])
        self.assertEqual(self.states()["T1.1"]["state"], "open")


class ReportTests(InterviewBase):
    def test_report_renders_table_and_is_deterministic(self):
        self.put_ledger([entry("F-1", tags=["measure"]), entry("Q-1", blocking=True), entry("Q-2"),
                         entry("Q-3", topics=["T2.1"])])
        path = interview.report(self.root, 1)
        self.assertEqual(path, self.root / "program" / "reports" / "stage-1.md")
        first = path.read_bytes()
        text = path.read_text(encoding="utf-8")
        self.assertIn("# Stage 1 coverage report: discover", text)
        self.assertIn("questions asked: 2 of budget 4", text)
        self.assertIn("| Topic | Title | Owner | State | Missing | Entries |", text)
        self.assertIn("| T1.1 | Outcome | program | open | beneficiary | F-1, Q-1, Q-2 |", text)
        self.assertIn("| T1.9 | Regulatory and contractual | program | not_applicable |  |  |", text)
        self.assertNotIn("$", text)
        interview.report(self.root, 1)
        self.assertEqual(first, path.read_bytes())


class SourceTests(InterviewBase):
    def test_ingest_copies_file_and_adds_a_source_only(self):
        doc = self.write("incoming/spec.pdf.txt", "hello\n")
        source_id = interview.ingest(self.root, doc)
        self.assertEqual(source_id, "S2")  # S1 exists already
        stored = self.root / "program" / "sources" / (interview.sha256_bytes(b"hello\n")[:12] + "-spec.pdf.txt")
        self.assertEqual(stored.read_bytes(), b"hello\n")
        ledger = read_yaml(self.root / "program" / "ledger.yaml")
        self.assertEqual(ledger["entries"], [])
        record = ledger["sources"]["S2"]
        self.assertEqual(record["kind"], "document")
        self.assertEqual(record["ref"], "program/sources/" + stored.name)
        self.assertRegex(record["at"], r"\A\d{4}-\d\d-\d\dT")
        self.assertEqual(interview.ingest(self.root, doc), "S3")

    def test_ingest_failure_leaves_no_orphan_and_ledger_unchanged(self):
        doc = self.write("incoming/notes.md", "hello\n")
        with mock.patch("interview.write_yaml", side_effect=KitError("boom")):
            with self.assertRaises(KitError):
                interview.ingest(self.root, doc)
        self.assertEqual(list((self.root / "program" / "sources").iterdir()), [])
        self.assertEqual(read_yaml(self.root / "program" / "ledger.yaml")["sources"], SOURCES)
        self.assertEqual(len(list((self.root / "program" / "sources").iterdir())), 0)
        self.assertEqual(interview.ingest(self.root, doc), "S2")
        self.assertEqual(len(list((self.root / "program" / "sources").iterdir())), 1)

    def test_ingest_refusals(self):
        bad_name = self.write("incoming/-weird name.txt")
        with self.assertRaises(KitError):
            interview.ingest(self.root, bad_name)
        real = self.write("incoming/real.txt")
        link = self.root / "incoming" / "link.txt"
        link.symlink_to(real)
        with self.assertRaises(KitError):
            interview.ingest(self.root, link)
        with self.assertRaises(KitError):
            interview.ingest(self.root, self.root / "incoming" / "missing.txt")
        big = self.write("incoming/big.bin")
        with mock.patch.object(interview, "MAX_SOURCE_BYTES", 3):
            with self.assertRaises(KitError):
                interview.ingest(self.root, big)
        self.assertFalse((self.root / "program" / "sources").exists())
        self.assertEqual(read_yaml(self.root / "program" / "ledger.yaml")["sources"], SOURCES)


class ReplayTests(InterviewBase):
    def make_inputs(self, entries):
        recorded = self.root / "recorded.yaml"
        write_yaml(recorded, {"sources": SOURCES, "entries": entries})
        artifacts = self.root / "artifacts"
        artifacts.mkdir()
        (artifacts / "discovery.md").write_text("d\n", encoding="utf-8")
        (artifacts / "product.md").write_text("p\n", encoding="utf-8")
        (artifacts / "reports").mkdir()
        (artifacts / "reports" / "stage-2-challenge.md").write_text(
            "# challenge\n\n| # | Finding | Severity | Resolution |\n|---|---|---|---|\n", encoding="utf-8")
        return recorded, artifacts

    def test_replay_approves_in_order_and_stops_on_first_gap(self):
        recorded, artifacts = self.make_inputs(CLOSING + [entry("F-3", topics=["T2.1"])])
        # stale approvals disappear first
        stale = self.root / "program" / "approvals" / "stage-2.json"
        stale.parent.mkdir()
        stale.write_text("{}", encoding="utf-8")
        interview.replay(self.root, recorded, artifacts)
        self.assertEqual(interview.next_stage(self.root), 3)
        self.assertEqual(sha256_file(self.root / "program" / "ledger.yaml"), sha256_file(recorded))
        approval = read_json(self.root / "program" / "approvals" / "stage-2.json")
        self.assertEqual(list(approval["artifacts"]), ["program/product.md"])
        self.assertEqual((self.root / "program" / "discovery.md").read_text(encoding="utf-8"), "d\n")

        write_yaml(recorded, {"sources": SOURCES, "entries": CLOSING})  # stage 2 has no entry for T2.1
        with self.assertRaises(KitError) as ctx:
            interview.replay(self.root, recorded, artifacts)
        self.assertEqual(str(ctx.exception), "replay stopped at stage 2: T2.1: required topic open (missing: any fact, confirmed assumption or decision)")
        self.assertTrue((self.root / "program" / "approvals" / "stage-1.json").exists())
        self.assertFalse((self.root / "program" / "approvals" / "stage-2.json").exists())

    def test_replay_stops_without_a_challenge_report_and_refuses_reserved_names(self):
        recorded, artifacts = self.make_inputs(CLOSING + [entry("F-3", topics=["T2.1"])])
        (artifacts / "reports" / "stage-2-challenge.md").unlink()
        with self.assertRaises(KitError) as ctx:
            interview.replay(self.root, recorded, artifacts)
        self.assertEqual(str(ctx.exception), "replay stopped at stage 2: stage-2-challenge.md: missing")
        for name in ("ledger.yaml", "approvals"):
            shutil.rmtree(self.root / "program")
            (self.root / "program").mkdir()
            target = artifacts / name
            target.mkdir() if name == "approvals" else target.write_text("x", encoding="utf-8")
            with self.assertRaises(KitError) as ctx:
                interview.replay(self.root, recorded, artifacts)
            self.assertIn(name, str(ctx.exception))
            self.assertEqual(list((self.root / "program").iterdir()), [])  # nothing written
            shutil.rmtree(target) if name == "approvals" else target.unlink()

    def test_replay_rejects_invalid_ledger_and_missing_artifact(self):
        recorded, artifacts = self.make_inputs([entry("F-1", source="S9")])
        with self.assertRaises(KitError) as ctx:
            interview.replay(self.root, recorded, artifacts)
        self.assertIn("F-1: source S9 is not in sources", str(ctx.exception))
        write_yaml(recorded, {"sources": SOURCES, "entries": CLOSING})
        (artifacts / "discovery.md").unlink()
        with self.assertRaises(KitError) as ctx:
            interview.replay(self.root, recorded, artifacts)
        self.assertIn("stage 1", str(ctx.exception))


class CliTests(InterviewBase):
    def test_cli_round_trip(self):
        code, out, err = self.run_cli("interview", "next")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[0], "next stage: 1 (discover)")
        self.put_ledger(CLOSING)
        self.write("program/discovery.md", "v1\n")
        code, out, err = self.run_cli("interview", "validate")
        self.assertEqual((code, out.strip(), err), (0, "ledger valid: 2 entries", ""))
        code, out, err = self.run_cli("interview", "report", "1")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["program/reports/stage-1.md", "questions asked: 0 of budget 4"])
        code, out, err = self.run_cli("interview", "challenge", "1")
        self.assertEqual((code, out.strip()), (0, "program/reports/stage-1-challenge.md"))
        code, out, err = self.run_cli("interview", "challenge", "1")
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("error: "))
        code, out, err = self.run_cli("interview", "approve", "1")  # artifacts default to the coverage map's list
        self.assertEqual((code, out.strip(), err), (0, "approved stage 1: program/approvals/stage-1.json", ""))
        code, out, _ = self.run_cli("interview", "next")
        self.assertEqual(out.splitlines()[0], "next stage: 2 (define the product)")
        self.assertEqual(self.run_cli("interview", "amend", "1")[1], "no changes\n")
        self.write("program/discovery.md", "v2\n")
        self.assertEqual(self.run_cli("interview", "amend", "1")[1], "program/discovery.md\n")
        doc = self.write("incoming/notes.md")
        code, out, _ = self.run_cli("interview", "ingest", str(doc))
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("S2 program/sources/"))

    def test_cli_errors_are_one_line(self):
        self.put_ledger([entry("F-1", source="S9")])
        code, out, err = self.run_cli("interview", "validate")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "error: F-1: source S9 is not in sources\n")
        code, out, err = self.run_cli("interview", "approve", "1", "--artifact", "program/discovery.md")
        self.assertEqual(code, 1)
        self.assertEqual(err.count("\n"), 1)
        code, out, err = self.run_cli("interview", "report", "9")
        self.assertEqual((code, out), (1, ""))
        code, out, err = self.run_cli("interview")
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
