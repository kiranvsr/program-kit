# ADR 0001: Serve HTTP with the standard library

*Status: accepted.*

## Context

REQ-001 to REQ-005 need two small endpoints. Third-party packages are forbidden (ledger F-5) and there is no budget for a framework.

## Options

- **http.server.** In the standard library. Threaded server, one handler class. Weak point: it is not hardened for the open internet.
- **wsgiref.** In the standard library. Needs an application function plus a server; more moving parts for two endpoints.
- **A third-party framework.** Best features, but it breaks the "standard library only" constraint and adds something to keep patched.
- **Do nothing.** Every team keeps its own throwaway script, which is the problem this service solves.

## Decision

Use `http.server` with `ThreadingHTTPServer`. Chosen by the user on 2026-10-06 (D-8).

## Consequences

Easier: no dependency, so no licence to check and nothing to patch; one file. Harder: no routing, authentication or TLS helpers; the service must stay on the loopback address until the later release named secured. Accepted cost: the module is not suitable for the open internet.

## Reversibility

Easy. `handle(method, path, body)` does not depend on the server, so moving to another server is a change to the handler class only. The signal to revisit it is the secured release.
