# Architecture

*Stage 4 artifact. Frozen at approval 5; after that it changes only through change requests.*

## Candidate Approaches And Choice

*Two or three candidate architectures with their trade-offs, the recommendation, and the option the user chose with the reason.*

## Components

*One entry per component with an ID (C1, C2, ...): its responsibility, its boundary, the requirement IDs it owns, and where it lives in the repository.*

## Data Flow

*How data and control move between components for each journey, and where state is stored.*

## Deployment Per Environment

*For each environment in `environments.yaml`: what runs there, how it gets there, and what differs from the others.*

## Cross-Cutting Concerns

*Authentication and authorization, tenancy, observability, data lifecycle, configuration and secrets: the approach for each and the components that carry it.*

## Technology Decisions

*One line per decision: the choice and a pointer to its record in `program/adr/`.*

## Quality Gates

*Each gate by name (G-...) with the claim it proves, the evidence it requires and the environment it needs. The definitions live in `gates.yaml`.*

## Delivery Mechanics

*Branching, review, merge and release practice, what the pipeline checks, and who may approve what.*

## Change Log

*Date, change request or ruling, what changed, and the IDs it touches. Empty until the first change after approval.*
