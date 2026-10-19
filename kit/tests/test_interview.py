import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import briefs
import interview
import pk
from common import KitError, read_json, read_yaml, sha256_file, write_yaml
from test_register import reg, task

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


# a challenge report every lens has answered, as a replay installs it
FINISHED_CHALLENGE = "# challenge\n\n" + "".join(
    f"## {lens}\n\n| # | Finding | Severity | Resolution |\n|---|---|---|---|\n\nNo findings.\n\n"
    for lens in interview.LENSES)

# closes T1.1 (measure + beneficiary)
CLOSING = [entry("F-1", tags=["measure"]), entry("F-2", tags=["beneficiary"])]


def amended(stage, *paths):
    """What `pk interview amend` prints when it re-hashed `paths`: each path, then one summary line (KF-5)."""
    return "".join(f"{path}\n" for path in paths) + (
        f"amended stage {stage}: {len(paths)} changed file(s) re-hashed into program/approvals/stage-{stage}.json\n")


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
        """A finished challenge report of `stage` (every lens: no findings); stages 2 and later need one."""
        path = interview.challenge_init(self.root, stage)
        path.write_text(path.read_text(encoding="utf-8").replace(
            "|---|---|---|---|", "|---|---|---|---|\n\nNo findings."), encoding="utf-8")
        return path

    def put_register(self, *tasks):
        """A register `stopping_rule` accepts at stage 6 (the default is one task needing no environment), with its briefs."""
        write_yaml(self.root / "program" / "register.yaml", reg(*(tasks or (task("P1-M01-S01-T01"),))))
        briefs.generate(self.root)

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

    def test_a_condition_tag_counts_from_every_kind_of_entry(self):
        """Controller ruling: switching a conditional topic on too early is the safe direction."""
        self.assertEqual(self.states()["T1.9"]["state"], "not_applicable")
        for item in (entry("Q-1", topics=["T2.1"], tags=["regulated"]),  # a question nobody has answered
                     entry("A-1", topics=["T2.1"], tags=["payments"], confirmed=False),
                     entry("A-2", topics=["T2.1"], tags=["regulated"], confirmed=None),
                     entry("D-1", topics=["T2.1"], tags=["regulated"]),
                     entry("F-1", topics=["T2.1"], tags=["regulated"])):
            with self.subTest(entry=item["id"]):
                self.put_ledger([item])
                self.assertEqual(self.states()["T1.9"]["state"], "open")

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
        text = path.read_text(encoding="utf-8")
        for lens in interview.LENSES:
            self.assertIn(f"## {lens}\n", text)
        self.assertEqual(text.count("| # | Finding | Severity | Resolution |"), 5)
        text = text.replace("|---|---|---|---|", "|---|---|---|---|\n\nNo findings.")
        rows = "| 1 | no target | high |  |\n| 2 | no owner | low | answered in F-9 |\n| 3 | a | b | |\n"
        path.write_text(text.rstrip("\n") + "\n" + rows, encoding="utf-8")
        self.assertEqual(interview.stopping_rule(self.root, 1), ["stage-1-challenge.md: 2 findings unresolved"])
        with self.assertRaises(KitError):
            interview.challenge_init(self.root, 1)  # never overwrites

    def test_a_challenge_report_with_a_byte_order_mark_reads_like_one_without(self):
        self.put_ledger(CLOSING)
        path = interview.challenge_init(self.root, 1)
        skeleton = path.read_text(encoding="utf-8")
        finished = "".join(f"## {lens}\n\n| # | Finding | Severity | Resolution |\n|---|---|---|---|\n\nNo findings.\n\n"
                           for lens in interview.LENSES)  # starts on a heading: a mark would hide the first one
        for name, text, expected in (("skeleton", skeleton, interview.stopping_rule(self.root, 1)),
                                     ("finished", finished, [])):
            with self.subTest(report=name):
                path.write_text(text, encoding="utf-8")
                plain = interview.stopping_rule(self.root, 1)
                self.assertEqual(plain, expected)
                path.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
                self.assertEqual(interview.stopping_rule(self.root, 1), plain)
        self.assertEqual(interview.stopping_rule(self.root, 1), [])
        path.write_bytes(b"\xff\xfe not utf-8")
        with self.assertRaises(KitError) as caught:
            interview.stopping_rule(self.root, 1)
        self.assertTrue(str(caught.exception).startswith(f"cannot read {path}: "), str(caught.exception))
        self.assertEqual(len(str(caught.exception).splitlines()), 1)

    def test_a_template_with_a_byte_order_mark_does_not_put_one_in_the_report(self):
        kit = self.root / "kitcopy"
        shutil.copytree(interview.KIT_DIR / "templates", kit / "templates")
        for name in ("challenge_report.md", "stage_report.md"):
            template = kit / "templates" / name
            template.write_bytes(b"\xef\xbb\xbf" + template.read_bytes())
        self.put_ledger(CLOSING)
        with mock.patch("interview.KIT_DIR", kit):
            challenge = interview.challenge_init(self.root, 1)
            report = interview.report(self.root, 1)
        for path in (challenge, report):
            self.assertFalse(path.read_bytes().startswith(b"\xef\xbb\xbf"), path.name)

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
        self.put_ledger(CLOSING + [entry("A-4", topics=["T2.1"]), entry("A-5", topics=["T2.1"], confirmed=True)])
        for stage in (4, 5, 6):
            self.challenge(stage)
        self.put_register()
        self.assertEqual(interview.stopping_rule(self.root, 4), [])
        with mock.patch("trace.validate", return_value=[]):
            self.assertEqual(interview.stopping_rule(self.root, 5), ["A-4: assumption unconfirmed at stage 5"])
            self.assertEqual(interview.stopping_rule(self.root, 6), ["A-4: assumption unconfirmed at stage 6"])

    def test_stage_5_and_6_include_trace_problems(self):
        self.put_ledger(CLOSING + [entry("F-3", topics=["T2.1"])])  # the earlier stages are closed
        self.challenge(5)
        with mock.patch("trace.validate", return_value=["REQ-001: names no component"]) as validate:
            self.assertEqual(interview.stopping_rule(self.root, 5), ["REQ-001: names no component"])
            validate.assert_called_once_with(self.root, 5)
            self.assertEqual(interview.stopping_rule(self.root, 4), ["stage-4-challenge.md: missing"])

    def test_stage_7_is_per_task_and_never_approved(self):
        message = ("stage 7 is per task: run playbooks 07 and 08 for each task; there is nothing to approve")
        for call in (lambda: interview.stopping_rule(self.root, 7), lambda: interview.approve(self.root, 7, []),
                     lambda: interview.challenge_init(self.root, 7), lambda: interview.amend(self.root, 7),
                     lambda: interview.report(self.root, 7)):
            with self.assertRaises(KitError) as caught:
                call()
            self.assertEqual(str(caught.exception), message)
        self.assertFalse((self.root / "program" / "approvals" / "stage-7.json").exists())
        self.assertFalse((self.root / "program" / "reports" / "stage-7-challenge.md").exists())


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
            self.put_register()
            interview.approve(self.root, 6, [product])
        # stage 7 is never approved: once stages 1 to 6 are current, 7 is the final value
        self.assertEqual(interview.next_stage(self.root), 7)
        # an entry on an earlier-stage topic added after approval 6 names the freeze, not only stage 6: the
        # stage 5 digest is still compared, so amending stage 6 alone cannot absorb it
        self.put_ledger(stage2 + [entry("F-11"), entry("F-12"), entry("F-13")])
        self.assertEqual(interview.next_stage(self.root), 5)
        # an artifact change is caught at any stage
        self.put_ledger(stage2 + [entry("F-11"), entry("F-12")])
        self.assertEqual(interview.next_stage(self.root), 7)
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


    def test_report_lists_the_assumptions_still_unconfirmed(self):
        path = interview.report(self.root, 1)
        self.assertIn("## Unconfirmed assumptions\n\nnone\n", path.read_text(encoding="utf-8"))
        self.put_ledger([entry("A-1"), entry("A-2", confirmed=True),
                         entry("A-3", topics=["T2.1", "T1.1"], text="a | b\n c")])
        text = interview.report(self.root, 1).read_text(encoding="utf-8")
        self.assertIn("## Unconfirmed assumptions\n\n- A-1 (T1.1): text of A-1\n- A-3 (T2.1, T1.1): a | b c\n", text)
        self.assertNotIn("A-2", text.split("## Unconfirmed assumptions")[1])


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

    def failing_replace(self, name):
        """os.replace that fails when it would put a file called `name` in place (the ledger or the stored copy)."""
        real = interview.os.replace

        def replace(src, dst, *args, **kwargs):
            if name == "ledger.yaml" and Path(dst).name == name or name == "stored" and "sources" in Path(dst).parts:
                raise PermissionError(13, "Permission denied")
            return real(src, dst, *args, **kwargs)

        return mock.patch("os.replace", side_effect=replace)

    def test_ingest_leaves_file_and_ledger_as_they_were_when_a_final_replace_fails(self):
        doc = self.write("incoming/notes.md", "hello\n")
        ledger = self.root / "program" / "ledger.yaml"
        sources = self.root / "program" / "sources"
        for name in ("ledger.yaml", "stored"):
            with self.subTest(failing=name):
                before = ledger.read_bytes()
                with self.failing_replace(name), self.assertRaises(KitError) as caught:
                    interview.ingest(self.root, doc)
                self.assertTrue(str(caught.exception).startswith("cannot write "), str(caught.exception))
                self.assertEqual(ledger.read_bytes(), before)  # no half-written ledger
                self.assertEqual(sorted(p.name for p in sources.iterdir()) if sources.exists() else [], [])
        self.assertEqual(interview.ingest(self.root, doc), "S2")  # nothing is left in the way
        # a stored copy that was there before the call is not a leftover of it
        stored = next(iter(sources.iterdir()))
        before = ledger.read_bytes()
        with self.failing_replace("ledger.yaml"), self.assertRaises(KitError):
            interview.ingest(self.root, doc)
        self.assertEqual(stored.read_bytes(), b"hello\n")
        self.assertEqual(ledger.read_bytes(), before)
        self.assertEqual([p.name for p in sources.iterdir()], [stored.name])

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
            FINISHED_CHALLENGE, encoding="utf-8")
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


