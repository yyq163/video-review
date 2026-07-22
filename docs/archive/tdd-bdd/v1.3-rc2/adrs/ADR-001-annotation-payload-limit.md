# ADR-001: Annotation Payload Limit (1 MiB)

- **status**: proposed
- **normative**: false
- **date**: 2026-06-21

## Context
SPEC §10.6 defines the AnnotationSet shape schema but does not specify a payload size limit.

## Decision
Propose a 1 MiB payload limit as a defensive engineering guard.

## Compatibility Impact
None on contract V1; consumers remain unknown-safe.

## Contract Impact
None. Not registered in errors.yaml as a V1 domain error code.

## Note
This is a non-normative engineering extension. It must not be counted as SPEC coverage or release acceptance. Removed from the normative BDD; BDD-ANN-026 asserts it is non-normative only.
