#!/usr/bin/env python3
"""`pk spec precheck <task folder or ID>`: the spec author's check before spec QA, of what spec QA keeps finding.

One output line per finding, `PC-n <file>[:line] <message>`, then exit 1; `spec precheck: clean` and exit 0 when
there is none. `note:` lines say what was skipped and why; they are not findings.

  PC-1  plant rows. Every row of the PLAN's plant table (a `## ` section whose heading contains "plant", holding a
        table whose first header cell starts with `#`) names a killing test and, where the table has a Needle
        column, a needle. When the folder has a plants file (`plants.py` or `tools/plants.py`: module-level lists
        of tuples `(id, file, find, replace, member, test file, test title[, needle])`; or `plants.json`: a list of
        `{id, file, find, replace, killer, needle}`), every plant has a PLAN row and every PLAN row a plant (or its
        id quoted in another script under `tools/`). Against the code (below): every find occurs exactly once in
        the file it names, every `case:` or `check:` needle occurs in the plant's test file, and every killing test
        is in a test file.
  PC-2  no needle is a proper prefix of another `case:` or `check:` label in the same test: a test that fails on
        the longer label would otherwise be read as killing the plant whose needle is the shorter one.
  PC-3  every row of the SPEC's Failure Modes table has a plant: by its Row column when the table has one, else by
        a test it names being a plant's killing test. Without any plant table, each row must still name a test,
        and the plant checks are skipped with a note, unless `--require-plants` is given: a program whose specs
        always carry a plant table passes it, and then a missing table, or no code to check it against, is a
        finding too.
  PC-4  `standin.patch`, when the folder has one, applies to main, and applies three-way onto the tip of every
        task branch not merged into main (local `task/*` and `origin/task/*`), reported by file. The task's own
        branch is skipped.
  PC-5  `gate.parse_plan` parses the PLAN's `## Exact Commands` and finds at least one command.
  PC-6  only with `--build`: each plant row's replacement, applied alone to a throwaway mirror of the code, still
        passes main's `build` and `compile_check` commands (`program/checks.yaml` on main). A row that does not
        build is judged `build failed` by the gate, never `killed`. The unmutated code must build first.

The code is the stand-in (`standin.patch` applied to main) or, without one, the tip of the task branch
(`task/<folder name>`, else `origin/task/<folder name>`); with neither, the checks that need the code are skipped
with a note. The stand-in is applied to a private index and object directory, never to a checkout, so the
repository is not written to. `--build` adds and removes a detached worktree per run (`evidence.PlantJudge`, the
evidence runner's mirror) and runs main's build commands in it, one row at a time; a plants.py is imported in a
separate Python process. Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from common import KitError, find_root, read_text

TEST_FILE = re.compile(r"(\.test\.|\.spec\.|selftest|(^|/)tests?/|(^|/)test_[^/]*$|_test\.[^/]+$)")
CALL = re.compile(r"^[ \t]*(?:it|test)(?:\.(?:each|soft|only|skip|concurrent|sequential)\b[^\n]*?)?\(", re.M)
LABEL = re.compile(r"\"((?:case|check):[^\"\n]*)\"|'((?:case|check):[^'\n]*)'|`((?:case|check):[^`]*)`")
ID = re.compile(r"^[A-Z]{1,3}\d+$")
_MARK = "pk-precheck-plants: "
_PLANTS_BOOT = (
    "import importlib.util, json, re, sys\n"
    "sys.path.insert(0, sys.argv[1].rsplit('/', 1)[0])\n"
    "spec = importlib.util.spec_from_file_location('task_plants', sys.argv[1])\n"
    "module = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(module)\n"
    "rows, seen = [], set()\n"
    "for value in list(vars(module).values()):\n"
    "    if isinstance(value, list):\n"
    "        for r in value:\n"
    "            if (isinstance(r, tuple) and len(r) >= 7 and isinstance(r[0], str)\n"
    "                    and re.match(sys.argv[3], r[0]) and r[0] not in seen):\n"
    "                seen.add(r[0])\n"
    "                rows.append(list(r[:8]))\n"
    "sys.stdout.write('\\n' + sys.argv[2] + json.dumps(rows, default=str) + '\\n')\n"
)


class Finding:
    def __init__(self, check, where, message):
        self.check, self.where, self.message = check, where, message

    def __str__(self):
        return f"{self.check} {self.where} {self.message}"


# ---------------------------------------------------------------- git


class Repo:
    """Read-only git access: a private index and object directory take every write."""

    def __init__(self, root):
        self.root = str(root)
        common = self.run(["rev-parse", "--path-format=absolute", "--git-common-dir"]).stdout.strip()
        self.objects = os.path.join(common, "objects")
        self.tmp = tempfile.mkdtemp(prefix="pk-precheck-")
        os.makedirs(os.path.join(self.tmp, "objects"))

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run(self, args, env=None):
        full = dict(os.environ)
        full.update(env or {})
        return subprocess.run(["git", "-C", self.root, *args], capture_output=True, text=True,
                              errors="replace", env=full)

    def ref_ok(self, ref):
        return self.run(["rev-parse", "--verify", "--quiet", ref + "^{commit}"]).returncode == 0

    def index_env(self, name):
        path = os.path.join(self.tmp, name)
        if os.path.exists(path):
            os.remove(path)
        return {"GIT_INDEX_FILE": path, "GIT_OBJECT_DIRECTORY": os.path.join(self.tmp, "objects"),
                "GIT_ALTERNATE_OBJECT_DIRECTORIES": self.objects}

    def apply(self, ref, patch, name, three_way=False):
        """Applies `patch` onto a private index of `ref`; (result, env) so the content can be read back."""
        env = self.index_env(name)
        read = self.run(["read-tree", ref], env=env)
        if read.returncode != 0:
            return read, env
        args = ["apply", "--cached"] + (["--3way"] if three_way else []) + [str(patch)]
        return self.run(args, env=env), env

    def show(self, env, path):
        out = self.run(["cat-file", "blob", ":" + path], env=env)
        return out.stdout if out.returncode == 0 else None

    def ls(self, env):
        return self.run(["ls-files"], env=env).stdout.splitlines()

    def task_tips(self):
        """(short name, sha) of every local `task/*` and `origin/task/*` branch whose tip main does not contain."""
        listing = self.run(["for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads/task",
                            "refs/remotes/origin/task"]).stdout.splitlines()
        base = "main" if self.ref_ok("main") else "HEAD"
        found, seen = [], set()
        for line in listing:
            name, _, sha = line.partition(" ")
            if name and sha not in seen and self.run(["merge-base", "--is-ancestor", sha, base]).returncode != 0:
                seen.add(sha)  # a local branch and its pushed copy at the same commit are one tip
                found.append((name, sha))
        return found


# ---------------------------------------------------------------- markdown tables


def split_row(line):
    """Cells of a markdown table row; `\\|` stays inside its cell."""
    cells, cur, i = [], "", 0
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    while i < len(line):
        ch = line[i]
        if ch == "\\" and line[i + 1:i + 2] == "|":
            cur += "\\|"
            i += 2
            continue
        if ch == "|":
            cells.append(cur.strip())
            cur = ""
        else:
            cur += ch
        i += 1
    if cur.strip():
        cells.append(cur.strip())
    return cells


def sections(text):
    """(heading, [(line number, line)]) for every `## ` section (`###` subsections stay inside their parent)."""
    out, name, lines = [], None, []
    for n, line in enumerate(text.splitlines(), 1):
        if line.startswith("## "):
            if name is not None:
                out.append((name, lines))
            name, lines = line[3:].strip(), []
        elif name is not None:
            lines.append((n, line))
    if name is not None:
        out.append((name, lines))
    return out


def tables(lines):
    """[(header cells lowercased, [(line number, cells)])] for each table in (n, line) pairs."""
    found, cur = [], None
    for n, line in lines:
        if line.lstrip().startswith("|"):
            cells = split_row(line)
            if cur is None:
                cur = ([c.lower() for c in cells], [])
                found.append(cur)
            elif all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            else:
                cur[1].append((n, cells))
        else:
            cur = None
    return found


def column(header, *words):
    for i, h in enumerate(header):
        if all(w in h for w in words):
            return i
    return None


def cell_text(cell):
    """A PLAN cell as plain text: one outer backtick pair dropped, `\\|` and `\\n` unescaped, (nothing) empty."""
    if cell == "(nothing)":
        return ""
    if len(cell) >= 2 and cell[0] == "`" and cell[-1] == "`":
        cell = cell[1:-1]
    return cell.replace("\\|", "|").replace("\\n", "\n")


# ---------------------------------------------------------------- plants


class Plant:
    def __init__(self, pid, file, find, replace, member=None, testfile=None, title=None, needle=None, killer=None):
        self.id, self.file, self.find, self.replace = pid, file, find, replace
        self.member, self.testfile, self.title, self.needle, self.killer = member, testfile, title, needle, killer
        self.path = None  # the stand-in path the file resolved to, once PC-1 found it


def _plants_py(path: Path) -> list:
    proc = subprocess.run([sys.executable, "-B", "-c", _PLANTS_BOOT, str(path), _MARK, ID.pattern],
                          cwd=str(path.parent), capture_output=True, text=True, errors="replace", timeout=120)
    found = [line[len(_MARK):] for line in proc.stdout.split("\n") if line.startswith(_MARK)]
    if proc.returncode != 0 or not found:
        tail = [line for line in proc.stderr.splitlines() if line.strip()][-1:] or [f"exit {proc.returncode}"]
        raise KitError(f"{path}: cannot be loaded: {tail[0]}")
    return json.loads(found[-1])


def load_plants(folder):
    """(plants, source path or None). plants.py (every module-level list of plant tuples) wins over plants.json."""
    for cand in (folder / "tools" / "plants.py", folder / "plants.py"):
        if cand.exists():
            rows = _plants_py(cand)
            return [Plant(r[0], r[1], r[2], r[3], member=r[4], testfile=r[5], title=r[6],
                          needle=r[7] if len(r) > 7 else None) for r in rows], cand
    for cand in (folder / "plants.json", folder / "tools" / "plants.json"):
        if cand.exists():
            return [Plant(r["id"], r.get("file"), r.get("find"), r.get("replace"), killer=r.get("killer"),
                          needle=r.get("needle")) for r in json.loads(read_text(cand))], cand
    return [], None


def plan_rows(plan_text):
    """PLAN plant-table rows: dicts of id, line, file, find, replace, killer, needle and which columns exist."""
    rows = []
    for name, lines in sections(plan_text):
        if "plant" not in name.lower():
            continue
        for header, body in tables(lines):
            if not header or not header[0].startswith("#"):
                continue
            ci = {"file": column(header, "file"), "find": column(header, "find"),
                  "replace": column(header, "replace"), "killer": column(header, "killing"),
                  "needle": column(header, "needle")}
            for n, cells in body:
                if not cells or not ID.match(cells[0]):
                    continue
                row = {"id": cells[0], "line": n, "has_killer_col": ci["killer"] is not None,
                       "has_needle_col": ci["needle"] is not None}
                for key, i in ci.items():
                    row[key] = cell_text(cells[i]) if i is not None and i < len(cells) else None
                killer = (row["killer"] or "").strip()
                if len(killer) >= 2 and killer[0] == killer[-1] and killer[0] in "\"'":
                    row["killer"] = killer[1:-1]  # a quoted test name: the quotes are not part of it
                rows.append(row)
    return rows


def line_of(path, pid):
    """Line of the first quoted occurrence of the id in a plants file, else None."""
    pat = re.compile(r"[\"']" + re.escape(pid) + r"[\"']")
    try:
        for n, line in enumerate(read_text(Path(path)).splitlines(), 1):
            if pat.search(line):
                return n
    except KitError:
        pass
    return None


# ---------------------------------------------------------------- labels


def blocks_of(text):
    """[(start, end)] of every test call: from one it()/test() to the next."""
    starts = [m.start() for m in CALL.finditer(text)]
    return [(s, starts[i + 1] if i + 1 < len(starts) else len(text)) for i, s in enumerate(starts)]


def block_for(text, title, needle):
    """The text of the test holding the literal `title` or, failing that, the first occurrence of `needle`."""
    pos = -1
    if title:
        for q in ('"', "'", "`"):
            for form in (title, title.replace('"', '\\"'), title.replace("'", "\\'")):
                i = text.find(q + form + q)
                if i >= 0:
                    pos = i
                    break
            if pos >= 0:
                break
    if pos < 0 and needle:
        pos = text.find(needle)
    if pos < 0:
        return None
    for s, e in blocks_of(text):
        if s <= pos < e:
            return text[s:e]
    return text[pos:]


def labels_in(block):
    return sorted({next(g for g in m.groups() if g is not None) for m in LABEL.finditer(block)})


def clashes(needle, labels):
    """Labels of which `needle` is a proper prefix (a template counts by its text before the first ${)."""
    return [l for l in labels if l != needle and l.split("${")[0].startswith(needle)]


def test_title(block):
    m = re.match(r"[ \t]*(?:it|test)[^\n(]*\(\s*(?:\[[^\n]*\]\s*,\s*)?([\"'`])(.*?)\1", block, re.S)
    return m.group(2) if m else None


def norm(s):
    return re.sub(r"\s+", " ", s.replace("\\'", "'").replace('\\"', '"')).strip().lower()


def composed(needle, text):
    """A needle built at run time: its head is a literal label and its tail follows a `${...}` in a template."""
    for i in range(len(needle) - 1, 0, -1):
        if needle[i] == " " and needle[:i] in text and "}" + needle[i:] in text:
            return True
    return False


def title_templates(text):
    """Regex stems of the `${}` test titles of a file: each fragment before the last placeholder, joined by .+?."""
    out = []
    for m in re.finditer(r"(?:it|test)\(\s*`([^`\n]*\${[^`\n]*)`", text):
        frags = re.split(r"\$\{[^}]*\}", m.group(1))
        out.append(frags)
    return out


def template_matches(killer, frags):
    """The killer (a full title or a name prefix) fits a `${}` title: fragments exact, placeholders any text, the
    last fragment may be cut short."""
    if len(frags) < 2 or not frags[0].strip():
        return False
    last = frags[-1]
    tail = ""
    for ch in reversed(last):
        tail = "(?:" + re.escape(ch) + tail + ")?"
    parts = [re.escape(f) + ".+?" for f in frags[:-1]]
    return bool(re.match("^" + "".join(parts) + tail + "$", killer, re.S))


def title_matches(name, title):
    """A SPEC test name against a plant's killing-test title: equal, a prefix either way, or (for names written
    with an ellipsis or a placeholder) every fragment contained."""
    n, t = norm(name), norm(title)
    if not n or not t:
        return False
    if n == t or t.startswith(n) or n.startswith(t):
        return True
    frags = [f.strip(" .:;,") for f in re.split(r"…|\.\.\.|<[^>]*>", n)]
    frags = [f for f in frags if len(f) >= 8]
    return bool(frags) and all(f in t for f in frags)


# ---------------------------------------------------------------- checks


def targets(files, rel):
    """The stand-in paths a plant's file may name: exact, else those ending with it (plants.json is relative)."""
    if rel in files:
        return [rel]
    return [f for f in files if f.endswith("/" + rel)]