class AdrApprovalTests(InterviewBase):
    """Stage 4 also hashes the ADRs (playbook 04, step 11), by default and in a replay."""

    def setUp(self):
        super().setUp()
        (self.root / "kit" / "interview" / "coverage.yaml").write_text(
            "stages:\n  4:\n    name: architect\n    budget: 0\n    artifacts:\n      - ARCHITECTURE.md\n    topics: {}\n",
            encoding="utf-8")
        self.challenge(4)
        self.write("program/ARCHITECTURE.md", "a\n")
        approvals = self.root / "program" / "approvals"
        approvals.mkdir()
        for stage in (1, 2, 3):  # the stages below 4 the one-stage coverage map does not list: current, empty records
            (approvals / f"stage-{stage}.json").write_text(
                json.dumps({"stage": stage, "at": "x", "artifacts": {}, "ledger_sha256": "x"}), encoding="utf-8")

    def test_default_approval_hashes_every_adr_in_name_order(self):
        self.write("program/adr/0002-b.md", "b\n")
        self.write("program/adr/0001-a.md", "a\n")
        self.write("program/adr/notes.txt", "not a record\n")
        code, _, err = self.run_cli("interview", "approve", "4")
        self.assertEqual((code, err), (0, ""))
        record = read_json(self.root / "program" / "approvals" / "stage-4.json")
        self.assertEqual(list(record["artifacts"]),
                         ["program/ARCHITECTURE.md", "program/adr/0001-a.md", "program/adr/0002-b.md"])
        self.write("program/adr/0001-a.md", "changed\n")  # an edited ADR is now a change after approval
        self.assertEqual(interview.amend(self.root, 4), ["program/adr/0001-a.md"])

    def test_approval_without_an_adr_folder_hashes_only_the_listed_files(self):
        interview.approve(self.root, 4, interview.stage_artifacts(
            self.root, 4, interview.load_coverage(self.root)["stages"][4]))
        record = read_json(self.root / "program" / "approvals" / "stage-4.json")
        self.assertEqual(list(record["artifacts"]), ["program/ARCHITECTURE.md"])

    def test_replay_hashes_the_adrs_it_installs(self):
        recorded = self.root / "recorded.yaml"
        write_yaml(recorded, {"sources": SOURCES, "entries": []})
        artifacts = self.root / "artifacts"
        (artifacts / "adr").mkdir(parents=True)
        (artifacts / "reports").mkdir()
        (artifacts / "ARCHITECTURE.md").write_text("a\n", encoding="utf-8")
        (artifacts / "adr" / "0001-a.md").write_text("adr\n", encoding="utf-8")
        (artifacts / "reports" / "stage-4-challenge.md").write_text(
            FINISHED_CHALLENGE, encoding="utf-8")
        shutil.rmtree(self.root / "program" / "reports")  # the replay installs its own challenge report
        interview.replay(self.root, recorded, artifacts)
        record = read_json(self.root / "program" / "approvals" / "stage-4.json")
        self.assertEqual(list(record["artifacts"]), ["program/ARCHITECTURE.md", "program/adr/0001-a.md"])


