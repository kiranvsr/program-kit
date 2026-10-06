# Product Definition

*Stage 2 artifact. Personas and journeys carry IDs that later stages trace to requirements and tasks.*

## Personas

- **U1 Operator.** Wants to know in seconds whether the process is up. Checks a few times a day. Knows the command line. Works at a terminal. Main frustration: having to read logs to find out.
- **U2 Developer.** Wants to see exactly what their HTTP client sends. Uses it daily while building. Knows HTTP and JSON. Works against a local port. Main frustration: guessing what the client really sent.

## Journeys

### J1 Operator checks the service is alive (walking skeleton)

- Persona: U1.
- Trigger: the operator wants to know whether the service is up.
- Steps: the operator sends GET /health; the service answers 200 with {"status": "ok"}.
- End state: the operator knows the process is up.
- Failure paths: the connection is refused (the process is down; the operator restarts it); the operator uses the wrong method or path and gets a JSON 405 or 404.

### J2 Developer echoes a JSON message

- Persona: U2.
- Trigger: the developer wants to check what their client sends.
- Steps: the developer sends POST /echo with a JSON body; the service answers 200 with {"echo": <the JSON>}.
- End state: the developer sees the JSON the client really sent.
- Failure paths: the body is not valid JSON (400 with a JSON error); the body is over 1024 bytes (413); the wrong method or an unknown path (405, 404).

## Product Surface Checklist

| Item | Decision | Release or reason |
| --- | --- | --- |
| Onboarding | out | One command starts the service; nothing to onboard. |
| Authentication and authorization | later | Release secured. The first release binds the loopback address instead. |
| Administration | out | No settings and no users to administer. |
| Billing or entitlement | out | Internal tool, no charge. |
| Operations: deploy | first | `python3 src/tiny_service.py` on a developer machine. |
| Operations: backup and restore | out | The service keeps no data. |
| Operations: monitoring and alerting | first | `/health` and one access log line per request. No alerting: nothing runs unattended. |
| Data lifecycle | out | The service keeps no data. |
| Documentation | first | The README of the repository says how to start it and shows both endpoints. |
| Support | out | The service owner answers within one working day; no support process. |

## Release Shape

A single first release made of J1 and J2, with the service owner as the pilot user. The later release, named secured, adds authentication and TLS. It starts when the service is to run anywhere other than a developer machine.