def _other_scripts(folder, source):
    """The text of every other Python script under the folder's tools/: a row whose id one quotes has an entry."""
    tools = folder / "tools"
    if not tools.is_dir():
        return ""
    return "\n".join(path.read_text(errors="replace") for path in sorted(tools.glob("*.py"))
                     if source is None or path.resolve() != Path(source).resolve())


def check_plants(folder, plants, source, plan_text, repo, env, files, findings, notes, unavailable,
                 require_plants=False):
    plan_name = "PLAN.md"
    rows = plan_rows(plan_text)
    by_row = {r["id"]: r for r in rows}
    by_plant = {p.id: p for p in plants}
    sname = source.name if source else "plants"

    def loc(p):
        if source:
            return f"{sname}:{line_of(source, p.id) or '?'}"
        return f"{plan_name}:{by_row[p.id]['line'] if p.id in by_row else '?'}"

    for r in rows:
        where = f"{plan_name}:{r['line']}"
        if r["has_killer_col"] and not (r["killer"] or "").strip():
            findings.append(Finding("PC-1", where, f"{r['id']} names no killing test"))
        known = by_plant.get(r["id"])
        if r["has_needle_col"] and (r["needle"] or "").strip() in ("", "-") and not (known and known.needle == "None"):
            findings.append(Finding("PC-1", where, f"{r['id']} names no needle"))
    others = _other_scripts(folder, source)
    if source:
        for p in plants:
            if p.id not in by_row:
                findings.append(Finding("PC-1", loc(p), f"{p.id} has no PLAN table row"))
        for r in rows:
            if r["id"] not in by_plant and not re.search(r"[\"']" + re.escape(r["id"]) + r"[\"']", others):
                findings.append(Finding("PC-1", f"{plan_name}:{r['line']}", f"{r['id']} has no entry in {sname}"))
    else:
        if (rows or require_plants) and not any(r["find"] for r in rows):
            findings.append(Finding("PC-1", plan_name, "no plants.py or plants.json, and no PLAN plant table with a "
                                                       "Find column"))
        for r in rows:
            plants.append(Plant(r["id"], r["file"], r["find"], r["replace"], killer=r["killer"], needle=r["needle"]))

    if env is None:
        if unavailable or require_plants:
            findings.append(Finding("PC-1", folder.name, unavailable or "no standin.patch and no task branch: the "
                                    "find, needle and label checks need the code"))
        elif plants:
            notes.append("no standin.patch and no task branch: the find, needle and label checks need the code "
                         "and were skipped")
        return
    cache = {}

    def content(path):
        if path not in cache:
            cache[path] = repo.show(env, path)
        return cache[path]

    testish = [f for f in files if TEST_FILE.search(f)]
    for p in plants:
        where = loc(p)
        if p.find is not None and p.file:
            hit = targets(files, p.file)
            full = (by_row.get(p.id) or {}).get("file") or ""
            if len(hit) != 1 and full.strip("` ") in files:  # plants.json names the file relative; the PLAN row is full
                hit = [full.strip("` ")]
            if len(hit) != 1:
                findings.append(Finding("PC-1", where, f"{p.id} file {p.file} matches {len(hit)} files of the stand-in"))
            elif p.find == "":
                findings.append(Finding("PC-1", where, f"{p.id} has an empty find"))
            else:
                n = (content(hit[0]) or "").count(p.find)
                if n != 1:
                    findings.append(Finding("PC-1", where, f"{p.id} find occurs {n} times in {hit[0]} (must be once)"))
                else:
                    p.path = hit[0]
        text = None
        if p.testfile:
            path = f"{p.member}/{p.testfile}" if p.member else p.testfile
            text = content(path)
            if text is None:
                findings.append(Finding("PC-1", where, f"{p.id} test file {path} is not in the stand-in"))
        elif p.killer:
            blob = "\n".join(content(f) or "" for f in testish)
            stem = re.split(r"…|\.\.\.", p.killer)[0].replace('"', '\\"')
            tpls = [t for f in testish for t in title_templates(content(f) or "")]
            if stem not in blob and stem.replace('\\"', '"') not in blob \
                    and not any(template_matches(p.killer, t) for t in tpls):
                findings.append(Finding("PC-1", where, f"{p.id} killing test {p.killer[:70]!r} is not in a stand-in test file"))
        if text is None:
            continue
        if p.needle and re.match(r"(case|check):[^\s']", p.needle):
            heads = [l.split("${")[0] for l in labels_in(text) if "${" in l]
            if p.needle not in text and not any(h and p.needle.startswith(h) for h in heads) \
                    and not composed(p.needle, text):
                findings.append(Finding("PC-1", where, f"{p.id} needle {p.needle!r} is not in {p.testfile}"))
            block = block_for(text, p.title, p.needle)
            if block is not None:
                p.killer = p.killer or test_title(block)
                for label in clashes(p.needle, labels_in(block)):
                    findings.append(Finding("PC-2", where, f"{p.id} needle {p.needle!r} is a prefix of label "
                                                           f"{label!r} in {p.testfile}"))
        if not p.killer and p.title:
            p.killer = p.title


