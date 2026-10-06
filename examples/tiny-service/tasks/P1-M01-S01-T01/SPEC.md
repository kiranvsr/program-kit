# Task Spec: P1-M01-S01-T01 Health and echo endpoints

## Summary

Build the whole first version of the service: `src/tiny_service.py` answers GET /health and POST /echo over `http.server`, refuses bad input with JSON errors, caps the body at 1024 bytes and binds 127.0.0.1 by default. Unit tests cover every requirement.

## Scope In

- `src/tiny_service.py`: `handle(method, path, body)`, the request handler, `make_server`, a command line with `--host` and `--port`.
- `src/test_tiny_service.py`: unit tests, with a real socket test for each endpoint.
- `program/evidence/P1-M01-S01-T01/produce.py`: the evidence producer.

## Scope Out

- The access log (NFR-002): task P1-M01-S01-T02. The handler stays silent until then.
- Authentication and TLS: the later release named secured.
- Any file other than the three above. No guarded file is edited, so there is no `Guarded edit:` line.

## Dependencies

None. The only environment is `dev`, which exists. Nothing here needs staging, which does not exist; nothing is claimed for it.

## Consumed Contracts

- SPEC.md: REQ-001 to REQ-005 and NFR-001 (statuses, bodies and the 1024 byte limit).
- ARCHITECTURE.md: component C1, `http.server` (ADR 0001), errors as `{"error": text}`.

## Security And Tenancy

Single tenant. The request body is untrusted: at most 1025 bytes are read, it is parsed as JSON and returned to the sender, never stored or logged. The default bind is 127.0.0.1 (NFR-001).

## Failure Modes And Recovery

| Mode | Test |
| --- | --- |
| Body is not JSON, not UTF-8, or empty (400) | `test_echo_refuses_a_body_that_is_not_json`, `..._not_utf8`, `..._an_empty_body` |
| Body over 1024 bytes (413), exactly 1024 accepted | `test_echo_refuses_a_body_over_the_limit`, `test_echo_takes_a_body_of_exactly_the_limit`, `test_oversized_body_over_http` |
| Bad Content-Length (400) | `test_bad_content_length_over_http` |
| Unknown path (404) or wrong method (405) | `test_unknown_path_is_not_found`, `test_health_refuses_other_methods`, `test_echo_refuses_other_methods`, `test_other_methods_get_a_json_answer`, `test_head_gets_405_with_headers_only` |
| Process down | not testable in a unit test; the operator restarts it (SPEC.md, Failure Modes) |

## Observability

None added. `/health` is the only signal; the access log is the next task.

## Module Placement

`src/tiny_service.py` and `src/test_tiny_service.py` only. Nothing else imports them.

## Resolved Questions

- Which port does the service default to? Open when this SPEC was accepted. The implementer asks the controller, who answers with a ruling (R1 in this example: 8080 on 127.0.0.1).

## Closes

- J1
- J2
- REQ-001
- REQ-002
- REQ-003
- REQ-004
- REQ-005
- NFR-001