class AdrAddedAfterApprovalTests(InterviewBase):
    """approve, next_stage, amend and replay agree on one file set: an ADR added later is part of it."""

    def setUp(self):
        super().setUp()
        (self.root / "kit" / "interview" / "coverage.yaml").write_text(
            "stages:\n  4:\n    name: architect\n    budget: 0\n    artifacts:\n      - ARCHITECTURE.md\n    topics: {}\n",
            encoding="utf-8")
        self.challenge(4)
        self.write("program/ARCHITECTURE.md", "a\n")
        self.write("program/adr/0001-a.md", "first\n")
        ledger = sha256_file(self.root / "program" / "ledger.yaml")
        approvals = self.root / "program" / "approvals"
        approvals.mkdir()
        for stage in (1, 2, 3, 5, 6):  # stages the one-stage coverage map does not list: empty, current records
            (approvals / f"stage-{stage}.json").write_text(
                json.dumps({"stage": stage, "at": "x", "artifacts": {}, "ledger_sha256": ledger}), encoding="utf-8")
        interview.approve(self.root, 4, interview.stage_artifacts(
            self.root, 4, interview.load_coverage(self.root)["stages"][4]))

    def test_an_adr_added_after_the_approval_is_stale_until_amended(self):
        self.assertEqual(interview.next_stage(self.root), 7)
        self.write("program/adr/0002-b.md", "second\n")
        self.assertEqual(interview.next_stage(self.root), 4)  # a new file in the approved set
        self.assertEqual(interview.amend(self.root, 4), ["program/adr/0002-b.md"])
        record = read_json(self.root / "program" / "approvals" / "stage-4.json")
        self.assertEqual(list(record["artifacts"]),
                         ["program/ARCHITECTURE.md", "program/adr/0001-a.md", "program/adr/0002-b.md"])
        self.assertEqual(interview.next_stage(self.root), 7)
        self.assertEqual(interview.amend(self.root, 4), [])

    def test_replay_of_the_same_files_records_the_set_amend_would(self):
        self.write("program/adr/0002-b.md", "second\n")
        interview.amend(self.root, 4)
        amended = read_json(self.root / "program" / "approvals" / "stage-4.json")["artifacts"]
        recorded = self.root / "recorded.yaml"
        write_yaml(recorded, {"sources": SOURCES, "entries": []})
        artifacts = self.root / "artifacts"
        for relative in ("ARCHITECTURE.md", "adr/0001-a.md", "adr/0002-b.md"):
            (artifacts / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.root / "program" / relative, artifacts / relative)
        (artifacts / "reports").mkdir()
        shutil.copyfile(self.root / "program" / "reports" / "stage-4-challenge.md",
                        artifacts / "reports" / "stage-4-challenge.md")
        shutil.rmtree(self.root / "program" / "reports")
        interview.replay(self.root, recorded, artifacts)
        replayed = read_json(self.root / "program" / "approvals" / "stage-4.json")["artifacts"]
        self.assertEqual(replayed, amended)
        self.assertEqual(interview.amend(self.root, 4), [])


class ChallengeRoundTests(InterviewBase):
    """The challenge round is not satisfied by the untouched skeleton."""

    SKELETON = "stage-2-challenge.md: still the untouched skeleton; each lens needs a finding row or a No findings. line"

    def setUp(self):
        super().setUp()
        self.put_ledger(CLOSING + [entry("F-3", topics=["T2.1"])])
        self.path = interview.challenge_init(self.root, 2)

    def fill(self, **sections):
        """Rewrite the skeleton: each lens given gets the text, the others `No findings.`."""
        text = self.path.read_text(encoding="utf-8")
        for lens in interview.LENSES:
            marker = f"## {lens}\n\n| # | Finding | Severity | Resolution |\n|---|---|---|---|\n"
            self.assertIn(marker, text)
            text = text.replace(marker, marker + sections.get(lens, "\nNo findings.\n"))
        self.path.write_text(text, encoding="utf-8")

    def test_the_untouched_skeleton_is_refused(self):
        self.assertEqual(interview.stopping_rule(self.root, 2), [self.SKELETON])
        with self.assertRaises(KitError) as caught:
            interview.approve(self.root, 2, [self.write("program/product.md")])
        self.assertEqual(str(caught.exception), self.SKELETON)
        self.assertFalse((self.root / "program" / "approvals").exists())

    def test_a_skeleton_with_only_trailing_blank_lines_added_is_still_the_skeleton(self):
        self.path.write_text(self.path.read_text(encoding="utf-8") + "\n\n", encoding="utf-8")
        self.assertEqual(interview.stopping_rule(self.root, 2), [self.SKELETON])

    def test_every_empty_section_is_named(self):
        self.fill(architect="| 1 | gap | minor | answered in F-3 |\n")
        self.path.write_text(self.path.read_text(encoding="utf-8").replace(
            "## program\n\n| # | Finding | Severity | Resolution |\n|---|---|---|---|\n\nNo findings.\n",
            "## program\n\n| # | Finding | Severity | Resolution |\n|---|---|---|---|\n"), encoding="utf-8")
        self.assertEqual(interview.stopping_rule(self.root, 2), [
            "stage-2-challenge.md: section program is empty; add a finding row or a No findings. line"])

    def test_a_blank_row_does_not_count_and_a_missing_section_is_empty(self):
        self.fill(delivery="| | | | |\n")
        self.assertEqual(interview.stopping_rule(self.root, 2), [
            "stage-2-challenge.md: section delivery is empty; add a finding row or a No findings. line"])
        text = self.path.read_text(encoding="utf-8")
        self.path.write_text(text.replace("## engineering", "## engineers"), encoding="utf-8")
        reasons = interview.stopping_rule(self.root, 2)
        self.assertIn("stage-2-challenge.md: section engineering is empty; add a finding row or a No findings. line",
                      reasons)

    def test_a_row_or_a_no_findings_line_satisfies_each_section(self):
        self.fill(architect="| 1 | gap | minor | answered in F-3 |\n", delivery="\nno findings.\n")
        self.assertEqual(interview.stopping_rule(self.root, 2), [])
        self.write("program/product.md")
        self.assertEqual(interview.approve(self.root, 2, [self.root / "program" / "product.md"]).name, "stage-2.json")

    def test_a_row_without_a_resolution_is_still_unresolved_not_empty(self):
        self.fill(architect="| 1 | gap | minor | |\n")
        self.assertEqual(interview.stopping_rule(self.root, 2), ["stage-2-challenge.md: 1 finding unresolved"])

    def test_the_cli_refuses_the_skeleton_with_one_line(self):
        self.write("program/product.md")
        self.run_cli_approved_one()
        code, out, err = self.run_cli("interview", "approve", "2")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, f"error: {self.SKELETON}\n")

    def run_cli_approved_one(self):
        self.write("program/discovery.md")
        self.assertEqual(self.run_cli("interview", "approve", "1")[0], 0)


def _register_with_a_cycle():
    return reg(task("P1-M01-S01-T01", depends_on=["P1-M01-S01-T02"]),
               task("P1-M01-S01-T02", depends_on=["P1-M01-S01-T01"]),
               task("P1-M01-S01-T03"), task("P1-M01-S01-T03"))