def check_failure_modes(folder, plants, findings, require_plants=False):
    spec = folder / "SPEC.md"
    if not spec.exists():
        findings.append(Finding("PC-3", folder.name, "SPEC.md is missing"))
        return
    ids = {p.id for p in plants}
    killers = [k for p in plants for k in (p.killer, p.title) if k]
    seen = False
    for name, lines in sections(read_text(spec)):
        if not name.lower().startswith("failure modes"):
            continue
        for header, body in tables(lines):
            row_col = column(header, "row")
            test_col = next((i for i, h in enumerate(header) if "test" in h), None)
            for n, cells in body:
                seen = True
                label = (cells[0] if cells else "?")[:60]
                if row_col is not None and (plants or require_plants):
                    refs = re.findall(r"\b[A-Z]{1,3}\d+\b", cells[row_col]) if row_col < len(cells) else []
                    if not any(r in ids for r in refs):
                        findings.append(Finding("PC-3", f"SPEC.md:{n}", f"failure mode {label!r} has no plant row"))
                    continue
                if test_col is None or test_col >= len(cells) or not cells[test_col].strip():
                    findings.append(Finding("PC-3", f"SPEC.md:{n}", f"failure mode {label!r} names no test"))
                    continue
                if not plants and not require_plants:
                    continue
                names = [a or b for a, b in re.findall(r"`([^`]+)`|\"([^\"]+)\"", cells[test_col])]
                if not any(title_matches(nm, k) for nm in names for k in killers):
                    findings.append(Finding("PC-3", f"SPEC.md:{n}", f"failure mode {label!r} has no plant (none of "
                                                                    f"its tests is a plant's killing test)"))
    if not seen:
        findings.append(Finding("PC-3", "SPEC.md", "no Failure Modes table found"))


