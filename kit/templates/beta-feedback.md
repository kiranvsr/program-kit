# Feedback register

The user's feedback messages during a beta or a pilot, one row each. The controller adds a row when the message
arrives (`pk feedback add`) and updates it as the item moves (`pk feedback set`); the dashboard's Feedback panel
and one line of the hand-off are made from this table. Feedback goes ahead of planned work by its urgency.

- **BF**: `BF-n`, numbered in order of arrival, never reused.
- **Date**: the day the feedback came in, `YYYY-MM-DD`.
- **Urgency**: `urgent` (preempts the lowest-priority work in flight), `high` (takes the next free slot) or
  `polish` (batched).
- **Summary**: one line, in the user's terms.
- **Touches**: the task IDs it touches, comma separated, or `none`.
- **Decision**: `fold-in` (into a task in progress or waiting), `follow-up` (a new task after the touched one),
  `pull-forward` (a register task moved up) or `batched` (with other polish).
- **State**: `open`, `in progress`, `done` or `declined`.
- **Ruling**: the ruling that decided it (`R<n>`, several comma separated), or `-` while none has.

| BF | Date | Urgency | Summary | Touches | Decision | State | Ruling |
|---|---|---|---|---|---|---|---|