class FreezeAndRegisterTests(InterviewBase):
    """Stage 5 re-runs the stopping rules of stages 1 to 4; stage 6 validates the register."""

    def setUp(self):
        super().setUp()
        self.entries = CLOSING + [entry("F-3", topics=["T2.1"])]
        self.put_ledger(self.entries)
        for stage in (5, 6):
            self.challenge(stage)
        self.put_register()
        patcher = mock.patch("trace.validate", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_clean_ledger_passes_the_freeze_and_stage_6(self):
        self.assertEqual(interview.stopping_rule(self.root, 5), [])
        self.assertEqual(interview.stopping_rule(self.root, 6), [])

    def test_a_reopened_topic_in_an_earlier_stage_refuses_the_freeze_and_names_the_stage(self):
        self.put_ledger([item for item in self.entries if item["id"] != "F-1"])
        self.assertEqual(interview.stopping_rule(self.root, 5),
                         ["T1.1: required topic open at stage 1 (missing: measure)"])
        self.assertEqual(interview.stopping_rule(self.root, 6),
                         ["T1.1: required topic open at stage 1 (missing: measure)"])
        with self.assertRaises(KitError) as caught:
            interview.approve(self.root, 5, [self.write("program/freeze/review.md")])
        self.assertIn("T1.1: required topic open at stage 1", str(caught.exception))
        self.assertFalse((self.root / "program" / "approvals").exists())

    def test_a_new_blocking_question_in_an_earlier_stage_refuses_the_freeze_and_names_the_stage(self):
        self.put_ledger(self.entries + [entry("Q-9", topics=["T2.1"], blocking=True)])
        self.assertEqual(interview.stopping_rule(self.root, 5), ["Q-9: blocking question unanswered at stage 2"])
        self.put_ledger(self.entries + [entry("Q-9", topics=["T2.1"], blocking=True), entry("F-4", answers=["Q-9"])])
        self.assertEqual(interview.stopping_rule(self.root, 5), [])

    def test_stages_below_the_freeze_do_not_look_back_or_forward(self):
        self.put_ledger([entry("F-3", topics=["T2.1"])])  # stage 1 is open, stage 2 is closed
        self.challenge(2)
        self.assertEqual(interview.stopping_rule(self.root, 2), [])  # a stage 2 approval ignores stage 1
        self.put_ledger(CLOSING)  # stage 2 is open
        self.assertEqual(interview.stopping_rule(self.root, 1), [])

    def test_a_register_the_validator_rejects_refuses_stage_6_with_its_problems(self):
        from register import validate as validate_register

        write_yaml(self.root / "program" / "register.yaml", _register_with_a_cycle())
        expected = validate_register(_register_with_a_cycle(), None,
                                     read_yaml(interview.KIT_DIR / "interview" / "roles.yaml"))
        self.assertTrue(any("duplicate id" in line for line in expected), expected)
        self.assertTrue(any(line.startswith("cycle:") for line in expected), expected)
        self.assertEqual(interview.stopping_rule(self.root, 6), expected)
        with self.assertRaises(KitError) as caught:
            interview.approve(self.root, 6, [self.root / "program" / "register.yaml"])
        self.assertEqual(str(caught.exception), "; ".join(expected))

    def test_a_register_with_a_malformed_collection_refuses_stage_6(self):
        write_yaml(self.root / "program" / "register.yaml", {"phases": [{"id": "P1", "title": "p", "milestones": 5}]})
        self.assertIn("P1.milestones: must be a list", interview.stopping_rule(self.root, 6))

    def test_a_missing_register_refuses_stage_6_and_the_environments_are_read(self):
        (self.root / "program" / "register.yaml").unlink()
        self.assertEqual(interview.stopping_rule(self.root, 6), ["program/register.yaml is missing"])
        self.put_register(task("P1-M01-S01-T01", environments=["staging"]))
        write_yaml(self.root / "program" / "environments.yaml",
                   {"environments": {"staging": {"exists": False, "owner": "ops", "notes": ""}}})
        self.assertEqual(len(interview.stopping_rule(self.root, 6)), 1)
        self.assertIn("environment staging does not exist yet", interview.stopping_rule(self.root, 6)[0])

    def test_stage_6_through_the_cli_prints_one_error_line_per_problem_set(self):
        write_yaml(self.root / "program" / "register.yaml", _register_with_a_cycle())
        code, out, err = self.run_cli("interview", "approve", "6", "--artifact", "program/register.yaml")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err.count("\n"), 1)
        self.assertTrue(err.startswith("error: "), err)


class EarlierStagesTests(InterviewBase):
    """`pk interview approve N` needs every stage below N approved and current; `next` says why a stage is back."""

    def approve_cli(self, stage):
        return self.run_cli("interview", "approve", str(stage))

    def setUp(self):
        super().setUp()
        self.put_ledger(CLOSING + [entry("F-3", topics=["T2.1"])])
        self.write("program/discovery.md", "d1\n")
        self.write("program/product.md", "p1\n")
        self.challenge(2)

    def test_approving_a_later_stage_names_the_lowest_stage_not_approved(self):
        code, out, err = self.approve_cli(2)
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "error: stage 1 is not approved yet; approve it before stage 2\n")
        code, out, err = self.approve_cli(3)
        self.assertEqual(err, "error: stage 1 is not approved yet; approve it before stage 3\n")
        self.assertFalse((self.root / "program" / "approvals").exists())

    def test_the_lowest_stage_that_is_not_current_is_named_even_when_a_higher_one_is_approved(self):
        self.assertEqual(self.approve_cli(1)[0], 0)
        self.assertEqual(self.approve_cli(2)[0], 0)
        self.write("program/discovery.md", "d2\n")
        code, out, err = self.approve_cli(3)
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "error: stage 1 was approved and its files changed (program/discovery.md); "
                              "run pk interview amend 1 or raise a change request before approving stage 3\n")
        self.assertEqual(self.run_cli("interview", "amend", "1")[0], 0)
        self.assertEqual(self.approve_cli(2)[0], 0)  # a re-approval of a stage whose lower stages are current

    def test_a_current_chain_lets_the_next_stage_through_and_stage_1_has_no_predecessor(self):
        self.assertEqual(self.approve_cli(1)[0], 0)
        self.assertEqual(self.approve_cli(1)[0], 0)
        code, out, err = self.approve_cli(2)
        self.assertEqual((code, err), (0, ""))

    def test_next_says_when_a_stage_is_approved_but_modified(self):
        self.assertEqual(self.approve_cli(1)[0], 0)
        self.write("program/discovery.md", "d2\n")
        code, out, err = self.run_cli("interview", "next")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(lines[0], "next stage: 1 (discover)")
        self.assertIn("stage 1 was approved and its files changed: program/discovery.md; "
                      "run pk interview amend 1 for a correction, or raise a change request", lines)
        self.assertEqual(self.run_cli("interview", "amend", "1")[1],  # KF-5: the paths, then a summary line
                         "program/discovery.md\n"
                         "amended stage 1: 1 changed file(s) re-hashed into program/approvals/stage-1.json\n")
        self.assertEqual(self.run_cli("interview", "amend", "1"), (0, "no changes\n", ""))
        code, out, err = self.run_cli("interview", "next")
        self.assertEqual(out.splitlines()[0], "next stage: 2 (define the product)")
        self.assertNotIn("was approved", out)

    def test_next_for_a_stage_never_approved_says_nothing_about_changes(self):
        out = self.run_cli("interview", "next")[1]
        self.assertEqual(out.splitlines()[0], "next stage: 1 (discover)")
        self.assertNotIn("approved", out)

    def test_next_names_a_changed_ledger_after_the_freeze(self):
        (self.root / "program" / "discovery.md").unlink()  # the stages below record no files here
        (self.root / "program" / "product.md").unlink()
        ledger = sha256_file(self.root / "program" / "ledger.yaml")
        approvals = self.root / "program" / "approvals"
        approvals.mkdir()
        for stage in (1, 2, 3, 4, 5, 6):
            (approvals / f"stage-{stage}.json").write_text(
                json.dumps({"stage": stage, "at": "x", "artifacts": {}, "ledger_sha256": ledger}), encoding="utf-8")
        self.assertEqual(interview.next_stage(self.root), 7)
        self.put_ledger(CLOSING + [entry("F-3", topics=["T2.1"]), entry("F-4")])
        out = self.run_cli("interview", "next")[1]
        self.assertIn("stage 6 was approved and its files changed: program/ledger.yaml", out)


