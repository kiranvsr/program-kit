# Failure-modes rubric

- FM-1 Each requirement the task closes has at least one failure mode with a literal test name.
- FM-2 Dependencies fail: the database, a queue, an external service, a timer or the clock; each has a defined, tested outcome.
- FM-3 Partial work: a run stopped part-way leaves a state that a retry completes without duplicates.
- FM-4 Concurrency: two requests at once, a state change while a page is open, a duplicate event; the outcome is defined and tested.
- FM-5 Time: expiries, timeouts and windows at their edges; a slow dependency does not eat a user's time or hold a lock.
- FM-6 Limits: what happens at and over each limit, and that the answer names its status and error type.
- FM-7 Observability: each failure emits a countable signal without personal data.
- FM-8 Recovery: the operator's path back (retry, repair, re-run) is stated and tested.
