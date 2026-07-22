# ADR-003: Host Cookie Hardening (__Host- / Origin Gate)

- **status**: proposed
- **normative**: false
- **date**: 2026-06-21

## Context
SPEC §4.3 requires HttpOnly, SameSite, and Secure (when HTTPS). It does not mandate `__Host-` prefix, Origin Gate, always-Secure, or Retry-After.

## Decision
Propose `__Host-` prefix, Origin Gate, cross-site disable, and Retry-After as a deployment hardening profile only.

## Compatibility Impact
HTTPS and HTTP both remain valid V1 behaviors per SPEC. This ADR must not be written as the V1唯一结果.

## Contract Impact
None on contract V1.

## Note
Non-normative. Removed from normative BDD (P1-018 closure).
