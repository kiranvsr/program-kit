# Changelog

## 0.2.0

Breaking change: the evidence layout.

- **In-repo, append-only records.** A task's evidence is no longer one `program/evidence/<ID>/results.json` and one `acceptance.json` rewritten in place. Each run appends a new, read-only record named by its own digest, `program/evidence/<ID>/results/<utc>-<sha256[:12]>.json` (QA's acceptances under `acceptance/`), and records are never modified or deleted (rule 11). Each task's pointer `program/evidence/<ID>/latest.json` names its latest results and acceptance record and is the only mutable evidence file; a task branch edits only its own pointer, so evidence never conflicts on merge. `pk evidence validate` with no record, the hand-back gate and `pk doctor` refuse a record whose bytes no longer match its name, a commit that modified or deleted a record, and a pointer entry that does not resolve to a committed record. `pk evidence history <ID>` lists every record of a task. A refresh appends a record with `supersedes`; a re-acceptance appends an acceptance (`pk evidence accept`).
- **Drift replaces staleness.** A record states what was verified at its `head` and stays true; a later change only makes it drift. `pk stale` is now a drift report (`fresh` or `drifted since <head>`, against the checkout's HEAD) that exits 0 unless `--strict`. Records no longer carry a `stale` field and `Evidence.stale()` is gone. The hand-back gate asks a task for a new record only when its own inputs changed; `pk gate binding` reports drift instead of failing on it; `done` needs the latest acceptance to bind the latest results record and its head to be an ancestor of HEAD. A gate record binds its children by digest and records each child's drift.
- **Acyclic closure.** A record binds only inputs, never a derived output (rule 12). `pk evidence lint` names offending inputs; `Evidence.write`, the gate, the binding check and `done` refuse them. Only a gate record binds other records, by digest; nothing binds a gate record.
- **API.** `Evidence.write()` takes no path and returns `{digest, path, file}`. New commands: `pk evidence lint`, `history`, `accept`, `import` and `validate` with no record (`init` creates nothing); `pk stale --strict`. `pk doctor` runs the evidence integrity checks and warns about a record pending commit.
- **Import path for 0.1 projects.** Run `pk evidence import`: it copies each `results.json` and `acceptance.json` byte for byte into the new layout as the first history entry, writes each task's pointer, and prints the exact `git rm` lines for the originals (it never deletes them). `gates.yaml` records may keep `path: program/evidence/<ID>/results.json` or say `task: <ID>`.

## 0.1.1

Review fixes in the gate, status, evidence, fetch, common, pk and install paths.

## 0.1.0

First release.
