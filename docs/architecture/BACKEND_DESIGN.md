# BACKEND DESIGN

Baseline source: `FJ_Final_Cut_Review_SPEC_V1.3_Reviewed.md`.
The current runnable delivery can use mock adapters, but backend-facing design must match SPEC V1.3 exactly so formal adapters can replace mocks without changing UI, domain rules, or contracts.

## Module Layout

Required backend module boundary:

```text
src/modules/
├── review_contracts/
├── project_catalog/
├── final_cut_review/
│   ├── domain/
│   ├── application/
│   ├── infra/
│   └── tests/
├── review_access/
├── review_media/
├── finalized_package/
├── review_integration/
└── review_http/
```

`final_cut_review/domain` contains aggregates, entities, enums, command types, event types, invariants, and domain errors only. Domain tests use fake ports and do not touch database, network, filesystem, framework objects, or current time.

`final_cut_review/application` contains command handlers, query services, repository/port orchestration, transaction boundaries, idempotency handling, optimistic-lock checks, and outbox writes.

Concrete database, storage, HTTP, host, media, and package integrations are adapters behind ports.

## Required Ports

At minimum:

```text
ProjectCatalogPort
PrincipalAuthorizationPort
EntryPolicyPort
WriteGuardPort
ReviewRepositoryPort
FileStoragePort
MediaPort
FinalizedPackagePort
EventOutboxPort
OperationLogPort
ClockPort
IdGeneratorPort
TransactionManagerPort
```

Adapter replacement must not change domain models, command payloads, query payloads, state machines, API business DTOs, player components, annotation components, file reference model, or event structure.

## Execution Context

The backend creates `ExecutionContext`; clients never send trusted security context.

Fields include request/correlation/causation IDs, `entrySource`, principal kind/id, write guard mode/verified flag, client IP/user-agent, and optional host project/module IDs.

Forbidden client body/header fields:

```text
capabilities
permissions
roles
is_admin
is_reviewer
security_context
write_guard_verified
principal_id
```

Write request authorization is:

```text
route facade entry source
-> principal resolution
-> write guard
-> fixed route capability
-> entry policy
-> principal authorization
-> full ancestry lookup
-> domain state machine
-> command execution
```

The decision is an intersection. No layer can grant a capability denied by another layer.

## Project Catalog

Review core stores only `project_ref_id`. Local project details and future host project details are behind `ProjectCatalogPort`.

Current adapter: `LocalReviewProjectCatalogAdapter`.
Future host adapter: `CanvasProjectCatalogAdapter`.

`ProjectCatalogPort.getFeatures()` exposes `canCreate`, `canUpdate`, `canArchive`, and `canRestore`. Unsupported operations return `PORT_OPERATION_NOT_SUPPORTED`.

Project status design:

- Persisted lifecycle: `active`, `archived`.
- Derived completion: `empty`, `in_progress`, `completed`.

Archived projects are read-only and restorable.

## Domain Persistence Model

Core records:

- `project_refs(id, project_code, project_name, description, source, local_project_id?, external_project_id?, created_at)`
- `review_items`: project ref, item code, episode, title, workflow status, current version, active finalization, lock version.
- `review_versions`: project ref, item, previous version, version number/label, current flag, original media snapshot, playback/thumbnail asset IDs, notes, lock version.
- `review_issues`: project ref, item, version, issue number, status, current revision, timestamp, frame number, lock version.
- `issue_revisions`: immutable issue content/annotation revision.
- `annotation_sets`: immutable shapes tied to exact issue/version/frame and video/canvas dimensions.
- `thread_messages`: message content tied to exact issue/version. V1 does not store personal display names.
- `review_decisions`: `changes_requested` only.
- `finalizations`: active finalization freezes target version and original media snapshot. V1 does not expose supersede.
- `final_cut_package_snapshots`: package state and frozen package entries owned by `finalized-package`.

Original media snapshot persists:

```text
original_file_id
original_filename
mime_type
file_size
sha256
duration_ms
width
height
fps_num
fps_den
media_probe_version
```

Frame rate is rational. Do not persist only float FPS.

## Database Constraints

Required unique constraints:

```text
UNIQUE(source, external_project_id) where external_project_id is not null
UNIQUE(local_project_id) where local_project_id is not null
UNIQUE(project_code) for local project catalog
UNIQUE(project_ref_id, item_code)
UNIQUE(review_item_id, version_no)
UNIQUE(review_item_id, issue_no)
UNIQUE(issue_id, revision_no)
UNIQUE(original_file_id)
UNIQUE(review_item_id) where is_current = true
UNIQUE(review_item_id) where finalization_status = 'active'
```