CONFLICT = re.compile(r"Applied patch to '([^']+)' with conflicts|error: patch failed: ([^:]+):|"
                      r"error: ([^:\n]+): (?:already exists in index|does not exist in index|does not match index)")


def conflict_files(stderr):
    out = []
    for m in CONFLICT.finditer(stderr):
        f = next(g for g in m.groups() if g)
        if f not in out:
            out.append(f)
    return out


def why(res):
    files = conflict_files(res.stderr)
    return ", ".join(files) if files else (res.stderr.strip().splitlines() or ["git apply failed"])[0]


def check_patch(patch, repo, findings, own):
    base = "main" if repo.ref_ok("main") else "HEAD"
    res, _ = repo.apply(base, patch, "main-check")
    if res.returncode != 0:
        findings.append(Finding("PC-4", patch.name, f"does not apply to {base}: {why(res)}"))
    for branch, sha in repo.task_tips():
        if branch in own:
            continue
        res, _ = repo.apply(sha, patch, "branch-check", three_way=True)
        if res.returncode != 0 or "with conflicts" in res.stderr:
            findings.append(Finding("PC-4", patch.name, f"three-way apply onto {branch} conflicts: {why(res)}"))


def check_commands(folder, findings):
    import gate  # late: the gate imports most of the kit

    plan = folder / "PLAN.md"
    if not plan.exists():
        findings.append(Finding("PC-5", folder.name, "PLAN.md is missing"))
        return
    try:
        found = gate.parse_plan(read_text(plan))
    except KitError as exc:
        text = str(exc).splitlines()
        findings.append(Finding("PC-5", plan.name, "gate.parse_plan failed: " + (text[0] if text else repr(exc))))
        return
    if not found:
        findings.append(Finding("PC-5", plan.name, "gate.parse_plan finds no `$ ` command under ## Exact Commands"))


