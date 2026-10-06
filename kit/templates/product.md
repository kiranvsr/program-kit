# Product Definition

*Stage 2 artifact. Personas and journeys carry IDs that later stages trace to requirements and tasks.*

## Personas

*One entry per persona with an ID (U1, U2, ...): who they are, what they want, what they know, and what they are allowed to do.*

## Journeys

*One entry per journey with an ID (J1, J2, ...). Each states four things: the trigger, the steps, the end state, and the failure paths.*

### Journey Entry Format

*Copy this block for each journey.*

- *ID and name:*
- *Persona:*
- *Trigger: what starts the journey.*
- *Steps: the ordered actions and the system's responses.*
- *End state: what is true when the journey succeeds.*
- *Failure paths: what can go wrong at each step and what the user sees and can do.*

## Product Surface Checklist

*Every item is in scope, a later release (name the release), or out (give the reason). Delete none.*

| Item | Decision | Release or reason |
| --- | --- | --- |
| Onboarding | | |
| Authentication and authorization | | |
| Administration | | |
| Billing or entitlement | | |
| Operations: deploy | | |
| Operations: backup and restore | | |
| Operations: monitoring and alerting | | |
| Data lifecycle | | |
| Documentation | | |
| Support | | |

## Release Shape

*The first release as a set of journey IDs, then each later release by name with the journeys it adds.*