`description` is ordinary local project metadata and is not unique. `external_project_id` is reserved for host identity and must never be populated from the UI project-description field. Migration `20260714_0014` adds the dedicated non-null description column, backfills legacy rows with an empty string, and removes the temporary server default after upgrade.

Required composite ownership constraints:

- `review_items`: `(id, project_ref_id)`
- `review_versions`: `(id, project_ref_id, review_item_id)` and `(id, project_ref_id, review_item_id, original_file_id)`
- `review_issues`: `(id, project_ref_id, review_item_id, version_id)`
- `issue_revisions`, `annotation_sets`, and `thread_messages` reference issues by composite ancestry.
- `review_issues.current_revision_id` references `(issue_id, revision_id)` with application-generated IDs and deferrable constraints for first revision.
- `finalizations` reference version by `(version_id, project_ref_id, review_item_id, original_file_id)`.

Business FKs use RESTRICT by default. The only physical business delete command is `DeleteReviewItem`, limited to pre-review duplicate cleanup with explicit `confirmed: true`, `pending_review`, one version, no issues, no finalization records, and no active finalization. The command deletes the item/version rows and the unreferenced file object plus storage-root-contained blob; outbox and operation logs remain intact with item/version pointers detached before deletion. A linked upload session is deleted only when its part cleanup is confirmed and it has no remaining part references. Otherwise deletion detaches `file_id` but preserves part references, cleanup state and reservation. Maintenance atomically deletes the detached completed session only after every referenced part is removed or confirmed absent; any cleanup failure retains the row, remaining references and reservation for retry. A completed session that still has `file_id` is never deleted by maintenance and instead records cleanup confirmation after its parts are gone.

## Commands

Supported command types:

```text
CreateProject
UpdateProject
ArchiveProject
RestoreProject
SoftDeleteProject
CreateReviewItem
UpdateReviewItem
DeleteReviewItem
UploadReviewVersion
StartReview
CreateReviewIssue
UpdateReviewIssue
AddReviewMessage
ResolveReviewIssue
ReopenReviewIssue
SoftDeleteReviewIssue
RequestChanges
FinalizeVersion
PrepareFinalizedPackage
```

`CommandEnvelope` contains `commandId`, `commandType`, `contractVersion`, optional `expectedAggregateVersion`, and payload. `ExecutionContext` is passed separately by the server.

HTTP `Idempotency-Key` must match `command_id` or be gateway-mapped. The endpoint fixes `command_type`; if a client-submitted type conflicts with the route, reject it.

Required idempotency keys:

- Create project.
- Create review item.
- Complete upload.
- Upload version.
- Create issue.
- Request changes.
- Finalize.
- Create package snapshot.

Same key plus same body returns the original result. Same key plus different body returns `IDEMPOTENCY_CONFLICT`. PostgreSQL reservation uses one `INSERT ... ON CONFLICT DO NOTHING RETURNING` race: a losing identical request waits for the winning transaction, re-reads the committed record in a new statement snapshot, validates principal, command type, and request hash, then replays the completed response rather than reporting a false conflict.

## State Machine Enforcement

Review workflow states:

```text
pending_review
in_review
changes_requested
finalized
```

Allowed transitions:

- Create V1 -> `pending_review`.
- `pending_review -> in_review` through `StartReviewCommand` or same-transaction first issue creation, with playback ready.
- `in_review -> changes_requested` when current version has unresolved issues and note exists.
- `changes_requested -> pending_review` after successful version upload.
- `pending_review -> finalized` or `in_review -> finalized` when current version has no unresolved issues, playback is ready, original is available, and hash matches.

Blocked:

- GET, playing, seeking, and version switching cannot change status.
- `in_review` cannot upload a new version.
- `finalized` cannot produce any write.
- Finalization checks current version only.
- Historical unresolved issues do not block current finalization.

Issue status transitions are explicit `ResolveReviewIssue` and `ReopenReviewIssue` commands only, and only for `/review`.

## File And Media Design

`FileStoragePort` owns upload session creation, upload completion, stream/download descriptors, and hash verification.

`MediaPort` owns probe, playback asset generation, and playback URL resolution.

Upload requirements:

