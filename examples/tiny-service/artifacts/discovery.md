# Discovery

*Stage 1 artifact. Facts and decisions from the discovery interview; every statement names its source in the ledger.*

## Purpose

An operator can tell that a service process is alive, and a developer can see exactly what their HTTP client sends. tiny-service is a small internal HTTP service with two endpoints: `/health` and `/echo` (ledger F-1, source S1). No measure has a target yet; the service owner reviews that on 2026-12-01. Without it every team keeps writing its own throwaway health script.

## Users And Stakeholders

The operator (checks health) and the developer (echoes requests). No other system depends on the service. The service owner decides, accepts and pays; there is no separate budget. If needs conflict, the operator wins (F-2).

## Existing Landscape

Greenfield. No existing system, so no owners or health notes, nothing that must stay unchanged, and no prior documents (F-3).

## Scope Edges

Out: authentication, TLS, stored data, a metrics endpoint, containers or packaging. Reduced first release: the two endpoints on the loopback address. Deferred, with no date: authentication and TLS, until the service leaves a developer machine (F-4).

## Constraints

No date, no budget beyond one person's time. Mandated: Python 3.11 or later, standard library only. Forbidden: third-party packages. No organisational constraints (F-5).

## Environments

`dev` exists (a developer machine). `staging` does not exist and none is planned, so it is declared out and nothing is claimed for it. The matrix is in `environments.yaml` (F-6, answering Q-1).

## Team And Ways Of Working

One implementer agent per task and a separate QA agent; the service owner merges. No on-call. The kit's rules and unittest are the conventions. Response time: one working day (F-7).

## Risks And Unknowns

Top risk: the service is run on a non-loopback address, where it has no authentication. No prior failures, no spike. One assumption, that every developer machine has Python 3.11 or later, is verified by `pk doctor` and confirmed by the service owner (F-8, A-1).

## Regulatory

None apply. The service stores no data, has no outside customers and no regulated use, so the regulatory topic does not apply and no entry carries a regulatory tag.
