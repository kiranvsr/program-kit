# Research (not for implementation)

This folder holds research: positioning, strategy options, a draft roadmap. It records what is being considered, the arguments, and the choices as they are made.

**The rules**

- **Isolated.** Nothing here is a task, a register item or a requirement. The controller does not dispatch work from it, and no brief, spec, plan, ruling or release cites an item here as a go. `pk research check` refuses a task or brief file that cites an item that has not graduated.
- **It graduates only on the user's word.** When a direction is decided and the user says it should be built, the controller records a ruling in `program/rulings.md` that quotes the item's ID and its decided text and turns it into register work (a change request after the freeze). Then `pk research graduate <ID> --ruling R<n>` writes the item's status. Until then, the item stays here.
- **Keep the context.** Record each discussion with its date, who decided and why. Add new entries; do not rewrite old ones. When a choice changes, the item shows the new choice and points at the entry.
- **Separate claims from facts.** A market or competitor statement is a hypothesis until it is checked, then it gets a source.

## Items

One row per item: `| <ID> | [title](file) | status |`, the link relative to this file.

| ID | Item | Status |
| --- | --- | --- |

Status values: `open` (questions listed), `deliberating` (options being weighed), `decided` (choice made, not yet for building), `graduated by R<n> (<date>)` (moved into the pipeline by that ruling), `dropped`.