- Multipart and resumable upload.
- Progress and retry.
- Page-leave protection.
- MIME, extension, magic bytes, file size, and SHA-256 validation.
- At least 2 GB per file as configurable deployment value.
- PostgreSQL-serialized global/principal session and reserved-byte quotas, plus a filesystem low-water mark.
- One short finalizing lease transaction, connection-free concatenate/hash/probe I/O, then one short publish transaction.

Review version is created only after upload completion, hash verification, and media probe. Playback proxy may be async. `PlaybackStatus` is `processing`, `ready`, or `failed`.

`processing` or `failed` blocks start review, issue creation, request changes, and finalization.

Physical paths are never exposed through APIs, logs, DTOs, or errors.

## Finalization And Package Design

Single original download reads:

```text
review_item.active_finalization_id
-> finalization.version_id
-> finalization.original_media.original_file_id
-> FileStoragePort.download
```

It returns original uploaded media with HTTP Range support and never returns playback proxy. The response streams from a no-follow, device/inode-pinned regular-file descriptor so a symlink or leaf replacement cannot redirect a download after authorization. The HTTP frontend delegates the response to browser-native download instead of buffering multi-gigabyte media as a JavaScript `Blob`.

Project package is `/review` only and contains only current project active finalization originals.

Snapshot transaction freezes:

```text
review_item_id
version_id
original_file_id
original_filename
sha256
package_filename
```

Package POST commits and returns a durable `preparing` snapshot before any ZIP I/O. A dedicated single-concurrency package worker holds a session advisory lock on one dedicated physical PostgreSQL connection and commits a durable attempt claim plus identity-bearing delayed lease before ZIP work; business-session commits cannot release or transfer the lock, and unlock is checked on that same connection. The claim Session closes before source reads, ZIP creation, output hashing and file/directory `fsync`; a fresh short transaction row-locks the snapshot, checks the exact build lease identity and package-volume quota, then publishes readiness. Lease expiry permits takeover, but once a replacement lease commits the old worker cannot publish or identity-delete the replacement output. One preparing row per project, a bounded global queue, deterministic ready-snapshot reuse, and a package-volume byte quota prevent request-driven I/O and disk fan-out. The reservation includes a conservative ZIP overhead estimate; migration `20260713_0010` forward-corrects already-applied legacy active rows, and the completed ZIP replaces the estimate with its actual byte size under the queue lock. A damaged ready ZIP remains charged until identity-bound physical deletion and reclaim accounting both commit; a pending post-commit deletion is never subtracted during replacement admission. Timeout, unexpected exception, or process death leaves a bounded next-at retry and cannot keep a poison row immediately eligible ahead of newer work; caught failures recompute next-at from the failure-recording time so long ZIP I/O cannot consume its own retry delay. Unsafe managed paths and contract/state failures terminate immediately, while exhausted caught failures and an expired max-attempt claim fail the snapshot. If any source file is missing or hash mismatched, the whole package fails. The package output is exclusively created with no-follow semantics, every pinned source is streamed into ZIP while hashing, and the completed ZIP is hashed through that output descriptor before the file plus parent directory are `fsync`ed and readiness is published. The digest is stored on the package row. Complete original and ZIP downloads verify the expected digest through the same pinned descriptor that is streamed; partial original Range responses use the immutable published file identity and startup storage audit without a full-file pre-hash, avoiding Range I/O amplification. Temporary package defaults to 24-hour expiry and does not create download-center history. The UI exposes distinct preparing and ready states with bounded count and per-request polling timeout. A signed header token is exchanged for a path-scoped HttpOnly one-shot session cookie; the native GET atomically consumes it into one database lease per package before hashing and streaming, rejects replay/concurrency, and records completion cooldown. Package idempotency records exclude bearer tokens; valid replay signs a fresh short-lived token after rechecking package state, and the frontend clears unused authorization at the explicit server expiry.

The package download stream renews the exact database lease identity at a bounded interval while digest verification or response streaming is active. Renewal failure is fail closed and is checked during digest and before every streamed chunk. Response completion, client/send failure and setup failure stop and join the heartbeat before releasing only the matching lease; a stale heartbeat cannot renew or release a replacement lease.

## Query Design

`FinalCutReviewQueryPort` returns read DTOs, not ORM objects or aggregate roots.

Every query uses full context:

```text
project_ref_id
review_item_id when applicable
version_id when applicable
issue_id when applicable
```

Forbidden repository/query shapes:

```text
getVersion(versionId)
getIssue(issueId)
getAnnotation(annotationId)
```

Current-version statistics and historical-version statistics are separate. Historical unresolved issues must not appear in current-version finalization statistics.