def _error_line(tail):
    """The line of a build's output tail most worth quoting: the last one that mentions an error, else the last."""
    lines = [line.strip() for line in tail if line.strip()]
    flagged = [line for line in lines if "error" in line.lower()]
    return (flagged or lines or ["no output"])[-1][:200]


def check_builds(root, plants, base, patch, findings, timeout):
    """PC-6: each plant's replacement alone, on a mirror of the code, through main's build and compile_check."""
    import gate  # late: the gate imports most of the kit
    from evidence import PlantJudge

    try:
        checks = gate.load_checks_at(root, gate.MAIN)
    except KitError as exc:
        findings.append(Finding("PC-6", "program/checks.yaml", f"cannot be read on main: {exc}"))
        return
    builds = checks["build"] + checks["compile_check"]
    if not builds:
        findings.append(Finding("PC-6", "program/checks.yaml", "main lists no build or compile_check command"))
        return

    def prepare(mirror):
        if patch is not None:
            res = subprocess.run(["git", "-C", str(mirror), "apply", str(patch)], capture_output=True, text=True)
            if res.returncode != 0:
                raise KitError(f"{patch.name} does not apply in the mirror: {res.stderr.strip()[:200]}")

    rows = [p for p in plants if p.path and isinstance(p.replace, str) and p.find]
    with PlantJudge(root, base, prepare=prepare, timeout=timeout) as judge:
        first = judge.baseline(builds, None)
        if not first["ok"]:
            findings.append(Finding("PC-6", patch.name if patch else base, f"the unmutated code does not build: "
                                    f"{' '.join(first['argv'])} exit {first['exit']}: {_error_line(first['tail'])}"))
            return
        for p in rows:
            result = judge.attempt({"id": p.id, "path": p.path, "find": p.find, "replace": p.replace,
                                    "build_argvs": builds, "test_argv": None})
            if result["problem"]:
                findings.append(Finding("PC-6", p.id, f"{p.id} not built: {result['problem']}"))
            elif not result["builds"]:
                findings.append(Finding("PC-6", p.id, f"{p.id} replace does not build: "
                                        f"{' '.join(result['build_argv'])} exit {result['build_exit']}: "
                                        f"{_error_line(result['build_tail'])} (the gate would judge it build failed, "
                                        "not killed)"))


