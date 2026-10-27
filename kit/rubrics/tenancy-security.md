# Tenancy and security rubric

- TS-1 Tenancy: every read and write is scoped to the caller's tenant (row-level security or an equivalent guard on every query, a tenant predicate on every join and upsert, the tenant taken from the session, never from input). A second tenant is proved unable to read or change the rows.
- TS-2 Access: each route or entry point names its roles; a privileged action refuses the other roles with a tested status and error type.
- TS-3 Secrets and personal data: no token, key, id, email or contact data in URLs, logs, metric labels, error text, caches or browser storage; redaction cases cover ids, uuids, long hex strings and URLs as well as tokens.
- TS-4 Tokens and signatures: signed values have known-answer tests; rotation and expiry are tested; comparisons are constant time.
- TS-5 Privileged code paths: database functions or services that run with raised rights fix their search path or scope, qualify every name, and are granted only to the role that needs them. No statement bypasses the guard its neighbours go through.
- TS-6 Inputs: bodies are size-capped before parsing; every external value is validated before use (markup, queries, URLs, file paths).
- TS-7 Outbound calls: credentials stay in the adapter; failures fail safe; an interrupted call cannot lose state (for example a single-use refresh token).
- TS-8 Boundaries: no third-party name or wire detail outside the adapter that wraps it, where the program has that rule.
- TS-9 Abuse: limits and rate gates are keyed per caller or tenant, proved with a second caller.
