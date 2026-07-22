# SPEC V1.3 Execution Checklist

Authority: `FJ_Final_Cut_Review_SPEC_V1.3_Reviewed.md`.
Legacy repository documents are non-authoritative when they conflict with this SPEC.

This checklist separates SPEC/document alignment, historical implementation evidence,
and remaining design-delivery evidence. Historical evidence was preserved in the local
pre-clean Git backup and is intentionally excluded from the public source tree; checked
items below are not a fresh attestation for this rewritten commit.

## Inputs

- [x] Use repository-local `FJ_Final_Cut_Review_SPEC_V1.3_Reviewed.md` as the only baseline.
- [x] Keep project copy `FJ_Final_Cut_Review_SPEC_V1.3_Reviewed.md` synchronized with the baseline file.
- [x] Keep historical task context in the local pre-clean Git backup, not in the public source tree.

## Documentation Alignment Covered

- [x] Product boundary: no login, user, member, role, task center, notification center, delivery center, download center, mobile layout, AI, delete, or revoke-finalization feature.
- [x] Entry model: standalone roots are `/edit` and `/review`; child routes are implementation details and cannot change the entry capability model.
- [x] Capability split: `/edit` owns project read/create/update/archive/restore, item read/create/update, version read/upload/compare, issue read, finalization read, and finalized-original download; `/review` owns project/item/version read, version compare, review issues, request changes, finalization, finalized-original download, and project package.
- [x] Access control: server-created `ExecutionContext`, `EntryPolicyPort`, `WriteGuardPort`, `PrincipalAuthorizationPort`, and intersection authorization.
- [x] Project catalog abstraction: `ProjectCatalogPort`, local adapter now, host adapter later, and derived project completion status.
- [x] Unified contract source: OpenAPI, capabilities, errors, commands, queries, events, module manifest, generated DTO/client/server schemas.
- [x] Domain model: review item, version, issue, revision, annotation set, thread message, decision, finalization record, and package snapshot.
- [x] State machine: pending review, in review, changes requested, finalized, first issue implicit start, append version, request changes, finalization, and finalized write rejection.
- [x] Version isolation: new versions do not inherit, copy, map, or auto-track old issues and marks.
- [x] File/media/package: original file, playback proxy, media readiness, upload sessions, finalized original download, package snapshot consistency, ZIP naming, and no package history/download center.
- [x] HTTP/API: shared read API, edit write facade, review write facade, upload API, no DELETE routes, headers, idempotency, optimistic lock, transactions, errors, events, and Outbox.
- [x] Host integration: module manifest, `ReviewHostBridge`, embedded rendering rules, host catalog/auth/http/event/file/portal/theme injection.
- [x] Frontend design: shared pages/components, CapabilityGate as UI-only gate, query keys with ownership IDs, context switching cleanup, style isolation, responsive layout, accessibility.
- [x] SPEC Chapter 40 precise annotation playback: `ReviewPlaybackTarget`, rational FPS, media event sequence, AnnotationSet filtering, race handling, UI states, auto pause, 17 acceptance criteria, and required tests.

## Historical Implementation Evidence (not current release proof)

- [x] `npm ci` completed after clearing stale generated `node_modules`; only Node engine/TLS environment warnings remained.
- [x] `npm run contracts:generate`.
- [x] `npm run contracts:check`.
- [x] `npm run typecheck`.
- [x] `npm run lint`.
- [x] `npm run test`.
- [x] `npm run test:e2e`.
- [x] `npm run build`.
- [x] Browser smoke on local dev server.
- [x] Screenshot/DOM evidence for 1920 and 1366 precise playback replay.
- [x] Final T6 review JSON.
- [x] `review_gate.py` pass for final T6 implementation scope.

## Precise Playback Code Evidence Required

- [x] Issue card click emits full `ReviewPlaybackTarget`.
- [x] Issue timecode click emits full `ReviewPlaybackTarget`.
- [x] Timeline marker click uses the same playback flow.
- [x] Previous/next issue navigation is scoped to the current version and sorted by `timestampMs + issueNo`.
- [x] Historical-version issue click switches to target `versionId` before seek.
- [x] Playback waits for `loadedmetadata`, `canplay`, `seeked`, and `requestVideoFrameCallback` where available.
- [x] Playback pauses at target frame.
- [x] Only selected Issue current Revision AnnotationSet is shown.
- [x] Other issues, other versions, and old Revisions are hidden after precise playback.
- [x] Stale playback request cancellation prevents old callbacks from winning.
- [x] Draft annotations and selected AnnotationSet clear on project/item/version switch.
- [x] Auto pause applies only to current-version unresolved issues during natural forward playback.
- [x] Coordinate helpers use actual contained video bounds, not black bars.
- [x] Variable-frame-rate media is documented as review timeline precision only, not source encoded PTS precision.

## Remaining Design-Delivery Evidence Gap

- [ ] Figma refresh and screenshot QA for SPEC Chapter 40 states. [`DESIGN-DELIVERY.md`](../design/DESIGN-DELIVERY.md) remains a partial inventory until this visual-design evidence exists.

## Known Limits

- [x] SMPTE Drop Frame is intentionally unsupported in MVP.
- [x] Embedded formal backend/account/canvas integrations are represented by ports/adapters until live integrations exist.