def precheck(folder, root, build=False, timeout=None, notes=None, require_plants=False):
    """The findings of the task folder `folder` against the repository at `root`; `notes` collects what was skipped."""
    folder = Path(folder).resolve()
    root = Path(root).resolve()
    notes = notes if notes is not None else []
    findings = []
    plan = folder / "PLAN.md"
    plan_text = read_text(plan) if plan.exists() else ""
    repo = Repo(root)
    try:
        plants, source = load_plants(folder)
        patch = folder / "standin.patch"
        own = {f"task/{folder.name}", f"origin/task/{folder.name}"}
        base = "main" if repo.ref_ok("main") else "HEAD"
        env, files, unavailable, code_ref = None, [], None, None
        if patch.exists():
            res, env = repo.apply(base, patch, "content")
            if res.returncode == 0:
                code_ref = base
            else:
                env, unavailable = None, "stand-in content unavailable (see PC-4): find, needle and label checks skipped"
        else:
            for ref in (f"refs/heads/task/{folder.name}", f"refs/remotes/origin/task/{folder.name}"):
                if repo.ref_ok(ref):
                    env, code_ref = repo.index_env("content"), ref
                    repo.run(["read-tree", ref], env=env)
                    break
        if env is not None:
            files = repo.ls(env)
        check_plants(folder, plants, source, plan_text, repo, env, files, findings, notes, unavailable,
                     require_plants)
        if not plants and not require_plants:
            notes.append("no plant table in PLAN.md and no plants.py or plants.json: the plant checks were skipped "
                         "(--require-plants makes that a finding)")
        check_failure_modes(folder, plants, findings, require_plants)
        if patch.exists():
            check_patch(patch, repo, findings, own)
        check_commands(folder, findings)
        if build:
            if code_ref is None:
                notes.append("--build needs the code (a stand-in that applies, or the task branch): nothing built")
            elif plants:
                check_builds(root, plants, code_ref, patch if patch.exists() else None, findings, timeout)
    finally:
        repo.close()
    return findings