## HTTP Facades

Shared read APIs are single route set. Edit writes and review writes are separate thin facades.

Facades may:

- Determine `entry_source`.
- Map route to fixed capability and command type.
- Validate request headers and envelope shape.
- Call command/query application services.

Facades must not:

- Duplicate business services.
- Duplicate repositories.
- Accept trusted capability/principal fields from client.
- Implement state transitions outside command handlers.

No DELETE endpoint is registered. Unknown DELETE returns HTTP 405.

## Transactions

Single transaction is required for:

- Create item + V1 + current version pointer.
- Upload version + current version switch.
- First issue creation + start review transition.
- Request changes + decision + status change + outbox.
- Finalize + finalization + status change + outbox.
- Create package snapshot file list.

Business data and outbox event rows are written in the same transaction.

## Events And Logs

Event envelope includes event ID/type/version, aggregate type/ID/version, sequence, project/item/version/issue/finalization/package IDs, correlation/causation IDs, metadata, and payload.

Event types:

```text
review.project.created
review.project.updated
review.project.archived
review.project.restored
review.item.created
review.version.uploaded
review.session.started
review.issue.created
review.issue.updated
review.issue.message_added
review.issue.resolved
review.issue.reopened
review.changes_requested
review.version.finalized
review.finalized_original.download_requested
review.package.requested
review.package.ready
review.package.failed
```

Operation log is separate from domain events and access logs. Its schema is:

```text
operation_logs(
  id,
  request_id,
  entry_source,
  command_type,
  capability?,
  principal_kind,
  principal_id?,
  client_ip?,
  user_agent?,
  idempotency_key_hash?,
  operation_identity_hash?,
  resource_type,
  resource_id?,
  result,
  error_code?,
  failure_stage?,
  created_at
)
```

`request_id` is correlation metadata and can be reused; it never deduplicates
commit outcomes. Current writers derive `operation_identity_hash` from the command
identity when available and bind it to command type, resource and principal. A
successful `ok` row commits with business state and holds an identity-scoped
transaction advisory lock through commit. A pre-commit rollback is recorded as
`error` in the independent audit transaction. After an attempted commit raises,
the independent transaction acquires the same identity lock and then looks for committed `ok`; only when none is
visible does it insert `unknown/COMMIT_OUTCOME_UNKNOWN`. Migration `20260714_0017`
uses a partial unique index to make concurrent unknown inserts conflict-safe and
adds the package build lease columns. Before downgrading, stop all `0017` writers;
the downgrade maps residual `unknown` rows to
`error/COMMIT_OUTCOME_UNKNOWN`, removes the unique identity index/column, and then
restores the old `ok|error` check. The mapped row remains an uncertain outcome, not
a proven command failure.

The collection boundary is the registered command-route map, not every HTTP
request. It includes command successes and identified validation, policy,
domain, constraint, and infrastructure failures. It excludes query, health,
streaming, upload-transfer, download-session, and write-guard-session traffic
unless a later contract explicitly opts that route in. Success audit rows share
the business transaction. Failure rows are written after rollback through an
independent short session so rejected attempts remain visible; failure of that
secondary write emits only a bounded diagnostic and does not replace the
original error.

The record is metadata-only. Server code derives command, capability, principal,
entry source and route resource identity. It bounds every text field and stores
only the SHA-256 digest of an accepted idempotency key. It must not store request
or command payloads, response bodies, comment/annotation content, filenames,
physical paths, raw idempotency keys, authorization headers, cookies,
write-guard values, download/package tokens, account tokens, secrets, full URLs,
query strings, or raw exception text.

Migration `20260714_0015` adds the attribution fields and backfills existing rows
with `LegacyOperation`, `anonymous`, and `request`. Migration `20260714_0016`
restores those three server defaults for already-upgraded databases so an older
application image remains write-compatible during a controlled rollback. Current
writers still provide explicit attribution. V1 defines no automatic audit-log TTL or cleanup. Operation rows
survive business deletion and follow database backup/restore retention until an
explicit operator retention procedure is approved. No HTTP read/list/export
adapter exists. Direct reads require an explicitly granted database role; the
runtime role is non-owner and has no DDL, database `CREATE`, schema `CREATE`, or
`TEMP` privilege. It may read and append operation rows for runtime processing,
but cannot update, delete, or truncate them.

## Error Contract

Backends return unified error envelopes with SPEC error codes. Parent-child mismatch returns `RESOURCE_NOT_FOUND`, not leakage details.

