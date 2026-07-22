# ADR-006: Issue Counter as Non-Authoritative Derived Value

- **status**: accepted
- **normative**: false
- **date**: 2026-06-21

## Context
SPEC §14.3/§17.2 define max+1 allocation for version_no and issue_no. A counter column may be used as an implementation optimization.

## Decision
If a counter column is retained, it is a non-authoritative derived value. SPEC max+1 is authoritative. A consistency constraint, repair migration, and this ADR must accompany it.

## Compatibility Impact
BDD only asserts observable unique/monotonic/consecutive results, not the counter column as product spec.

## Contract Impact
None.

## Note
Non-normative (P1-001 closure).
