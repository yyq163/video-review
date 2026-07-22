# DEV PLAN

Source of truth: [`FJ_Final_Cut_Review_SPEC_V1.3_Reviewed.md`](../product/FJ_Final_Cut_Review_SPEC_V1.3_Reviewed.md). [`Product-Spec.md`](../product/Product-Spec.md) remains the operational product summary, including the 22-item UI acceptance scope.

The numbered Browser acceptance matrix is defined in [`Product-Spec.md`](../product/Product-Spec.md) under `22 项可见 Browser 验收基线`; final evidence must preserve those IDs and attach archive/restore, restart persistence, physical-delete storage binding, post-review backend rejection and soft-delete audit as supplemental gates.

## Task Order

1. Freeze Contract V1 artifacts: OpenAPI, capability registry, command schema, query DTOs, errors, events, module manifest, and generated TypeScript DTO/client.
2. Implement frontend module shell under `src/modules/final-cut-review/`, including generated contracts, capability registry, entry profiles, route context, timecode, coordinates, playback target validation, query adapter, edit command adapter, review command adapter, standalone host, embedded host, and host context.
3. Implement shared routes and pages for `/edit` and `/review`: `ProjectListPage`, `ProjectDetailPage`, `ReviewItemPage`, and `ReviewWorkspacePage`. Do not duplicate pages by entry.
4. Implement `CapabilityGate` and entry profiles. Edit entry exposes project read/create/update, item read/create/update, version read/upload/compare, issue read, finalization read, and finalized original download. Review entry exposes project read/archive/restore/delete, item/version read, version compare, issue create/update/reply/resolve/reopen/delete, start review, request changes, finalization, finalized original download, and package create/read/download capabilities. All gates remain UI-only; server checks remain authoritative.
5. Implement shared read API usage and thin write facades. Reads call `/api/v1/final-cut-review/...`; edit writes call `/api/v1/final-cut-review/edit/...`; review writes call `/api/v1/final-cut-review/review/...`; uploads call `/api/v1/files/uploads/...`.
6. Implement style isolation, theme variables, responsive workbench layout, accessible icon/keyboard behavior, and embedded `portalRoot` support.
7. Implement edit flow: create/update project, create/update item, upload V1, append versions, read-only issues/replies/annotations, version compare, finalization read, finalized original download.
8. Implement review flow: archive/restore/soft-delete project, start review, create/update/delete issue, annotation snapshot, reply, resolve/reopen, request changes, finalization, finalized original download, finalized project package create/read/download.
9. Implement precise annotation playback: full `ReviewPlaybackTarget`, version switching, media event sequencing, frame-derived seek, AnnotationSet filtering, issue/timeline highlight, race cancellation, and auto pause integration.
10. Implement unit, component, E2E, accessibility, and browser smoke coverage listed below.
11. Run install if needed, then typecheck, lint, unit tests, component tests, E2E, build, and browser smoke.
12. Produce current-run release evidence outside the public source tree and complete the applicable independent review before marking a task complete.

## Non-Goals

- No login, account, member, role, notification center, task center, delivery center, download center, mobile layout, AI, general-purpose physical delete, revoke-finalization, or cross-version automatic issue tracking UI. The only physical-delete exception is the explicit edit-entry cleanup of a duplicate item before review starts.
- No duplicate business implementation between `/edit` and `/review`.
- No frontend-submitted trusted `ExecutionContext`, capability, principal, role, admin flag, or write guard state.

## Core Acceptance

- `/edit` and `/review` are the only standalone entry roots.
- `/edit` has no review write buttons and no project archive/restore/delete buttons.
- `/review` has no project creation, project editing, item creation/editing, V1 upload, or version append buttons; project archive/restore/soft-delete are review-entry actions.
- Both entries share the same pages, components, query adapter, and read API.
- Two write facades call the same backend command handlers through entry-specific routes.
- CapabilityGate controls only UI experience; backend policies remain authoritative.
- No DELETE endpoint is registered or called by frontend code.
- Query keys always include stable ownership IDs and never rely only on `versionNo`, `itemCode`, `issueId`, filename, timecode text, or array index.
- Project/item/version switches clear old media, issue lists, drawings, selected AnnotationSet, uploads, requests, timecode, and stale callbacks.
- Host embedded mode does not render the standalone global top bar, fills the host container, accepts host catalog/auth/http/event/file/portal/theme services, and recalculates CapabilityGate on host permission changes.
- CSS is isolated to `.fj-review-root`, `.fj-review-*`, and `--fj-review-*`; global element resets are forbidden.
- Player uses real `HTMLVideoElement`, `object-fit: contain`, rational FPS timecode, frame stepping, timecode input, volume, speeds, fit/original ratio, fullscreen, and SPEC shortcuts.
- Annotation coordinates are normalized to the actual contained video rectangle, not black bars, and render correctly with DPR scaling.
- Issues, revisions, messages, statuses, annotations, finalizations, downloads, and packages never cross project/item/version boundaries.
- V2 does not overwrite V1 and does not inherit V1 issues or annotations.
- V1 unresolved issues do not block V2 finalization.
- Finalization and download use frozen finalization records and original files, not player proxy URLs.
- Project package includes only the current project active finalization original files and does not drift after snapshot creation.