Representative state and infrastructure codes:

```text
VALIDATION_ERROR
ENTRY_CAPABILITY_DENIED
PRINCIPAL_PERMISSION_DENIED
WRITE_GUARD_REQUIRED
WRITE_GUARD_INVALID
RESOURCE_STATE_CONFLICT
PORT_OPERATION_NOT_SUPPORTED
PLAYBACK_NOT_READY
VERSION_NOT_CURRENT
REVIEW_IN_PROGRESS
REVIEW_ITEM_FINALIZED
UNRESOLVED_ISSUES_EXIST
NO_UNRESOLVED_ISSUE
VERSION_FILE_NOT_READY
FILE_HASH_MISMATCH
UPLOAD_INCOMPLETE
IDEMPOTENCY_CONFLICT
OPTIMISTIC_LOCK_CONFLICT
PACKAGE_NO_FINALIZED_FILES
PACKAGE_SOURCE_MISSING
PACKAGE_NOT_READY
PACKAGE_EXPIRED
FILE_TYPE_NOT_ALLOWED
FILE_TOO_LARGE
STORAGE_UNAVAILABLE
```

## Security And Deployment

Required even without account system:

- Private network, VPN, or trusted gateway deployment.
- TLS preferred.
- Storage directories not exposed by Nginx.
- File ID indirect access.
- Path traversal defense.
- SQL parameterization.
- XSS escaping and safe text rendering for comments and text annotations.
- CSP and `X-Content-Type-Options: nosniff`.
- Temporary upload and ZIP cleanup.
- Short-lived download tokens.
- `shared_code` verification rate limiting.
- Trusted proxy header cleanup.

`none` write-guard mode provides no identity-level non-repudiation and is only for controlled intranet use.

## Precise Playback Backend Obligations

Persist and return the source fields needed to build `ReviewPlaybackTarget`:

```text
projectRefId
reviewItemId
versionId
issueId
currentRevisionId
annotationSetId when present
timestampMs
frameNumber
```

Query APIs must expose the current revision and current annotation set identity. Default playback uses current revision only, never an old revision unless explicitly supported by a future contract.

The backend must not map V1 issues, timestamps, frames, or coordinates onto V2. Historical-version issue reads are reference-only.

Precise playback frame math uses the target version's frozen `fpsNum/fpsDen`. Display timecode, version label, filename, current selected version, and media URL are not trusted playback locators.

## Future Extension Boundaries

Future accounts add or replace `PrincipalResolver`, `AccountAuthorizationAdapter`, and `ProjectMemberAuthorizationAdapter`.

Future notifications, tasks, delivery, and download center integrate through events and ports:

- Notification subscribes to issue created, changes requested, and version finalized.
- Task center subscribes to changes requested and package events.
- Delivery center consumes version finalized.
- Download center replaces `FinalizedPackagePort` while preserving package snapshot contract.
- Canvas embedding replaces local project catalog, no-account authorization, and standalone host bridge.

None of those extensions modify domain model, command/query payloads, state machine, business DTOs, player, annotation component, file reference model, or event structure.
## Implemented Backend Runtime Addendum

The runnable backend added for SPEC V1.3 is located in `backend/`.

Implementation map:

- `contracts/final-cut-review/v1`: contract source for OpenAPI, capabilities, errors, commands, queries, events, and module manifest.
- `backend/scripts/generate_contracts.py`: contract generator and drift check.
- `backend/app/modules/review_contracts/generated.py`: generated Pydantic DTOs and constants.
- `backend/app/modules/final_cut_review/domain`: pure domain invariants and timecode functions.
- `backend/app/modules/final_cut_review/application`: `ExecutionContext`, ports, `CommandBus`, and query service.
- `backend/app/modules/final_cut_review/infra`: SQLAlchemy schema, repository, transaction work, idempotency, Outbox, finalization and package persistence.
- `backend/app/modules/review_access`: static entry policy, no-account authorization, and write guard adapters.
- `backend/app/modules/review_media`: chunk upload, hash/magic/size validation, upload status, and Range helpers.
- `backend/app/modules/review_http`: shared read routes, `/edit` facade, `/review` facade, upload routes, envelope and context dependencies.

Runtime grants treat `alembic_version` as read-only and grant sequences only `USAGE, SELECT`; runtime cannot mutate migration state or call `setval()`. Default privileges use the same sequence boundary.