def _folder(root: Path, given: str) -> Path:
    path = Path(given)
    if path.is_dir():
        return path
    under = root / "program" / "tasks" / given
    if under.is_dir():
        return under
    raise KitError(f"{given} is neither a folder nor a task under program/tasks/")


def cmd_spec(args: argparse.Namespace) -> int:
    if getattr(args, "spec_action", None) != "precheck":
        raise KitError("spec needs an action: precheck")
    root = find_root()
    notes: list[str] = []
    findings = precheck(_folder(root, args.folder), root, build=args.build, timeout=args.timeout, notes=notes,
                        require_plants=args.require_plants)
    for note in notes:
        print(f"note: {note}")
    for finding in findings:
        print(finding)
    if not findings:
        print("spec precheck: clean")
    return 1 if findings else 0


def _configure(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="spec_action", metavar="action", parser_class=type(parser))
    check = actions.add_parser("precheck", help="the spec author's check before spec QA: plant rows, needles and "
                                                "finds, test labels, failure modes, the stand-in patch, the PLAN")
    check.add_argument("folder", metavar="FOLDER", help="the task folder, or a task ID under program/tasks/")
    check.add_argument("--build", action="store_true",
                       help="also build each plant row's replacement on a throwaway mirror (main's build and "
                            "compile_check) and report the rows that do not build")
    check.add_argument("--require-plants", action="store_true",
                       help="a task folder with no plant table, or no code to check it against, is a finding, "
                            "and every failure mode needs a plant")
    check.add_argument("--timeout", type=int, default=3600, metavar="SECONDS",
                       help="per build command with --build (default 3600)")


def register(add_command) -> None:
    add_command("spec", cmd_spec, help="spec author's tools: precheck", configure=_configure)
