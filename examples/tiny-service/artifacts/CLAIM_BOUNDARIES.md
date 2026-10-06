# Claim boundaries

## Claimable in development

- GET /health answers 200 with {"status": "ok"}, and POST /echo returns the JSON it was sent, when the unit tests and the hand-back gate pass on a developer machine (environment dev).
- Bad input is refused with a JSON error: a body that is not JSON (400), a body over 1024 bytes (413), a Content-Length that is not a number (400), an unknown path (404), a method the path does not allow (405).
- The service binds 127.0.0.1 by default and keeps no data.
- Each of those claims is accepted for development by QA, with commit-bound evidence. That is not production readiness.

## Unclaimable until the named environment exists

- Behaviour, latency or availability under real traffic: needs staging, which does not exist and is not planned.
- Anything about the service running anywhere but a developer machine, including access from another host: needs staging.

## Never claimed

- That the service is secure outside the loopback address: it has no authentication and no TLS.
- Any latency, availability or scale figure: no target exists (owner: the service owner, review on 2026-12-01).
- That the service is production ready.