class RemovedAdrTests(InterviewBase):
    """An ADR removed or renamed after the approval is re-hashed out of the set by amend, like an added one is in."""

    def setUp(self):
        super().setUp()
        (self.root / "kit" / "interview" / "coverage.yaml").write_text(
            "stages:\n  4:\n    name: architect\n    budget: 0\n    artifacts:\n      - ARCHITECTURE.md\n    topics: {}\n",
            encoding="utf-8")
        self.challenge(4)
        self.write("program/ARCHITECTURE.md", "a\n")
        self.write("program/adr/0001-first.md", "first\n")
        self.write("program/adr/0002-second.md", "second\n")
        ledger = sha256_file(self.root / "program" / "ledger.yaml")
        approvals = self.root / "program" / "approvals"
        approvals.mkdir()
        for stage in (1, 2, 3, 5, 6):
            (approvals / f"stage-{stage}.json").write_text(
                json.dumps({"stage": stage, "at": "x", "artifacts": {}, "ledger_sha256": ledger}), encoding="utf-8")
        interview.approve(self.root, 4, interview.stage_artifacts(
            self.root, 4, interview.load_coverage(self.root)["stages"][4]))

    def recorded(self):
        return list(read_json(self.root / "program" / "approvals" / "stage-4.json")["artifacts"])

    def test_amend_drops_a_removed_adr(self):
        (self.root / "program" / "adr" / "0002-second.md").unlink()
        self.assertEqual(interview.next_stage(self.root), 4)
        self.assertEqual(interview.amend(self.root, 4), ["program/adr/0002-second.md"])
        self.assertEqual(self.recorded(), ["program/ARCHITECTURE.md", "program/adr/0001-first.md"])
        self.assertEqual(interview.next_stage(self.root), 7)
        self.assertEqual(interview.amend(self.root, 4), [])

    def test_amend_follows_a_renamed_adr_and_next_names_both_paths(self):
        adr = self.root / "program" / "adr"
        (adr / "0002-second.md").rename(adr / "0002-renamed.md")
        out = self.run_cli("interview", "next")[1]
        self.assertIn("program/adr/0002-renamed.md", out)
        self.assertIn("program/adr/0002-second.md", out)
        self.assertEqual(self.run_cli("interview", "amend", "4"), (0, "program/adr/0002-renamed.md\n"
                                                                     "program/adr/0002-second.md\n"
                                                                     "amended stage 4: 2 changed file(s) re-hashed "
                                                                     "into program/approvals/stage-4.json\n", ""))
        self.assertEqual(self.recorded(), ["program/ARCHITECTURE.md", "program/adr/0001-first.md",
                                           "program/adr/0002-renamed.md"])
        self.assertEqual(interview.next_stage(self.root), 7)

    def test_amend_and_replay_and_approve_agree_after_a_removal(self):
        (self.root / "program" / "adr" / "0001-first.md").unlink()
        interview.amend(self.root, 4)
        amended = read_json(self.root / "program" / "approvals" / "stage-4.json")["artifacts"]
        interview.approve(self.root, 4, interview.stage_artifacts(
            self.root, 4, interview.load_coverage(self.root)["stages"][4]))
        self.assertEqual(read_json(self.root / "program" / "approvals" / "stage-4.json")["artifacts"], amended)
        self.assertEqual(interview.amend(self.root, 4), [])

    def test_a_removed_listed_artifact_is_still_an_error_not_silently_dropped(self):
        (self.root / "program" / "ARCHITECTURE.md").unlink()
        with self.assertRaises(KitError) as caught:
            interview.amend(self.root, 4)
        self.assertIn("cannot read", str(caught.exception))
        self.assertIn("ARCHITECTURE.md", self.recorded()[0])

    def test_an_added_adr_still_works_in_the_other_direction(self):
        self.write("program/adr/0003-third.md", "third\n")
        self.assertEqual(interview.next_stage(self.root), 4)
        self.assertEqual(interview.amend(self.root, 4), ["program/adr/0003-third.md"])
        self.assertEqual(interview.next_stage(self.root), 7)


class MalformedLedgerValueTests(InterviewBase):
    """A ledger entry whose source or kind is a list or mapping is a problem line, not an unhashable-type traceback."""

    def test_unhashable_source_and_kind_are_problems(self):
        for field, value in (("source", []), ("source", {}), ("kind", []), ("kind", {}), ("source", ["S1"])):
            with self.subTest(field=field, value=value):
                self.put_ledger(CLOSING + [entry("F-3", **{field: value})])
                problems = interview.ledger_validate(self.root)
                self.assertEqual(len(problems), 1, problems)
                self.assertTrue(problems[0].startswith("F-3: "), problems)
                code, out, err = self.run_cli("interview", "validate")
                self.assertEqual((code, out), (1, ""))
                self.assertEqual(err.count("\n"), 1, err)
                code, out, err = self.run_cli("interview", "approve", "1", "--artifact", "program/ledger.yaml")
                self.assertEqual((code, out), (1, ""))
                self.assertTrue(err.startswith("error: "), err)
                self.assertNotIn("Traceback", err)

    def test_validate_prints_one_error_line_per_problem(self):
        self.put_ledger(CLOSING + [entry("F-3", source="S9"), entry("F-4", source="S8")])
        expected = interview.ledger_validate(self.root)
        self.assertEqual(len(expected), 2, expected)
        code, out, err = self.run_cli("interview", "validate")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err.splitlines(), ["error: " + problem for problem in expected])

    def test_a_non_ascii_digit_is_not_an_entry_number(self):
        self.put_ledger([entry("F-\u0661")])
        self.assertEqual(interview.ledger_validate(self.root),
                         ["F-\u0661: id must look like F-<n>, A-<n>, D-<n> or Q-<n>"])


