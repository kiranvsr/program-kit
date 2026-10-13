# Contract: ID and title

*Written first, before either side is built, and committed to main by the controller in the dispatch commit, so that every worktree sees it. The client side builds against this file, not against the server's code, so both sides proceed in parallel. A branch never edits it: the gates hold it identical to main's. A change is appended on main, with its reason.*

## Operations

*Each operation: name, method and path or message name, who may call it, request fields, response fields, and every error with its code.*

## Data shapes

*The types the operations exchange, field by field, with which fields are required and what an absent one means.*

## Examples

*One request and response per operation, literal, that both sides use as test fixtures.*

## Changes

*Appended, never edited in: date, what changed, why, and which side asked. An entry here is the signal for the other side to re-read.*