PostgreSQL 16 is the deployment target. SQLite is used only for local tests with `ALLOW_SQLITE_FOR_TESTS=true` and foreign keys enabled; delivery runtime must not silently fall back to SQLite. Docker Compose starts PostgreSQL, runs a one-shot migration service, then starts the backend, a dedicated maintenance service, and a dedicated package worker. The migration service idempotently creates distinct owner/migrator and runtime login roles, forces both to `NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT`, removes role memberships, transfers existing application and test database/schema/object sets to owner, runs Alembic as owner against both databases, grants runtime only database `CONNECT`, schema `USAGE`, table DML, and required sequence privileges on each, then exits. Database/schema `CREATE`, temporary-object creation, object ownership, and DDL remain unavailable to runtime. Backend startup depends on `service_completed_successfully`; the backend image CMD never runs Alembic. Admin and owner credentials are mounted only into the one-shot service, while backend, maintenance and package worker mount only the runtime database secret; only backend additionally mounts the write-guard signing secret. Compose environments contain `*_FILE` paths rather than secret values. PostgreSQL uses `POSTGRES_PASSWORD_FILE`; application and migration readers reject direct/file ambiguity, symlink or non-regular sources, empty/oversized/multiline/NUL/non-UTF-8 content. Direct environment values remain supported for explicit non-Compose Homebrew tests. The stack performs one complete storage-association preflight before starting HTTP, binds local-only ports, and provides `/healthz`, bounded database/Alembic `/runtimez`, and explicit full-storage `/readyz`. The high-frequency container healthcheck uses `/runtimez`; this avoids reopening every historical video every ten seconds while preserving fail-closed startup and operator readiness audits. The backend constructs the SQLAlchemy URL with `URL.create()` so URI-reserved password characters cannot corrupt the DSN.

The backend, one-shot migrate service, maintenance service, and package worker reuse one immutable `repository@sha256:<digest>` application image reference through `docker-compose.delivery.yml`; the overlay removes build configuration from all four services and inherits the base service-scoped secret mounts. All four application image services drop every Linux capability and add back only `DAC_READ_SEARCH` and `CHOWN` for traversal/repair of restricted legacy ownership plus `SETGID` and `SETUID` for `gosu app`. After that transition the process runs as fixed UID/GID `10001` with no effective capabilities. The overlay constructs the reference from separate repository and 64-lowercase-hex digest inputs. The supported `scripts/compose-delivery.sh` wrapper rejects mutable tags, a tag embedded in the repository field, and malformed digests before invoking Compose; the container engine and entrypoint then enforce the immutable reference and runtime identity. Native `docker compose config` remains a structural parser and is not treated as a digest authenticity, runtime capability, secret readability, or immutability check. The base Compose image tag exists only for local builds and is not a delivery startup reference. A root-only atomic lock serializes the three entrypoints and random exclusive temporary files prevent state collisions. Migration uses a no-symlink-follow recursive ownership update only when drift exists. Its data-volume identity marker lives in the separate root-owned `fj-final-cut-review-runtime-state` volume with mode `0444`, not in application-writable `/data`; this avoids duplicate full-volume scans by application services. Controlled restores or out-of-band imports must set `FORCE_DATA_OWNERSHIP_MIGRATION=1` on backend for one startup and return it to `0` after all services are healthy. The marker is not a continuous scanner, so an import that bypasses this forced revalidation is outside the supported restore contract. Application SQL and Alembic migrations use separate bounded statement timeouts, with a longer migration default.

One database has exactly one active storage-root contract. A host backend and a Compose backend must never write the same database with different roots. The HTTP lifespan holds a database-scoped PostgreSQL session advisory writer lock on one dedicated physical connection until shutdown; after acquiring that lock and before serving HTTP, the same runtime performs the complete storage-association audit. Business-session commits cannot return the lock connection to the pool, a second runtime fails startup, readiness checks the acquiring backend identity, and shutdown verifies unlock on the same connection. Maintenance and package-worker never acquire a second writer contract. Before each database/storage cycle they acquire the transaction fence as a shared session advisory lock on a dedicated participant connection, then verify from `pg_locks` that one backend PID holds both the database writer key and the current root-specific contract key in `ExclusiveLock` mode. The participant is bound through `ContextVar`; every marked worker Session commit takes the matching shared transaction fence and rechecks the participant before commit. If the backend releases while an old participant or worker transaction is active, that shared fence blocks any new exclusive acquisition, including a different-root backend, until the old cycle finishes. Participant unlock and connection PID must be confirmed on the acquiring connection. A missing owner, unbound marked Session, changed connection identity, or failed unlock is fail-closed; physical cleanup and package publication do not start without a lease. An already-bound HTTP writer lock satisfies the participant context without duplicate acquisition. SQLite bypass exists only when `ALLOW_SQLITE_FOR_TESTS=true`. Configured storage/package roots are opened component by component from `/` with directory FDs and `O_NOFOLLOW`, then inspected a second time for device/inode stability before acceptance. `/readyz` verifies every persisted file object and every unexpired ready package resolves to its canonical path below the configured root and is a pinned regular file; split-root, missing, symlink, cross-device, and path-escape associations fail readiness. Cutover from a host root must copy each file exclusively, verify size and SHA-256, fsync file and directory, then update paths in one transaction while retaining source files until restart/down-up verification passes.