## SPEC 40 Precise Playback Acceptance

1. Current V2 issue click stops on V2 target `timestampMs/frameNumber`.
2. V1 historical issue click from V2 switches to V1 before seeking.
3. Playback is paused after precise playback.
4. Displayed frame equals target `frameNumber` within no more than one review frame browser seek tolerance.
5. Only the selected issue `currentRevisionId` AnnotationSet is shown.
6. Other issue marks are hidden.
7. Other version marks are hidden.
8. Old Revision marks are hidden.
9. Multiple issues at the same timecode highlight only the selected issue.
10. Rapid #001 -> #002 -> #003 clicks end only on #003.
11. Old seek/media-load/request returns cannot override the latest selection.
12. 1920 and 1366 layouts keep the same annotation position relative to the video image.
13. Left/right black bar changes do not shift annotations.
14. Fullscreen restores annotations correctly.
15. Version switching clears the previous version temporary drawing and selected AnnotationSet.
16. After issue edit, default playback uses current Revision, not an old Revision.
17. Variable frame rate media promises review timeline frame precision only, not source encoded PTS precision.

## SPEC 40 Required Tests

Unit tests:

- `frameFromTimestampMs`
- `timestampMsFromFrame`
- `formatReviewTimecode`
- `computeContainedVideoRect`
- `pointerToNormalizedVideoPoint`
- `normalizedVideoPointToCanvasPoint`
- `ReviewPlaybackTarget` validation
- frame rates 25/1, 24/1, 30/1, 24000/1001, 30000/1001
- 9:16 video in 16:9 container
- 16:9 video in 16:9 container
- left/right black bars
- top/bottom black bars
- DPR 1 and 2
- pointer in black bars

Component tests:

- IssueCard click emits `ReviewPlaybackTarget`
- timecode click emits `ReviewPlaybackTarget`
- timecode button supports keyboard activation
- Timeline Marker click uses the same playback flow
- current card highlight
- historical version issue read-only display

E2E tests:

- current-version precise playback
- historical-version switch then playback
- consecutive-click race
- 1920 and 1366 coordinate replay
- V1 marks absent from V2
- V1 unresolved issue does not block V2 finalization
- current-version unresolved auto pause

## Verification Commands

```bash
npm run typecheck
npm run lint
npm run test
npm run test:e2e
npm run build
```

Browser smoke must run against the local dev server and capture current-version playback, historical switch/playback, rapid-click race, selected AnnotationSet filtering, V1/V2 mark isolation, and 1920/1366 overlay placement.

## 2026-07-09 UI Follow-up Execution Addendum

- Add `SoftDeleteReviewIssue` through contract source, generated backend/TS contract artifacts, review HTTP route, repository command handler, SQLAlchemy model, Alembic migration, mock adapter and frontend query mutation.
- Add `DeleteReviewItem` through contract source, generated backend/TS contract artifacts, edit HTTP route, repository command handler, mock/in-memory adapters and frontend query mutation. It is limited to pre-review duplicate cleanup only: edit entry, explicit `confirmed: true`, `pending_review`, one version, no issues, no finalization records, no active finalization. Review-started items are hidden/disabled in frontend and rejected by backend. The backend deletes the item, the single unreviewed version, and the unreferenced storage-root-contained file object/blob while preserving outbox and operation log records. It deletes a linked upload session only after part cleanup is confirmed; otherwise it detaches `file_id` and preserves cleanup references and quota for maintenance.
- Extend tests for current-version issue deletion, historical issue read-only behavior, soft-delete persistence, project/item count filtering, Browser QA delete flow, player-centered toast, episode de-duplication, issue-panel scrolling and version-compare equal-height fit.
- Extend visual layout coverage for the non-full-width centered workstation: topbar inner content and workspace frame must share a fluid max-width token, ultrawide viewports must not stretch the workstation to the full browser width, and append V2 must keep its full-height action rail.
- Verification commands for this addendum remain: `npm run typecheck`, `npm run lint`, `npm test -- --run`, `npm run build`, `npm run contracts:check`, `npm run test:e2e`, `git diff --check`, and `backend/.venv/bin/pytest backend/tests -q`.
- PostgreSQL constraint evidence uses
  `backend/scripts/postgres_constraint_gate.py` as a non-green environment
  gate. Missing PostgreSQL configuration must produce `BLOCKED_TEST_ENV`
  without printing the database URL; it is never folded into a PASS status.

