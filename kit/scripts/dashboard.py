#!/usr/bin/env python3
"""`pk dashboard <out.html>`: the program's state as one self-contained HTML page (standard library only). Optional.

The page shows what the kit already knows, read only from kit-managed files: the generated hand-off block (rendered
now, exactly as `pk handoff update` would write it), the register with each task's status and tier, the status
counts, the gates of `program/gates.yaml` with the status of the tasks whose records prove them, the cost log
(totals per task and per model, the agents past the fresh-agent threshold) and the open follow-ups. It adds no
state of its own and decides nothing: the hand-off stays the document a successor reads, and every check stays a
command. The page carries no script, no external asset and no wall-clock time, so the same commit and state give
the same bytes; it names the commit it was made at.

It writes the one file it is given and nothing else. A page written inside the repository makes the tree dirty,
which the gates refuse: write it outside, or to a path the project ignores.
"""
from __future__ import annotations

import argparse
import html
from pathlib import Path

import cost as cost_module
import ctlstate
import handoff as handoff_module
import register as register_module
import status as status_module
from common import KitError, find_root, git, read_yaml, write_text

ORDER = ("done", "merged", "review", "in_progress", "blocked", "parked", "not_started")


def _e(value) -> str:
    return html.escape(str(value), quote=True)


def _block_html(block: str) -> str:
    """The generated hand-off block (a small, fixed Markdown shape) as HTML: headings, bullet lists, lines."""
    out, in_list = [], False
    for line in block.splitlines():
        if line.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_e(html.unescape(line[2:]))}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if line.startswith("#### "):
            out.append(f"<h3>{_e(html.unescape(line[5:]))}</h3>")
        elif line.startswith("### "):
            continue  # the page has its own title
        elif line.strip():
            out.append(f"<p class=meta>{_e(html.unescape(line))}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def _register_rows(root: Path) -> list[dict]:
    """Every register task in order: {phase, id, title, status, tier}; empty without a register."""
    if not (Path(root) / "program" / "register.yaml").is_file():
        return []
    records = {record["task_id"]: record for record in status_module.list_all(root)}

    def children(node, key):
        items = node.get(key) if isinstance(node, dict) else None
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    rows = []
    for phase in children(register_module.load(root), "phases"):
        for milestone in children(phase, "milestones"):
            for step in children(milestone, "steps"):
                for task in children(step, "tasks"):
                    if not isinstance(task.get("id"), str):
                        continue
                    record = records.get(task["id"])
                    rows.append({"phase": f"{phase.get('id', '')} {phase.get('title', '')}".strip(),
                                 "id": task["id"], "title": task.get("title", ""),
                                 "status": record["status"] if record else "not_started",
                                 "tier": status_module.tier_of(record) if record else ""})
    return rows


def _gate_rows(root: Path, statuses: dict[str, str]) -> list[dict]:
    path = Path(root) / "program" / "gates.yaml"
    data = read_yaml(path) if path.is_file() else None
    gates = data.get("gates") if isinstance(data, dict) else None
    rows = []
    for gate_id, entry in (gates or {}).items() if isinstance(gates, dict) else ():
        entry = entry if isinstance(entry, dict) else {}
        claims = entry.get("claims") if isinstance(entry.get("claims"), list) else []
        records = entry.get("records") if isinstance(entry.get("records"), list) else []
        tasks = [str(item.get("task") or item.get("path") or "?") for item in records if isinstance(item, dict)]
        rows.append({"id": gate_id, "title": entry.get("title", ""), "environment": entry.get("environment", ""),
                     "claims": len(claims), "records": [(task, statuses.get(task, "")) for task in tasks]})
    return rows


def _chip(state: str) -> str:
    return f'<span class="chip s-{_e(state)}">{_e(state.replace("_", " "))}</span>' if state else ""


def _bar(counts: dict[str, int]) -> str:
    total = sum(counts.values()) or 1
    return "".join(f'<span class="s-{state}" style="width:{100 * counts[state] / total:.2f}%" '
                   f'title="{state}: {counts[state]}"></span>' for state in ORDER if counts.get(state))


def _table(head: list[str], rows: list[list[str]], empty: str) -> str:
    if not rows:
        return f'<p class="empty">{_e(empty)}</p>'
    cells = "".join(f"<tr>{''.join(f'<td>{cell}</td>' for cell in row)}</tr>" for row in rows)
    return f"<table><thead><tr>{''.join(f'<th>{_e(h)}</th>' for h in head)}</tr></thead><tbody>{cells}</tbody></table>"


def render(root: Path) -> tuple[str, dict]:
    """(the page, a summary {tasks, gates, tokens}) for the checkout at `root`."""
    root = Path(root)
    block = handoff_module.render(root)  # raises, as `pk handoff update` would, when the state does not read
    rows = _register_rows(root)
    statuses = {row["id"]: row["status"] for row in rows}
    statuses.update({record["task_id"]: record["status"] for record in status_module.list_all(root)})
    counts = {state: 0 for state in ORDER}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    gates = _gate_rows(root, statuses)
    records = cost_module.read(root)
    per_task = cost_module.totals(records)
    per_model: dict[str, int] = {}
    for record in records:
        per_model[record["model"]] = per_model.get(record["model"], 0) + record["tokens"]
    tokens = sum(per_model.values())
    followups = sorted(ctlstate.load(root)["followups"], key=lambda item: item["id"], reverse=True)
    commit = git(root, "rev-parse", "--short=12", "HEAD")
    done = counts["done"] + counts["merged"]

    phases: dict[str, list[dict]] = {}
    for row in rows:
        phases.setdefault(row["phase"], []).append(row)
    register_html = "".join(
        f"<details open><summary><span>{_e(phase)}</span><span class=meta>"
        f"{sum(r['status'] in ('done', 'merged') for r in tasks)} of {len(tasks)} done</span>"
        f'<span class="bar">{_bar({s: sum(r["status"] == s for r in tasks) for s in ORDER})}</span></summary>'
        + _table(["Task", "Title", "Status", "Tier"],
                 [[f"<code>{_e(r['id'])}</code>", _e(r["title"]), _chip(r["status"]), _e(r["tier"])] for r in tasks],
                 "no tasks")
        + "</details>" for phase, tasks in phases.items()) or '<p class="empty">No register yet (stage 6).</p>'
    tiles = "".join(f'<div class="tile"><div class="n">{_e(n)}</div><div class="l">{_e(label)}</div></div>' for n, label in (
        (len(rows), "tasks in the register"), (done, "done or merged"),
        (counts["in_progress"] + counts["review"], "in flight"), (counts["blocked"], "blocked"),
        (len(followups), "open follow-ups"), (f"{tokens:,}", "tokens logged")))
    legend = " ".join(f'{_chip(state)} {counts[state]}' for state in ORDER)
    gates_html = _table(["Gate", "Environment", "Claims", "Records (status)"], [
        [f"<code>{_e(g['id'])}</code><br><span class=meta>{_e(g['title'])}</span>", _e(g["environment"]),
         _e(g["claims"]), "<br>".join(f"<code>{_e(task)}</code> {_chip(state)}" for task, state in g["records"])]
        for g in gates], "No gates in program/gates.yaml yet.")
    costs_html = _table(["Task", "Tokens", "Runs"], [
        [f"<code>{_e(task)}</code>", f"{row['tokens']:,}", _e(row["runs"])] for task, row in per_task.items()],
        "No runs in program/costs.jsonl yet.") + _table(["Model", "Tokens"], [
            [_e(model), f"{count:,}"] for model, count in sorted(per_model.items(), key=lambda kv: -kv[1])], "")
    flagged = "".join(f"<li>{_e(line)}</li>" for line in cost_module.flagged_lines(root))
    followups_html = "".join(f"<li><code>{_e(item['id'])}</code> <b>{_e(item['area'])}</b>: {_e(item['item'])}"
                             + (f" <span class=meta>[{_e(', '.join(item['tags']))}]</span>" if item["tags"] else "")
                             + "</li>" for item in followups)
    page = TEMPLATE
    for key, value in (("COMMIT", _e(commit)), ("TILES", tiles), ("BAR", _bar(counts)), ("LEGEND", legend),
                       ("HANDOFF", _block_html(block)), ("REGISTER", register_html), ("GATES", gates_html),
                       ("COSTS", costs_html), ("FLAGGED", f"<ul>{flagged}</ul>" if flagged else ""),
                       ("FOLLOWUPS", f"<ul>{followups_html}</ul>" if followups_html
                        else '<p class="empty">None open.</p>')):
        page = page.replace(f"@@{key}@@", value)
    return page, {"tasks": len(rows), "gates": len(gates), "tokens": tokens}


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Program dashboard</title>
<style>
:root{--bg:#f6f5f1;--surface:#fff;--fg:#1d2126;--muted:#5d6670;--line:#dcdad2;--ok:#2f7d4a;--info:#2f5f9e;
--warn:#b2691b;--crit:#b2362e;--idle:#8a9199;--mono:ui-monospace,Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#15181c;--surface:#1d2127;--fg:#e8e6df;--muted:#9aa3ad;--line:#2d333b;
--ok:#6cc08a;--info:#7fa6dc;--warn:#e0a35a;--crit:#e3736a;--idle:#7a838c;color-scheme:dark}}
body{margin:0;padding:0 16px 48px;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
.wrap{max-width:1200px;margin:0 auto}
header{display:flex;flex-wrap:wrap;gap:8px 20px;align-items:baseline;padding:24px 0 10px;border-bottom:2px solid var(--fg)}
h1{margin:0;font-size:1.6rem}h2{font-size:1.1rem;margin:28px 0 10px}h3{font-size:.95rem;margin:14px 0 4px}
.meta{color:var(--muted);font-size:.82rem}code{font-family:var(--mono);font-size:.82rem}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-top:16px}
.tile,.panel,details{background:var(--surface);border:1px solid var(--line);padding:12px 14px;min-width:0}
.tile .n{font-size:1.6rem;font-weight:700;font-variant-numeric:tabular-nums}
.tile .l{font-size:.75rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.bar{display:flex;height:12px;border:1px solid var(--line);overflow:hidden;margin:8px 0;min-width:120px}
.bar span{display:block;height:100%}
.chip{display:inline-block;font-size:.72rem;font-weight:600;padding:0 8px;border-radius:999px;white-space:nowrap;
border:1px solid currentColor}
.s-done,.s-merged{color:var(--ok)}.s-review{color:var(--info)}.s-in_progress{color:var(--warn)}
.s-blocked{color:var(--crit)}.s-parked,.s-not_started{color:var(--idle)}
.bar .s-done,.bar .s-merged{background:var(--ok)}.bar .s-review{background:var(--info)}
.bar .s-in_progress{background:var(--warn)}.bar .s-blocked{background:var(--crit)}.bar .s-parked{background:var(--idle)}
.grid{display:grid;grid-template-columns:3fr 2fr;gap:20px;align-items:start}
@media (max-width:860px){.grid{grid-template-columns:1fr}}
details{margin-bottom:10px}summary{display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center;cursor:pointer;font-weight:600}
summary .bar{flex:0 0 160px;margin:0}
table{border-collapse:collapse;width:100%;font-size:.86rem;margin-top:6px}
th{text-align:left;font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);
padding:5px 8px;border-bottom:1px solid var(--line)}
td{padding:5px 8px;border-bottom:1px solid var(--line);vertical-align:top}
.panel{overflow-x:auto}ul{padding-left:1.2em;margin:4px 0}li{margin:3px 0}.empty{color:var(--muted);font-style:italic}
</style>
</head>
<body>
<div class="wrap">
<header><h1>Program dashboard</h1><span class="meta">at commit @@COMMIT@@ &middot; made by pk dashboard from the
kit's files; the hand-off and the commands are the record</span></header>
<div class="tiles">@@TILES@@</div>
<div class="bar">@@BAR@@</div>
<div class="meta">@@LEGEND@@</div>
<div class="grid">
<div>
<h2>Register</h2>
@@REGISTER@@
</div>
<div>
<h2>Generated hand-off state</h2>
<div class="panel">@@HANDOFF@@</div>
</div>
</div>
<h2>Gates</h2>
<div class="panel">@@GATES@@</div>
<div class="grid">
<div><h2>Costs</h2><div class="panel">@@COSTS@@@@FLAGGED@@</div></div>
<div><h2>Open follow-ups</h2><div class="panel">@@FOLLOWUPS@@</div></div>
</div>
</div>
</body>
</html>
"""


# --- command -----------------------------------------------------------------

def cmd_dashboard(args: argparse.Namespace) -> int:
    root = find_root()
    out = Path(args.out)
    if out.is_dir():
        raise KitError(f"{out} is a directory; name the HTML file to write")
    if not out.parent.is_dir():
        raise KitError(f"{out.parent} does not exist; create it first")
    page, summary = render(root)
    write_text(out, page)
    print(f"dashboard: wrote {out} ({summary['tasks']} tasks, {summary['gates']} gates, "
          f"{summary['tokens']:,} tokens logged)")
    return 0


def register(add_command) -> None:
    add_command("dashboard", cmd_dashboard, help="optional: write the program's state as one self-contained HTML page",
                configure=lambda p: p.add_argument("out", metavar="out.html", help="the file to write"))