Upload maintenance selects a stable bounded batch of stale sessions, caps every directory scan, removes fully reclaimed terminal rows so later batches advance, and defers failed rows without starving independent cleanup classes. The package worker takes one durable `preparing` snapshot at a time; maintenance only reclaims expired/unreferenced outputs and records `storage_reclaimed_at` so completed rows are not rescanned forever. Expired package deletion validates the canonical `package_id` to ZIP-path binding and skips an active download lease before unlink.

The maintenance loop reclaims stale upload parts, database-unreferenced upload candidates and final media, expired or unreferenced package files, and pending physical-delete tombstones. Managed roots are opened as private no-follow directories; a symlink root fails closed. A legal manifestless delete-quarantine entry is inspected only through its locked directory FD: if it is empty it remains as a benign shell and does not degrade the cycle, while any child or unverifiable state remains a failure. Runtime never performs a pathname `rmdir` for that manifestless state because POSIX cannot bind directory removal to the validated FD. Upload init requires a retry-stable idempotency key and atomically commits the idempotency response, upload session and quota reservation. A lost commit acknowledgement is observed through an independent session before any retry can reserve again. Init takes one PostgreSQL transaction advisory lock before calculating durable global/principal active-session and reserved-byte usage, and rejects requests that exceed either quota or the filesystem low-water mark. Initiated, receiving and finalizing rows count, as do completed/aborted rows whose referenced part cleanup is not confirmed; only a committed cleanup confirmation releases reservation. A PUT first enforces the configured part-number/body-size ceilings, then uses an independent short transaction and upload-session row lock to validate identity/owner/status, compute the remaining allowance, renew activity, commit, and close the connection before body I/O. Principal/session/process admission bounds the dedicated upload I/O executor. Request-body read, write, flush and `fsync` share one total timeout, and all blocking file operations run off the event loop. The body streams into a server-random `O_EXCL` candidate through a continuously pinned no-follow directory-FD chain without holding a database connection. After the body is durable it takes the upload-session row lock, repeats ownership/state/part validation, and commits candidate metadata in a short transaction. Maintenance or abort may win while the body is in flight; in that case the locked revalidation rejects the PUT and the uncommitted candidate is removed or left only for bounded TTL reconciliation. The candidate never uses the client Request-ID as file identity. A definite rollback deletes the candidate, while an ambiguous commit is reconciled through a fresh database session: committed references are preserved and returned as success, and an unobservable outcome leaves the unique candidate for TTL recovery rather than risking deletion of committed data. Complete claim acknowledgement loss follows the same independent-observer rule; if that observer is also unavailable, only an exact principal/idempotency/request replay may resume the active lease and deterministic file ID. Successful replacement deletes the superseded part only after commit.

Each upload reserves exactly twice its declared size so quota and low-water admission cover simultaneous part files plus the completed staging file. PUT preflight computes the remaining declared allowance excluding the part being replaced, rejects an oversized known `Content-Length` before candidate creation, and passes that remaining allowance to the streaming writer; the locked publish path repeats the authoritative total-size check. `UPLOAD_SESSION_TTL_SECONDS` must exceed the total body timeout by at least the configured safety margin.

The LAN delivery profile is sized for a shared no-account principal with process-local `16/1/64` admission: at least 10 distinct active sessions and 10 concurrent PUT admissions are accepted before quota, while one upload id can hold only one PUT candidate at a time to prevent same-session retry races. The bounded I/O executor may serialize disk writes without reducing admission capacity. The default inactivity TTL is 15 minutes, safely above the 120-second total body timeout plus its 60-second margin, so abandoned initiated/receiving sessions are reclaimed without retaining quota for a day. Declared-byte reservations and low-water checks still reject combinations that exceed real storage.

