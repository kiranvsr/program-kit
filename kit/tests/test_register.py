import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import contextlib
import copy
import io
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pk
import register
from common import KitError, read_yaml, write_yaml

KIT = Path(__file__).resolve().parent.parent
ROLES = {"roles": [{"id": name, "name": name.title(), "may_touch": ["*"]} for name in ("backend", "docs")]}
ENVS = {"environments": {"staging": {"exists": False, "owner": "ops", "notes": ""},
                         "dev": {"exists": True, "owner": "dev", "notes": ""}}}


def task(tid, **fields):
    base = {"id": tid, "title": "t " + tid, "text": "do it", "role": "backend", "size": "S",
            "environments": [], "depends_on": [], "gate": None, "closes": []}
    base.update(fields)
    return base


def reg(*tasks):
    """One phase, one milestone, one step holding the given tasks."""
    return {"phases": [{"id": "P1", "title": "p", "gate": None, "milestones": [
        {"id": "P1-M01", "title": "m", "gate": None, "steps": [
            {"id": "P1-M01-S01", "title": "s", "tasks": list(tasks)}]}]}]}


class RegisterTests(unittest.TestCase):
    def messages(self, register_, environments=None, roles=ROLES):
        return register.validate(register_, environments, roles)

    def test_assign_ids_is_stable_and_positional(self):
        bare = {"phases": [
            {"title": "p1", "milestones": [
                {"title": "m1", "steps": [{"title": "s1", "tasks": [{"title": "a"}, {"title": "b"}]},
                                          {"title": "s2", "tasks": [{"title": "c"}]}]},
                {"title": "m2", "steps": [{"title": "s3", "tasks": [{"title": "d"}]}]}]},
            {"title": "p2", "milestones": [{"title": "m3", "steps": [{"title": "s4", "tasks": [{"title": "e"}]}]}]}]}
        before = copy.deepcopy(bare)
        done = register.assign_ids(bare)
        self.assertEqual(bare, before, "assign_ids must not mutate its input")
        self.assertIsNot(done, bare)
        phases = done["phases"]
        self.assertEqual([p["id"] for p in phases], ["P1", "P2"])
        self.assertEqual([m["id"] for m in phases[0]["milestones"]], ["P1-M01", "P1-M02"])
        steps = phases[0]["milestones"][0]["steps"]
        self.assertEqual([s["id"] for s in steps], ["P1-M01-S01", "P1-M01-S02"])
        self.assertEqual([t["id"] for t in steps[0]["tasks"]], ["P1-M01-S01-T01", "P1-M01-S01-T02"])
        self.assertEqual(steps[1]["tasks"][0]["id"], "P1-M01-S02-T01")
        self.assertEqual(phases[0]["milestones"][1]["steps"][0]["tasks"][0]["id"], "P1-M02-S01-T01")
        self.assertEqual(phases[1]["milestones"][0]["steps"][0]["tasks"][0]["id"], "P2-M01-S01-T01")
        self.assertEqual(done["phases"][0]["title"], "p1")
        # stable: running again changes nothing
        self.assertEqual(register.assign_ids(done), done)
        # existing ids are kept even where they disagree with position, and new ids never collide with them
        partial = reg(task("P1-M01-S01-T02"), {"title": "new"})
        ids = [t["id"] for t in register.assign_ids(partial)["phases"][0]["milestones"][0]["steps"][0]["tasks"]]
        self.assertEqual(ids[0], "P1-M01-S01-T02")
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual(ids[1], "P1-M01-S01-T03")
        # a new task with a free position number takes it
        gap = reg({"title": "first"}, task("P1-M01-S01-T02"))
        self.assertEqual(register.assign_ids(gap)["phases"][0]["milestones"][0]["steps"][0]["tasks"][0]["id"],
                         "P1-M01-S01-T01")

    def test_assign_ids_pads_milestones_steps_and_tasks_but_not_phases(self):
        tasks = [{"title": str(i)} for i in range(10)]
        done = register.assign_ids(reg(*tasks))
        self.assertEqual(done["phases"][0]["milestones"][0]["steps"][0]["tasks"][9]["id"], "P1-M01-S01-T10")
        many = {"phases": [{"title": str(i)} for i in range(10)]}
        self.assertEqual(register.assign_ids(many)["phases"][9]["id"], "P10")

    def test_validate_reports_cycle_path(self):
        a, b, c = "P1-M01-S01-T01", "P1-M01-S01-T02", "P1-M01-S01-T03"
        problems = self.messages(reg(task(a, depends_on=[b]), task(b, depends_on=[c]), task(c, depends_on=[a])))
        self.assertEqual([p for p in problems if p.startswith("cycle")], [f"cycle: {a} -> {b} -> {c} -> {a}"])
        self.assertEqual(len(problems), 1, problems)
        selfish = self.messages(reg(task(a, depends_on=[a])))
        self.assertEqual(selfish, [f"cycle: {a} -> {a}"])
        acyclic = self.messages(reg(task(a), task(b, depends_on=[a]), task(c, depends_on=[a, b])))
        self.assertEqual(acyclic, [])

    def test_cycle_detection_does_not_recurse(self):
        ids = [f"P1-M01-S01-T{n}" for n in range(1, 3001)]
        chain = [task(ids[n], depends_on=[ids[n + 1]] if n + 1 < len(ids) else [ids[0]]) for n in range(len(ids))]
        problems = [p for p in self.messages(reg(*chain), None, None) if p.startswith("cycle")]
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith(f"cycle: {ids[0]} -> {ids[1]}") and problems[0].endswith(ids[0]))

    def test_validate_rejects_size_L_task_and_fan_in_over_6(self):
        deps = [f"P1-M01-S01-T{n:02d}" for n in range(1, 8)]
        tasks = [task(t) for t in deps]
        big = "P1-M01-S01-T08"
        tasks.append(task(big, size="L", depends_on=deps[:7]))
        problems = self.messages(reg(*tasks))
        self.assertEqual(len(problems), 2, problems)
        self.assertTrue(any(p.startswith(big) and "split into steps" in p for p in problems), problems)
        self.assertTrue(any(p.startswith(big) and "7" in p and "depends_on" in p for p in problems), problems)
        six = reg(*tasks[:7], task("P1-M01-S01-T09", depends_on=deps[:6]))
        self.assertEqual(self.messages(six), [])
        bad = self.messages(reg(task("P1-M01-S01-T01", size="XL"), task("P1-M01-S01-T02", size="M")))
        self.assertEqual(len(bad), 1)
        self.assertTrue(bad[0].startswith("P1-M01-S01-T01") and "size" in bad[0])

    def test_missing_environment_needs_blocked_by(self):
        tid = "P1-M01-S01-T01"
        problems = self.messages(reg(task(tid, environments=["staging"])), ENVS)
        self.assertEqual(len(problems), 1, problems)
        self.assertTrue(problems[0].startswith(tid) and "staging" in problems[0] and "blocked_by" in problems[0])
        self.assertEqual(self.messages(reg(task(tid, environments=["staging"], blocked_by=["staging"])), ENVS), [])
        wrong = self.messages(reg(task(tid, environments=["staging"], blocked_by=["dev"])), ENVS)
        self.assertEqual(len(wrong), 1)
        self.assertEqual(self.messages(reg(task(tid, environments=["dev"])), ENVS), [])
        unknown = self.messages(reg(task(tid, environments=["moon"])), ENVS)
        self.assertEqual(len(unknown), 1)
        self.assertTrue(unknown[0].startswith(tid) and "moon" in unknown[0])
        # no environments document: the check is skipped
        self.assertEqual(self.messages(reg(task(tid, environments=["staging"])), None), [])

    def test_unknown_role_and_dependency(self):
        tid = "P1-M01-S01-T01"
        problems = self.messages(reg(task(tid, role="wizard", depends_on=["P9-M09-S09-T09"])))
        self.assertEqual(len(problems), 2, problems)
        self.assertTrue(all(p.startswith(tid) for p in problems), problems)
        self.assertTrue(any("wizard" in p for p in problems))
        self.assertTrue(any("P9-M09-S09-T09" in p for p in problems))
        self.assertEqual(register.validate(reg(task(tid, role="wizard")), None, None), [])

    # beyond the five named tests
    def test_id_patterns_accept_only_ascii_digits(self):
        arabic = "\u0661"  # ARABIC-INDIC DIGIT ONE, which str.isdigit and \d (without ASCII) accept
        for text in (f"P{arabic}-M01-S01-T01", f"P1-M0{arabic}-S01-T01", f"P1-M01-S01-T0{arabic}"):
            self.assertIsNone(register.TASK_ID.match(text), text)
        self.assertIsNotNone(register.TASK_ID.match("P1-M01-S01-T01"))
        self.assertIsNone(register.CLOSES_ID.match(f"REQ-00{arabic}"))
        self.assertIsNone(register.CLOSES_ID.match(f"J{arabic}"))
        self.assertIsNotNone(register.CLOSES_ID.match("J1"))
        self.assertEqual(tuple(register.ID_PATTERNS), register.KINDS)
        self.assertIs(register.TASK_ID, register.ID_PATTERNS["task"])
        for kind, shape in register.ID_PATTERNS.items():
            self.assertTrue(shape.flags & re.ASCII, kind)

    def test_template_comment_names_the_optional_task_fields(self):
        text = (KIT / "templates" / "register.yaml").read_text(encoding="utf-8")
        brief = (KIT / "templates" / "task_brief.md").read_text(encoding="utf-8")
        for field in ("tests_unit", "tests_integration", "done"):
            self.assertRegex(text, rf"(?m)^\s*#\s+{field}: ", field)  # a comment line, never a live key
            self.assertIn(f"${field}", brief, field)  # and the brief template really uses the field
            self.assertNotRegex(text, rf"(?m)^\s*{field}:", field)

    def test_environment_hint_teaches_block_style_the_reader_accepts(self):
        tid = "P1-M01-S01-T01"
        message = self.messages(reg(task(tid, environments=["staging"])), ENVS)[0]
        self.assertNotRegex(message, r"[\[{]")  # no flow collection anywhere in the advice
        self.assertIn("blocked_by:", message)
        self.assertIn("- staging", message)
        with tempfile.TemporaryDirectory() as tmp:  # and the advice, followed literally, validates
            path = Path(tmp) / "register.yaml"
            path.write_text(
                "phases:\n  - id: P1\n    title: p\n    milestones:\n      - id: P1-M01\n        title: m\n"
                "        steps:\n          - id: P1-M01-S01\n            title: s\n            tasks:\n"
                f"              - id: {tid}\n                title: t\n                text: x\n"
                "                role: backend\n                size: S\n                environments:\n"
                "                  - staging\n                blocked_by:\n                  - staging\n",
                encoding="utf-8")
            self.assertEqual(self.messages(read_yaml(path), ENVS), [])
            # the same list written at the key's indent (the default of common YAML writers) reads the same
            path.write_text(path.read_text(encoding="utf-8").replace("                  - staging",
                                                                       "                - staging"),
                            encoding="utf-8")
            self.assertEqual(self.messages(read_yaml(path), ENVS), [])

    def test_validate_checks_ids_gates_and_closes(self):
        a = "P1-M01-S01-T01"
        problems = self.messages(reg(task(a, closes=["REQ-001", "NFR-012", "J3", "REQ-1", "X-001"], gate="BETA"),
                                     task(a), task("T2")))
        self.assertTrue(any(p.startswith(a) and "REQ-1" in p for p in problems), problems)
        self.assertTrue(any(p.startswith(a) and "X-001" in p for p in problems), problems)
        self.assertFalse(any("NFR-012" in p or "J3" in p or "REQ-001" in p for p in problems), problems)
        self.assertTrue(any(p.startswith(a) and "gate" in p for p in problems), problems)
        self.assertTrue(any(p.startswith(a) and "duplicate" in p for p in problems), problems)
        self.assertTrue(any(p.startswith("T2") and "id" in p for p in problems), problems)
        ok = self.messages(reg(task(a, gate="G-BETA", closes=["J10"])))
        self.assertEqual(ok, [])
        gates = reg(task(a))
        gates["phases"][0]["gate"] = "nope"
        gates["phases"][0]["milestones"][0]["gate"] = "G-DEV"
        self.assertEqual(len(self.messages(gates)), 1)

    def test_validate_never_raises_on_content_and_raises_on_shape(self):
        garbage = {"phases": [None, {"id": "P1", "milestones": "x"},
                              {"id": "P2", "milestones": [{"id": "P2-M01", "steps": [{"id": "P2-M01-S01",
                               "tasks": [3, {"id": "P2-M01-S01-T01", "depends_on": "x", "closes": 5,
                                             "environments": [1], "size": None, "role": 4}]}]}]}]}
        problems = self.messages(garbage, ENVS)
        self.assertTrue(problems and all(isinstance(p, str) for p in problems))
        for bad in (None, [], "x", {}, {"phases": "x"}, {"phases": {}}):
            with self.assertRaises(KitError):
                register.validate(bad, None, None)
            with self.assertRaises(KitError):
                register.assign_ids(bad)
        with self.assertRaises(KitError):
            register.validate(reg(), {"environments": []}, None)
        with self.assertRaises(KitError):
            register.validate(reg(), None, {"roles": "x"})

    def test_shipped_roles_and_template_are_valid(self):
        roles = read_yaml(KIT / "interview" / "roles.yaml")
        self.assertEqual([r["id"] for r in roles["roles"]],
                         ["backend", "frontend", "platform", "data", "security", "docs", "qa"])
        touch = {r["id"]: r["may_touch"] for r in roles["roles"]}
        self.assertEqual(touch["docs"], ["docs"])
        self.assertEqual(touch["qa"], ["tests"])
        self.assertTrue(all(v == ["*"] for k, v in touch.items() if k not in ("docs", "qa")))
        self.assertTrue(all(r["name"] for r in roles["roles"]))
        template = read_yaml(KIT / "templates" / "register.yaml")
        self.assertEqual(register.validate(template, read_yaml(KIT / "templates" / "environments.yaml"), roles), [])
        self.assertEqual(register.assign_ids(template), template)
        # No placeholder task: hand-off and status would count it as a real task that has not started.
        self.assertEqual(template, {"phases": []})
        self.assertEqual(register.task_count(template), 0)


class RegisterCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        (self.root / ".git").mkdir()
        (self.root / "program").mkdir()
        self.path = self.root / "program" / "register.yaml"

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("register.find_root", return_value=self.root):
            code = pk.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_load_reads_the_register_and_rejects_wrong_shape(self):
        write_yaml(self.path, reg(task("P1-M01-S01-T01")))
        self.assertEqual(register.load(self.root), reg(task("P1-M01-S01-T01")))
        self.path.write_text("phases: x\n")
        with self.assertRaises(KitError):
            register.load(self.root)
        self.path.unlink()
        with self.assertRaises(KitError):
            register.load(self.root)

    def test_cli_validate_reports_problems_on_stderr(self):
        write_yaml(self.path, reg(task("P1-M01-S01-T01"), task("P1-M01-S01-T02", role="wizard", size="L")))
        code, out, err = self.run_cli("register", "validate")
        self.assertEqual((code, out), (1, ""))
        lines = err.splitlines()
        self.assertEqual(len(lines), 3, err)
        self.assertEqual(lines[0], "error: register invalid: 2 problem(s)")
        self.assertTrue(all(line.startswith("error: P1-M01-S01-T02") for line in lines[1:]), err)

    def test_cli_validate_ok_counts_tasks_and_reads_environments(self):
        write_yaml(self.path, reg(task("P1-M01-S01-T01"), task("P1-M01-S01-T02", environments=["staging"])))
        code, out, err = self.run_cli("register", "validate")  # no environments.yaml: env check skipped
        self.assertEqual((code, out, err), (0, "register valid: 2 tasks\n", ""))
        write_yaml(self.root / "program" / "environments.yaml", ENVS)
        code, out, err = self.run_cli("register", "validate")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("staging", err)

    def test_cli_assign_ids_rewrites_the_file(self):
        write_yaml(self.path, {"phases": [{"title": "p", "milestones": [{"title": "m", "steps": [
            {"title": "s", "tasks": [{"title": "a"}, {"title": "b"}]}]}]}]})
        code, out, err = self.run_cli("register", "assign-ids")
        self.assertEqual((code, out, err), (0, "assigned ids: 2 tasks\n", ""))
        data = read_yaml(self.path)
        self.assertEqual(data["phases"][0]["id"], "P1")
        self.assertEqual([t["id"] for t in data["phases"][0]["milestones"][0]["steps"][0]["tasks"]],
                         ["P1-M01-S01-T01", "P1-M01-S01-T02"])

    def test_cli_errors_are_one_line(self):
        code, out, err = self.run_cli("register", "validate")  # no register file
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("error: ") and err.count("\n") == 1, err)
        code, out, err = self.run_cli("register")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("error: ") and err.count("\n") == 1, err)


if __name__ == "__main__":
    unittest.main()