class StageSixAnswersTests(InterviewBase):
    """Stage 6 is an interview stage: its answers reach the ledger after approval 5 and must not reopen the freeze.

    Between approval 5 and approval 6 only an entry that touches a topic of stages 1 to 5 makes stage 5 modified.
    """

    FILES = {1: "discovery.md", 2: "product.md", 3: "SPEC.md", 4: "ARCHITECTURE.md", 5: "freeze/signoff.md",
             6: "register.yaml"}
    MORE_SOURCES = dict(SOURCES, S2={"kind": "user", "ref": "message 9", "at": "2026-10-02T10:00:00Z"})

    def setUp(self):
        super().setUp()

        def topic(title):
            return {"title": title, "owner": "project", "contributes": [], "required": True, "condition": None,
                    "closes_when": "Stated.", "needs": []}

        coverage = read_yaml(FIXTURE)
        coverage["stages"].update({
            "3": {"name": "specify", "budget": 3, "artifacts": [self.FILES[3]], "topics": {"T3.1": topic("Scope")}},
            "4": {"name": "architect", "budget": 3, "artifacts": [self.FILES[4]], "topics": {}},
            "5": {"name": "freeze review", "budget": 0, "artifacts": [self.FILES[5]], "topics": {}},
            "6": {"name": "roadmap and register", "budget": 3, "artifacts": [self.FILES[6]],
                  "topics": {"T6.1": topic("Phase shaping"), "T6.2": topic("Capacity")}}})
        write_yaml(self.root / "kit" / "interview" / "coverage.yaml", coverage)
        self.spec = CLOSING + [entry("F-3", topics=["T2.1"]), entry("F-4", topics=["T3.1"]), entry("F-5")]
        self.put_ledger(self.spec)
        for stage in range(1, 6):
            self.write(f"program/{self.FILES[stage]}")
        self.put_register()
        for stage in range(2, 7):
            self.challenge(stage)
        patcher = mock.patch("trace.validate", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)
        for stage in range(1, 6):
            self.assertEqual(self.run_cli("interview", "approve", str(stage))[0], 0, stage)

    def next(self):
        code, out, err = self.run_cli("interview", "next")
        self.assertEqual((code, err), (0, ""))
        return out

    def assert_freeze_reopened(self):
        out = self.next()
        self.assertEqual(out.splitlines()[0], "next stage: 5 (freeze review)")
        self.assertIn("stage 5 was approved and its files changed: program/ledger.yaml; run pk interview amend 5", out)
        code, out, err = self.run_cli("interview", "approve", "6")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "error: stage 5 was approved and its files changed (program/ledger.yaml); "
                              "run pk interview amend 5 or raise a change request before approving stage 6\n")
        self.assertFalse((self.root / "program" / "approvals" / "stage-6.json").exists())

    def test_a_stage_6_fact_after_approval_5_leaves_the_freeze_current_and_stage_6_approves(self):
        self.assertEqual(self.next().splitlines()[0], "next stage: 6 (roadmap and register)")
        self.put_ledger(self.spec + [entry("F-6", topics=["T6.1"], source="S2")], self.MORE_SOURCES)
        out = self.next()
        self.assertEqual(out.splitlines()[:2], ["next stage: 6 (roadmap and register)", "topics closed: 1 of 2"])
        self.assertNotIn("was approved", out)
        code, out, err = self.run_cli("interview", "approve", "6")  # refused by stage 6's own stopping rule only
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "error: T6.2: required topic open (missing: any fact, confirmed assumption or "
                              "decision)\n")
        self.put_ledger(self.spec + [entry("F-6", topics=["T6.1"], source="S2"),
                                     entry("D-1", topics=["T6.2", "T6.1"], source="S2")], self.MORE_SOURCES)
        code, out, err = self.run_cli("interview", "approve", "6")
        self.assertEqual((code, out, err), (0, "approved stage 6: program/approvals/stage-6.json\n", ""))
        self.assertEqual(self.next().splitlines()[0], "next stage: 7 (stage 7)")

    def test_amend_agrees_that_a_stage_6_answer_changed_nothing_at_stage_5(self):
        record = self.root / "program" / "approvals" / "stage-5.json"
        before = record.read_bytes()
        self.put_ledger(self.spec + [entry("F-6", topics=["T6.1"])])
        self.assertEqual(self.run_cli("interview", "amend", "5"), (0, "no changes\n", ""))
        self.assertEqual(record.read_bytes(), before)

    def test_an_entry_on_a_stage_3_topic_after_approval_5_reopens_the_freeze_until_amended(self):
        self.put_ledger(self.spec + [entry("F-6", topics=["T3.1"]), entry("F-7", topics=["T6.1"])])
        self.assert_freeze_reopened()
        self.assertEqual(self.run_cli("interview", "amend", "5"), (0, amended(5, "program/ledger.yaml"), ""))
        self.assertEqual(self.next().splitlines()[0], "next stage: 6 (roadmap and register)")
        self.put_ledger(self.spec + [entry("F-6", topics=["T3.1"]), entry("F-7", topics=["T6.1"]),
                                     entry("F-8", topics=["T6.2"])])  # the amended record still lets stage 6 grow
        self.assertEqual(self.run_cli("interview", "approve", "6")[0], 0)

    def test_an_existing_stage_2_entry_edited_after_approval_5_reopens_the_freeze(self):
        edited = [dict(item, text="another meaning") if item["id"] == "F-3" else item for item in self.spec]
        self.put_ledger(edited + [entry("F-6", topics=["T6.1"]), entry("F-7", topics=["T6.2"])])
        self.assert_freeze_reopened()

    def test_an_earlier_entry_removed_or_its_source_changed_reopens_the_freeze(self):
        self.put_ledger([item for item in self.spec if item["id"] != "F-5"])
        self.assert_freeze_reopened()
        self.put_ledger(self.spec, dict(SOURCES, S1=dict(SOURCES["S1"], ref="another message")))
        self.assert_freeze_reopened()
        self.put_ledger(self.spec)
        self.assertEqual(self.next().splitlines()[0], "next stage: 6 (roadmap and register)")

    def test_an_entry_with_a_stage_6_topic_and_an_earlier_one_counts_as_the_earlier_stage(self):
        for topics in (["T6.1", "T2.1"], ["T1.1", "T6.2"]):
            with self.subTest(topics=topics):
                self.put_ledger(self.spec + [entry("F-6", topics=topics)])
                self.assert_freeze_reopened()

    def test_after_approval_6_a_stage_6_entry_change_makes_stage_6_modified(self):
        answers = [entry("F-6", topics=["T6.1"]), entry("F-7", topics=["T6.2"])]
        self.put_ledger(self.spec + answers)
        self.assertEqual(self.run_cli("interview", "approve", "6")[0], 0)
        self.put_ledger(self.spec + [dict(answers[0], text="another cadence"), answers[1]])
        out = self.next()
        self.assertEqual(out.splitlines()[0], "next stage: 6 (roadmap and register)")
        self.assertIn("stage 6 was approved and its files changed: program/ledger.yaml; run pk interview amend 6", out)
        self.assertEqual(self.run_cli("interview", "amend", "6"), (0, amended(6, "program/ledger.yaml"), ""))
        self.assertEqual(self.next().splitlines()[0], "next stage: 7 (stage 7)")
        self.put_ledger(self.spec + answers + [entry("F-8", topics=["T6.2"])])  # an added one, too
        self.assertIn("stage 6 was approved and its files changed: program/ledger.yaml", self.next())

    def test_a_tag_on_a_stage_6_entry_that_switches_an_earlier_topic_on_reopens_the_freeze(self):
        self.assertEqual(interview.topic_states(self.root, 1)["T1.9"]["state"], "not_applicable")
        self.put_ledger(self.spec + [entry("F-6", topics=["T6.1"], tags=["payments"])])  # a tag counts anywhere
        self.assertEqual(interview.topic_states(self.root, 1)["T1.9"]["state"], "open")
        self.assert_freeze_reopened()
        self.assertEqual(self.run_cli("interview", "amend", "5"), (0, amended(5, "program/ledger.yaml"), ""))
        self.put_ledger(self.spec + [entry("F-6", topics=["T6.1"])])  # and taking the tag away switches it off again
        self.assert_freeze_reopened()

    def test_a_tag_on_a_stage_6_entry_that_switches_nothing_leaves_the_freeze_current(self):
        self.put_ledger(self.spec + [entry("F-6", topics=["T6.1"], tags=["measure"])])
        self.assertEqual(self.next().splitlines()[0], "next stage: 6 (roadmap and register)")

    def test_after_approval_6_an_earlier_entry_edit_names_the_freeze_and_amending_stage_6_cannot_absorb_it(self):
        answers = [entry("F-6", topics=["T6.1"]), entry("F-7", topics=["T6.2"])]
        self.put_ledger(self.spec + answers)
        self.assertEqual(self.run_cli("interview", "approve", "6")[0], 0)
        edited = [dict(item, text="another meaning") if item["id"] == "F-3" else item for item in self.spec]
        self.put_ledger(edited + answers)
        out = self.next()
        self.assertEqual(out.splitlines()[0], "next stage: 5 (freeze review)")
        self.assertIn("stage 5 was approved and its files changed: program/ledger.yaml; run pk interview amend 5", out)
        self.run_cli("interview", "amend", "6")
        self.assertEqual(self.next().splitlines()[0], "next stage: 5 (freeze review)")
        self.assertEqual(self.run_cli("interview", "amend", "5"), (0, amended(5, "program/ledger.yaml"), ""))
        self.run_cli("interview", "amend", "6")
        self.assertEqual(self.next().splitlines()[0], "next stage: 7 (stage 7)")

    def test_an_approval_written_before_the_second_digest_falls_back_to_the_whole_file(self):
        path = self.root / "program" / "approvals" / "stage-5.json"
        record = read_json(path)
        self.assertEqual(sorted(record), ["artifacts", "at", "frozen_entries_sha256", "ledger_sha256", "stage"])
        del record["frozen_entries_sha256"]
        path.write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(self.next().splitlines()[0], "next stage: 6 (roadmap and register)")
        self.put_ledger(self.spec + [entry("F-6", topics=["T6.1"])])
        self.assert_freeze_reopened()  # as the record's writer behaved
        self.assertEqual(self.run_cli("interview", "amend", "5"), (0, amended(5, "program/ledger.yaml"), ""))
        self.assertIn("frozen_entries_sha256", read_json(path))
        self.put_ledger(self.spec + [entry("F-6", topics=["T6.1"]), entry("F-7", topics=["T6.2"])])
        self.assertEqual(self.next().splitlines()[0], "next stage: 6 (roadmap and register)")

    def test_only_the_freeze_approval_carries_the_second_digest(self):
        self.put_ledger(self.spec + [entry("F-6", topics=["T6.1"]), entry("F-7", topics=["T6.2"])])
        self.assertEqual(self.run_cli("interview", "approve", "6")[0], 0)
        for stage in (1, 4, 6):
            record = read_json(self.root / "program" / "approvals" / f"stage-{stage}.json")
            self.assertEqual(sorted(record), ["artifacts", "at", "ledger_sha256", "stage"], stage)
        self.assertEqual(record["ledger_sha256"], sha256_file(self.root / "program" / "ledger.yaml"))