ISO-BMFF validation treats the browser MIME and individual `ftyp` brands as advisory classification rather than proof. The backend reads a bounded complete first `ftyp` box, validates its structure plus the allowed filename/MIME pair, then performs SHA-256 and the restricted single-video-stream ffprobe on the same pinned descriptor. Unknown structurally valid brands proceed to ffprobe; malformed `ftyp`, disguised non-media, probe failures, missing/extra video streams, or invalid positive media metadata fail closed.

Complete row-locks the upload only long enough to commit a `finalizing` lease, deterministic final file id and idempotency reservation. It then closes the session before concatenate, SHA-256, media probe, file `fsync` and directory `fsync`, and opens a new short row-locked transaction to validate the lease and publish `file_objects`. A process crash leaves a durable lease and candidate; after expiry, the same idempotency key can reclaim the row and verify/reuse that final file. An older worker whose lease was superseded cannot publish. Complete and abort retain `received_parts` after committing terminal state, delete parts outside the transaction, and clear metadata plus release quota only in a cleanup-confirmation transaction. Failed or unsafe physical cleanup stays referenced and reserved; completed/aborted rows become eligible for maintenance retry after a 300-second backoff. After every referenced part is removed or confirmed absent, a completed row that still has `file_id` is retained with `parts_cleanup_confirmed_at`, while a completed row detached by physical business deletion is deleted in the same commit that confirms cleanup; no detached row or quota is released before that point. A finalizing row older than the upload TTL is converted to aborted only after its lease has expired, with all lease fields cleared before physical cleanup, so crashed finalizers cannot reserve quota forever. A replaced final name is preserved rather than deleted. Maintenance claims terminal or stale rows with `FOR UPDATE SKIP LOCKED`, commits `aborted`, and only then deletes referenced parts. Orphans are deleted only after the TTL and a fresh database-reference check. Ambiguous delete commits retain the durable tombstone until maintenance can reconcile the database result. Failures are isolated per file and reported as `degraded`; malformed, oversized, invalid UTF-8, non-object, FIFO, directory-symlink, file-symlink and storage-root-escape tombstones are rejected. Directory scans retain a bounded rotating cursor per cleanup class, while failed database claims receive a bounded retry lease or refreshed ordering timestamp so fixed blockers cannot starve later work. All stored-file reads and deletes verify pinned device/inode identity. Tombstones are written through a private temporary file, file `fsync`, atomic rename and directory `fsync`; blob and tombstone unlink operations also sync their parent directories. Each cycle has a hard timeout, repeated errors terminate the process, and heartbeat is refreshed during long sleeps. A failed cycle retries after at most 10 seconds and a later successful cycle restores the normal configured interval, so a backend/maintenance simultaneous restart cannot extend a transient writer-fence startup race across the full normal interval. Compose health requires both a fresh heartbeat and status `ok`, so `degraded/error` is unhealthy. Maintenance receives database/path/timeout settings only and does not receive HTTP signing, browser proxy or CORS secrets. Trusted proxy download-session requests require a valid forwarded scheme and fail closed otherwise, preventing a TLS request from receiving a non-Secure cookie.

The finalizing lease persists both a hash of the idempotency key and a canonical completion-request hash. The phrase "same idempotency key" above therefore also requires the same request hash; any different identity receives `IDEMPOTENCY_CONFLICT`. Migration `20260714_0013` resets unverifiable legacy finalizing rows to a recoverable receiving state instead of allowing an unknown operation to take them over.

Finalization lease expiry opens a takeover opportunity; it does not invalidate the current publisher until a newer lease wins. The finalization claim, including its random immutable `file_id`, is committed before any final path is published. The orphan scan's reference set includes both `UploadSession.finalization_file_id` and `UploadSession.file_id`; when maintenance reclaims an expired claim it first locks the upload row and commits the abort before scanning. A superseded publisher then fails lease validation, and no API can attach an arbitrary database-invisible file ID. Together with strict ID names and the stale-file cutoff, this prevents a recovered candidate from becoming a new reference between the negative lookup and unlink.

The in-process PUT admission limiter is valid only for the current Compose topology: exactly one backend replica and one Uvicorn worker. Compose pins both values. Horizontal scaling is blocked until a distributed admission coordinator replaces the process-local counters.

The current backend intentionally does not implement login, users, members, notifications, tasks, delivery, revoke finalization, or download-center history. Delete scope is limited to project soft delete, issue soft delete, and the pre-review duplicate item physical delete described above.
