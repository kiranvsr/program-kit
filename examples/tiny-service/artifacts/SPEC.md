# Specification

*Stage 3 artifact. Frozen at approval 5; after that it changes only through change requests.*

## Requirements

- **REQ-001** (J1). GET /health answers 200 with the JSON `{"status": "ok"}`. No input. Error: any other method on /health answers 405.
- **REQ-002** (J2). POST /echo with a valid JSON body answers 200 with `{"echo": <the parsed body>}`. Any JSON value is valid: object, array, string, number, boolean or null.
- **REQ-003** (J2). POST /echo with a body that is not valid UTF-8 JSON, or an empty body, answers 400 with `{"error": "body is not valid JSON"}`.
- **REQ-004** (J1, J2). An unknown path answers 404 with `{"error": "not found"}`. A method other than the one a path allows answers 405 with `{"error": "method not allowed"}`.
- **REQ-005** (J2). A request body of more than 1024 bytes answers 413 with `{"error": "body too large"}`. A body of exactly 1024 bytes is accepted. A Content-Length that is not a non-negative integer answers 400.

Every answer carries `Content-Type: application/json`.

## Non-Functional Requirements

- **NFR-001** Security. The service binds 127.0.0.1 by default. It keeps no request data and never writes an echoed body to a log or a file. Target: the default host is 127.0.0.1; no code path writes a body anywhere.
- **NFR-002** Observability. One access log line per request, with method, path and status, never the body. Target: exactly one line per request.
- Scale and latency: no target yet. Owner: the service owner. Date: 2026-12-01.
- Availability and recovery: no target. An operator restarts the process by hand.
- Accessibility: not applicable, the service is an API for programs.
- Compliance, tenancy, data retention and residency: not applicable. One tenant, no stored data.

## Domain Objects And Interfaces

No stored objects. The interface is HTTP on the host and port given on the command line (default 127.0.0.1 and 8080): GET /health, POST /echo. Answers are JSON objects. The Python entry point is `handle(method, path, body)`, which returns a status and a JSON-able payload.

## Security And Tenancy Boundaries

The trust boundary is the loopback address. There is no authentication and no TLS; both are the later release named secured. Every request body is untrusted: it is capped at 1024 bytes, parsed as JSON and returned to the sender only. A single tenant. No secrets exist.

## Failure Modes And Recovery

| Mode | Detected by | User sees | Recovery | Data at risk |
| --- | --- | --- | --- | --- |
| Process down | the caller | connection refused | the operator restarts the process | none |
| Body is not JSON | the service | 400 and a JSON error | the caller fixes the request | none |
| Body over the limit | the service | 413 and a JSON error | the caller sends less | none |
| Bad Content-Length | the service | 400 and a JSON error | the caller fixes the request | none |
| Unknown path or method | the service | 404 or 405 and a JSON error | the caller fixes the request | none |

No slow, corrupt or data-loss case exists: there is no integration and no stored state.

## Observability

Today: `/health`, which says the process is up. From the second task: one access log line per request on standard error (NFR-002). The operator reads it at the terminal; there is no alerting.

## Open Questions

None open. The earlier question Q-1 (does a staging environment exist?) was answered in discovery: no.

## Change Log

*Empty until the first change after approval.*