class StaleTaskCopyTests(InterviewBase):
    """Stage 6 reads the matrix's copy of each task's `closes`; a register edited since `pk trace sync` is refused."""

    SYNC = "program/traceability.yaml: tasks section differs from the register; run pk trace sync"

    def setUp(self):
        import trace as trace_module
        from test_trace import GATES, full_matrix

        super().setUp()
        self.trace = trace_module
        self.put_ledger(CLOSING + [entry("F-3", topics=["T2.1"])])
        self.challenge(6)
        write_yaml(self.root / "program" / "traceability.yaml", full_matrix())
        write_yaml(self.root / "program" / "gates.yaml", GATES)
        self.closing = (task("P1-M01-S01-T01", closes=["REQ-001", "J1"]), task("P1-M01-S01-T02", closes=["NFR-001"]))
        self.put_register(*self.closing)

    def test_a_register_never_synced_is_refused_and_a_synced_one_passes(self):
        self.assertIn(self.SYNC, interview.stopping_rule(self.root, 6))
        self.trace.sync(self.root)
        self.assertEqual(interview.stopping_rule(self.root, 6), [])
        self.put_register(*reversed(self.closing))  # the order of the tasks is not a difference
        self.assertEqual(interview.stopping_rule(self.root, 6), [])

    def test_removing_the_only_task_that_closes_a_requirement_is_refused_before_and_after_sync(self):
        self.trace.sync(self.root)
        self.put_register(self.closing[0])
        self.assertEqual(interview.ledger_validate(self.root), [])
        with self.assertRaises(KitError) as caught:
            interview.approve(self.root, 6, [self.root / "program" / "register.yaml"])
        self.assertEqual(str(caught.exception), self.SYNC)
        self.assertFalse((self.root / "program" / "approvals").exists())
        self.trace.sync(self.root)
        with self.assertRaises(KitError) as caught:
            interview.approve(self.root, 6, [self.root / "program" / "register.yaml"])
        self.assertEqual(str(caught.exception), "NFR-001: first-release requirement is not closed by any task")
        self.assertFalse((self.root / "program" / "approvals").exists())

    def test_an_empty_register_with_an_old_tasks_section_is_refused(self):
        self.trace.sync(self.root)
        write_yaml(self.root / "program" / "register.yaml", {"phases": []})
        briefs.generate(self.root)
        self.assertEqual(interview.stopping_rule(self.root, 6), [self.SYNC])

    def test_briefs_missing_or_out_of_date_refuse_stage_6_until_generated(self):
        self.trace.sync(self.root)
        self.assertEqual(interview.stopping_rule(self.root, 6), [])
        write_yaml(self.root / "program" / "register.yaml", reg(
            task("P1-M01-S01-T01", closes=["REQ-001", "J1"]), task("P1-M01-S01-T02", closes=["NFR-001"], text="new")))
        reasons = interview.stopping_rule(self.root, 6)  # the register is edited, the briefs are not regenerated
        self.assertEqual(reasons, ["program/briefs/MANIFEST.json: differs from the register",
                                   "program/briefs/tasks/P1-M01-S01-T02.md: differs from the register"])
        with self.assertRaises(KitError) as caught:
            interview.approve(self.root, 6, [self.root / "program" / "register.yaml"])
        self.assertEqual(str(caught.exception), "; ".join(reasons))
        self.assertFalse((self.root / "program" / "approvals").exists())
        briefs.generate(self.root)
        self.assertEqual(interview.stopping_rule(self.root, 6), [])
        shutil.rmtree(self.root / "program" / "briefs")  # never generated
        self.assertTrue(all("missing; run `pk briefs generate`" in line for line in interview.stopping_rule(self.root, 6)))
        # an invalid register is reported by its own check only, not once more by the briefs
        write_yaml(self.root / "program" / "register.yaml", {"phases": [{"id": "P1", "title": "p", "milestones": 5}]})
        self.assertEqual(interview.stopping_rule(self.root, 6), ["P1.milestones: must be a list"])

    def test_a_changed_closes_list_is_refused_until_synced(self):
        self.trace.sync(self.root)
        self.put_register(task("P1-M01-S01-T01", closes=["REQ-001", "J1", "NFR-001"]),
                          task("P1-M01-S01-T02", closes=[]))
        self.assertEqual(interview.stopping_rule(self.root, 6), [self.SYNC])
        self.trace.sync(self.root)
        self.assertEqual(interview.stopping_rule(self.root, 6), [])

    def test_a_missing_file_or_an_invalid_register_is_reported_once_by_its_own_check(self):
        self.trace.sync(self.root)
        write_yaml(self.root / "program" / "register.yaml", {"phases": [{"id": "P1", "title": "p", "milestones": 5}]})
        reasons = interview.stopping_rule(self.root, 6)  # the validator names it; nothing is synced from it
        self.assertIn("P1.milestones: must be a list", reasons)
        self.assertNotIn(self.SYNC, reasons)
        (self.root / "program" / "register.yaml").unlink()
        self.assertEqual(interview.stopping_rule(self.root, 6), ["program/register.yaml is missing"])
        self.put_register(*self.closing)
        (self.root / "program" / "traceability.yaml").unlink()
        self.assertEqual(interview.stopping_rule(self.root, 6), ["program/traceability.yaml is missing"])

    def test_stage_5_does_not_look_at_the_register(self):
        self.challenge(5)
        self.assertEqual(interview.stopping_rule(self.root, 5), [])


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
        code, out, err = self.run_cli("interview", "approve", "1")  # the skeleton alone is not a challenge round
        self.assertEqual((code, out), (1, ""))
        self.assertIn("untouched skeleton", err)
        challenge = self.root / "program" / "reports" / "stage-1-challenge.md"
        challenge.write_text(challenge.read_text(encoding="utf-8").replace(
            "|---|---|---|---|", "|---|---|---|---|\n\nNo findings."), encoding="utf-8")
        code, out, err = self.run_cli("interview", "approve", "1")  # artifacts default to the coverage map's list
        self.assertEqual((code, out.strip(), err), (0, "approved stage 1: program/approvals/stage-1.json", ""))
        code, out, _ = self.run_cli("interview", "next")
        self.assertEqual(out.splitlines()[0], "next stage: 2 (define the product)")
        self.assertEqual(self.run_cli("interview", "amend", "1")[1], "no changes\n")
        self.write("program/discovery.md", "v2\n")
        self.assertEqual(self.run_cli("interview", "amend", "1")[1], amended(1, "program/discovery.md"))
        doc = self.write("incoming/notes.md")
        code, out, _ = self.run_cli("interview", "ingest", str(doc))
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("S2 program/sources/"))

    def test_cli_stage_7_is_per_task(self):
        with mock.patch("interview.next_stage", return_value=7):
            code, out, err = self.run_cli("interview", "next")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("next stage: 7 ("), lines[0])
        self.assertEqual(lines[1:], ["discovery is complete: run playbooks 07 and 08 for each task"])
        for action in ("report", "challenge", "approve", "amend"):
            code, out, err = self.run_cli("interview", action, "7")
            self.assertEqual((code, out), (1, ""), action)
            self.assertEqual(err, "error: stage 7 is per task: run playbooks 07 and 08 for each task; "
                                  "there is nothing to approve\n", action)

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

    def test_a_file_where_the_reports_or_sources_directory_belongs_is_one_error_line(self):
        self.put_ledger(CLOSING)
        self.write("program/reports", "not a directory\n")
        for action in ("report", "challenge"):
            code, out, err = self.run_cli("interview", action, "1")
            self.assertEqual((code, out), (1, ""), action)
            self.assertEqual(err.count("\n"), 1, err)
            self.assertTrue(err.startswith("error: cannot write "), err)
            self.assertIn("program/reports", err)
        self.assertEqual((self.root / "program" / "reports").read_text(encoding="utf-8"), "not a directory\n")
        self.write("program/sources", "not a directory\n")
        ledger = (self.root / "program" / "ledger.yaml").read_bytes()
        code, out, err = self.run_cli("interview", "ingest", str(self.write("incoming/notes.md")))
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err.count("\n"), 1, err)
        self.assertTrue(err.startswith("error: cannot write "), err)
        self.assertIn("program/sources", err)
        self.assertEqual((self.root / "program" / "ledger.yaml").read_bytes(), ledger)


if __name__ == "__main__":
    unittest.main()
