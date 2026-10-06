# Architecture

*Stage 4 artifact. Frozen at approval 5; after that it changes only through change requests.*

## Candidate Approaches And Choice

- **A. http.server from the standard library.** One small module, no dependency, threads built in. Not meant for the open internet, which is fine on a loopback address.
- **B. wsgiref from the standard library.** Same dependency story, but needs a WSGI application and a second layer for no gain at two endpoints.
- **C. A third-party web framework.** Rich, but a forbidden dependency (F-5) for two endpoints.

Recommendation: A. The user chose A (decision D-8, ADR 0001).

## Components

- **C1 service** in `src/tiny_service.py`. One responsibility: answer GET /health and POST /echo. It owns REQ-001 to REQ-005, NFR-001 and NFR-002. Interface: HTTP, and the function `handle(method, path, body)`. It holds no data. Owner role: backend. Built, not bought.

## Data Flow

A request arrives on a socket. The handler reads at most 1025 bytes of the body and passes method, path and body to `handle`, which returns a status and a payload. The handler writes the payload as JSON. Nothing is stored.

## Deployment Per Environment

- **dev:** `python3 src/tiny_service.py [--host H] [--port P]` on a developer machine. Defaults 127.0.0.1 and 8080.
- **staging:** does not exist; nothing is deployed there and nothing is claimed for it.

There is no production. The path from merge to running ends at the merge to main. Rollback is reverting the commit and restarting the process. There are no migrations.

## Cross-Cutting Concerns

- Authentication and authorization: none; the loopback bind is the boundary (NFR-001). Authentication is the later release secured.
- Tenancy: one tenant.
- Observability: one access log line per request, never the body (NFR-002), added by the second task.
- Data lifecycle: no data.
- Configuration and secrets: two command-line options, no secrets.
- Versioning and errors: no versioning. Errors are a JSON object `{"error": text}` with a 4xx status.

## Technology Decisions

- Python standard library `http.server`: `adr/0001-stdlib-http.md`.

## Quality Gates

- **G-P1** proves, in dev, that the service answers both endpoints as REQ-001 to REQ-005 say and meets NFR-001 and NFR-002. Evidence: each task's `results.json` and `acceptance.json`. No gate is blocked: no claim needs staging.

## Delivery Mechanics

One branch per task, `task/<ID>`, built in its own worktree and merged with `--no-ff` after the hand-back gate and independent QA. Checks: build (compileall), unit tests (unittest in `src/`), scan (pip check and gitleaks). Done means the gate passed and QA accepted for development. Every requirement has a unit test and every planted bug is caught.

## Change Log

*Empty until the first change after approval.*