## 2026-07-13 PostgreSQL / Maintenance Delivery Addendum

- Run backend, a dedicated maintenance service, and a dedicated single-concurrency package worker from the same immutable `sha256` image reference through `docker-compose.delivery.yml` at fixed UID/GID `10001`; the base Compose tag is local-build-only. Serialize entrypoint ownership checks with a root-only lock, use random exclusive state temporaries, and perform migration once per data-volume identity. Record state in an application-user-read-only runtime-state volume; controlled restores and out-of-band imports must explicitly force one revalidation before returning the flag to its default.
- Construct the delivery image as `repository@sha256:<64-lowercase-hex>` from separate validated fields and reject a tag in the repository field before Compose. Commit package `preparing` before returning 202, build only through the recoverable worker under a database-global lock, use bounded delayed retries that cannot starve newer rows, enforce per-project/global queue and estimated-then-actual ZIP volume quotas, and keep failed-package reservations until physical cleanup is confirmed. Migration `20260713_0011` conservatively backfills legacy failed reservations. Use one-shot download sessions plus a single active lease, bound every cleanup scan/batch with forward progress, validate package-ID/path binding before cleanup, and use startup full-storage preflight plus bounded `/runtimez` for frequent health checks.
- Enforce one storage root per database. The backend lifespan must hold a database-scoped session advisory writer lock on one dedicated physical PostgreSQL connection, reject a second runtime before serving, verify lock identity during readiness, and unlock on the same connection; only explicit SQLite tests may bypass it. Resolve every managed-root component with directory FDs and `O_NOFOLLOW`, with a second identity pass to reject intermediate symlinks or replacement. Readiness must validate canonical path containment and regular-file existence for all file objects and active package associations. Any host-to-Compose cutover copies and hashes files before a transactional path update, preserves sources, and proves restart/down-up association stability.
- Keep PostgreSQL 16, application data, and test data on explicit stable engine-level named volumes with distinct admin, non-superuser owner/migrator, and minimum-DML runtime roles. The one-shot migration service must migrate and grant both application and disposable test databases before runtime startup; the three long-running application services receive only runtime credentials. Runtime may read but never mutate `alembic_version`, and sequences grant `USAGE, SELECT` without `UPDATE/setval`. Enforce health-based startup ordering, `restart: unless-stopped`, read-only application root filesystems, `/tmp` tmpfs, and `no-new-privileges`.
- Isolate cleanup failures per upload part, package, and pending-delete tombstone. Reject tombstone directory/file symlinks, malformed/oversized payloads and storage-root escapes. Preserve failed work for retry while allowing unrelated cleanup to complete.
- Serialize upload writes and stale-session cleanup with PostgreSQL row locks: each PUT uses a server-random `O_EXCL` candidate independent of client Request-ID and commits metadata before deleting a superseded part. Compensate only a definite rollback; reconcile ambiguous commits through a fresh database session, preserving committed or unobservable candidates. Maintenance must atomically claim with `FOR UPDATE SKIP LOCKED`, commit `aborted`, then remove referenced parts and independently reclaim TTL-expired database-unreferenced upload candidates, final media and packages. Persist physical-delete tombstones across ambiguous delete outcomes with atomic rename plus file/directory `fsync`; use separate bounded application and Alembic statement timeouts.
- Before a PUT body, use an independent short unlocked session to validate upload identity/owner/status and close it; after bounded dedicated-executor read/write/flush/fsync under one total timeout, repeat validation under the upload row lock. Preserve the deployment-lowered part-number precheck before candidate creation. Keep process-local 16/1/64 admission valid by forcing the current Compose delivery to one Uvicorn worker and one backend replica; require external coordination before horizontal scaling. Permit only one PUT candidate per upload id to prevent same-session retry races, while verifying at least 10 simultaneous PUT admissions for 10 distinct upload ids under the shared LAN principal. The four-worker I/O executor may serialize disk work safely without reducing admission capacity.
- Serialize upload init quota decisions with a PostgreSQL transaction advisory lock. Persist global/principal active-session accounting and reserve twice declared bytes for the parts-plus-final-staging peak; count terminal rows until physical part cleanup is confirmed and enforce the same peak against a filesystem low-water mark. Before PUT body I/O, use a short committed row-lock transaction to renew activity and compute the remaining declared-byte cap, reject known oversized Content-Length before candidate creation, and require upload TTL to exceed body timeout plus safety margin. Commit a short finalizing lease before complete I/O, close the database session during concatenate/hash/ffprobe/fsync, then publish under a new short lease-validating transaction. Recover expired leases idempotently, reject superseded publishers, include active finalization/published IDs in orphan references, and release upload quota only in a post-cleanup confirmation transaction. Retry failed completed/aborted cleanup after bounded backoff and move a TTL-stale finalizing row to abort cleanup only after its lease expires.
- Commit upload complete/abort state before deleting any referenced part, publish final files with no-replace/no-follow semantics and durability barriers, and recheck every orphan candidate under its owning upload row lock immediately before unlink. Read and delete managed files through pinned descriptors that reject directory or leaf symlink substitution.
- Persist a hash of the completion idempotency key and canonical request with the finalization lease. Expired-lease takeover is allowed only for the same pair; any different key/request fails with `IDEMPOTENCY_CONFLICT`. Migration `20260714_0013` resets unverifiable legacy `finalizing` rows to recoverable receiving state and conservatively backfills every unreclaimed legacy package reservation from the larger of recorded storage and original bytes plus overhead.
- Persist project descriptions in their own non-unique `project_refs.description` column. Migration `20260714_0014` backfills legacy rows without repurposing `external_project_id`; local UI project creation sends `description` and leaves host identity unset.
- Make PostgreSQL idempotency reservation an atomic conflict race. Identical concurrent losers re-read and replay the winner after commit; principal, command-type, or request-hash mismatches remain hard conflicts. Cover shared reservation behavior with real PostgreSQL barrier tests for item creation and finalization command identities.
- Keep stable mounted-page operation identity for V1 creation and V2/V3 append so lost upload/bind responses reuse the completed upload and command id instead of creating duplicate items or versions. Use Safari-compatible XHR only for binary PUT byte progress, preserve fetch/idempotency for init and complete, use bounded dynamic parts, and reuse the same in-memory operation when the same file metadata is reselected. Add real ISO-BMFF/ffprobe compatibility coverage and a separate real-stack Playwright profile that requires an explicit frontend base URL and a video from the authorized `test-video` directory, but continue to treat automation as supplementary to visible Safari/Chrome QA.
- Treat archive deletion, finalization confirmation and large downloads as explicit cross-layer contracts: archived projects cannot be deleted until restored; finalization requires an irreversible native confirmation plus backend `confirmed: true`; HTTP originals and ZIPs use browser-native streaming and same-descriptor digest verification; package preparation and download are separate UI states with bounded polling, expiring in-memory authorization, token-free idempotency persistence, and a header-to-HttpOnly-cookie download session. ZIP streaming renews the exact lease identity until close/error, aborts on renewal failure, then stops heartbeat before identity-bound release.
- Make backup/restore smoke create cryptographically random disposable source and restore databases, preflight collisions, track per-resource creation ownership, and clean only resources created by that run. Fail closed on database identity and prove an application-role business sentinel plus `file_objects`/`upload_sessions` rows and a real canonical data-volume blob survive restore and backend readiness; schema-only restore evidence is insufficient.
- Bound database connect/statement time, cleanup cycle time and retry count. Refresh heartbeat during long idle intervals, require both a fresh heartbeat and status `ok`, mark `degraded/error` unhealthy, and exit after repeated cycle errors so Compose can restart the service. Trusted proxy download-session requests without a valid forwarded scheme must fail closed.
- Verification requires Ruff, full app-and-tests mypy, all backend tests, PostgreSQL hard gate with no skips, same-Request-ID concurrent PUT and commit-failure compensation tests, Alembic current/head plus metadata drift, Compose build/up/health/restart/down-up persistence with deterministic full-row digests for all business/audit tables and file-association counts, backup/restore, non-root/security inspection, and adversarial cleanup probes.
- Final evidence commands must run from an accepted clean source commit through the fixed runner. The runner passes only an allowlisted environment, derives counts from output, writes fixed non-overwriting artifacts and signs a source/tree/argv/output-bound HMAC receipt using a mode-0600 key below `.git/review-gate/`. The gate rejects nonce replay, source mutation, ledger prefix rewrites, unsigned/manual command envelopes, unsafe attestation writes, raw secrets/URLs, blank or malformed screenshots, and Browser manifests without exact visible Google Chrome + Computer Use provenance.
