# ARCHITECTURE

Baseline source: `FJ_Final_Cut_Review_SPEC_V1.3_Reviewed.md`.
This document must stay aligned with SPEC V1.3, especially sections 5-12, 20-33, 36-40.

## Product And Boundary Rules

- The module has exactly two standalone front-end entries: `/edit` and `/review`.
- `/edit` owns project management, review item creation, V1 upload, version upload, issue viewing, version compare, finalization read, and finalized-original single download.
- `/review` owns review work: start review, create/update/reply/resolve/reopen issues, request changes, finalize, finalized-original single download, and project finalized-original package create/read/download.
- Entry source is not identity. It is only one input to capability calculation.
- V1 has no delete capability, no delete endpoint, no physical business delete, no cross-version issue tracking, no automatic fix judgment, no download center, and no account-specific permission model.
- Current no-account mode must still use access-control ports. "No account" is not a reason to hard-code authorization away.

## Logical Modules

The architecture is split by SPEC module boundary, not by current mock implementation convenience:

```text
review-contracts
project-catalog
final-cut-review-core
final-cut-review-application
review-access-control
review-media
finalized-package
review-integration
review-http
review-ui
```

Responsibilities:

- `review-contracts`: OpenAPI, capability registry, command/query DTOs, event schemas, error registry, module manifest, and generated client/server types. It contains no business logic and no database access.
- `project-catalog`: local project create/update/archive/restore now, host project adapter later. It must not own review item, version, issue, or finalization rules.
- `final-cut-review-core`: review item, version, issue, revision, annotation set, thread message, decision, finalization domain model and invariants. It must not depend on HTTP, UI, storage, permission adapters, or host bridge code.
- `final-cut-review-application`: command handlers, query services, transaction orchestration, idempotency lookup, optimistic-lock enforcement, and port calls.
- `review-access-control`: entry policy, write guard, and future principal authorization ports/adapters. It must not mutate domain objects.
- `review-media`: upload session, original media, playback proxy, media probe, streaming, playback readiness, and file validation.
- `finalized-package`: active finalization snapshot freeze, ZIP build, package status, short-lived download token. It must not decide which version is eligible for finalization.
- `review-integration`: outbox, domain event publication, operation log, host bridge, and future notification/task/delivery integrations. Consumers cannot directly rewrite review aggregates.
- `review-http`: route facade, DTO mapping, server-side `ExecutionContext` creation, request headers, and command/query dispatch.
- `review-ui`: shared `/edit` and `/review` pages/components, capability gates, host bridge mount/unmount, precise playback UI, and style isolation.

## Dependency Direction

```text
review-contracts
      ↑
final-cut-review-core
      ↑
final-cut-review-application
      ↑
ports / adapters
      ↑
review-http / review-ui / review-integration
```

Allowed dependencies:

- `application -> domain`
- `application -> ports`
- `adapter -> application ports`
- `http -> application`
- `ui -> generated contracts/client`

Forbidden dependencies:

- `domain -> FastAPI | SQLAlchemy | React | HTTP | Nginx | Write Guard | Host Bridge | localStorage`
- `application -> concrete S3 | MinIO | local physical path`
- `ui -> database | in-memory repository | trusted capability/principal calculation`
- `review-media -> review state machine`
- `finalized-package -> review finalization eligibility decision`

## Unified Contract Layer

External contract source is a single `contracts/final-cut-review/v1/` tree:

```text
openapi.yaml
capabilities.yaml
errors.yaml
commands/
queries/
events/
module-manifest.json
```

Generated outputs must include TypeScript DTO/API client, backend request/response schemas, capability constants, event payload schemas, and contract fixtures. Frontend and backend must not hand-write divergent DTOs with the same semantic names.

Wire JSON uses `snake_case`. Internal TypeScript may expose camelCase only as generated projection from the same schema.

Contract versions:

- API path version: `/api/v1`.
- Contract version: `1.0`.
- Event versions start at `1` per event type.
- Module manifest version: `1`.

V1 may add optional fields, endpoints, capabilities, event types, and unknown-safe enum values. V1 must not delete fields, make optional fields required, change existing semantics, or change error/event field meanings.

All API responses use the unified envelope:

- Success: `{ data, meta: { request_id, contract_version } }`
- List: `{ data: [], meta: { total_count, page, page_size, request_id, contract_version } }`
- Error: `{ error: { code, message, http_status, details, request_id, timestamp, contract_version } }`

## Execution Context And Access Control

`ExecutionContext` is always created by the server. Client request bodies and trusted headers must not submit `capabilities`, `permissions`, `roles`, `is_admin`, `is_reviewer`, `security_context`, `write_guard_verified`, or `principal_id`.

The access-control stack is three independent strategy layers:

- `EntryPolicyPort`: entry profile capability check for `edit`, `review`, `embedded`, or `unspecified`.
- `WriteGuardPort`: `none`, `shared_code`, or `reverse_proxy` write-protection verification.
- `PrincipalAuthorizationPort`: current no-account adapter, future account/member/role/host authorization adapters.

Current adapters:

- `StaticEntryPolicyAdapter`
- `NoAccountAuthorizationAdapter`
- `NoWriteGuardAdapter`
- `SharedCodeWriteGuardAdapter`
- `ReverseProxyWriteGuardAdapter`

Authorization order:

```text
1. HTTP facade determines entry_source
2. PrincipalResolver resolves principal
3. WriteGuard validates write protection
4. Route maps command to capability
5. EntryPolicy validates entry capability
6. PrincipalAuthorization validates principal capability
7. Repository/query validates full ancestry
8. Domain state machine validates transition
9. Command executes
```

The final authorization result is the intersection of all layers, never a union. `ProjectCatalogPort.getFeatures()` also participates in UI and command availability.

`embedded` entry capability comes from host-injected entry profile. If none is injected, embedded defaults to read-only.

## Capability Profiles

`/edit` profile:

```text
review.project.read
review.project.create
review.project.update
review.project.archive
review.project.restore
review.item.read
review.item.create
review.item.update
review.version.read
review.version.upload
review.version.compare
review.issue.read
review.finalization.read
review.download.finalized_original
```

`/review` profile:

```text
review.project.read
review.item.read
review.version.read
review.version.compare
review.issue.read
review.issue.create
review.issue.update
review.issue.reply
review.issue.resolve
review.issue.reopen
review.session.start
review.session.request_changes
review.finalization.read
review.finalization.create
review.download.finalized_original
review.package.create
review.package.read
review.package.download
```

No delete capability is registered in V1. Future deletion or voiding must be a new formal command, state, event, migration, and contract-version decision.

## Project Catalog Abstraction

Review core only stores and accepts `project_ref_id`; it must not depend on the local project database shape.

`ProjectRef` fields are `projectRefId`, `projectCode`, `projectName`, `source: "local" | "host"`, and optional `externalProjectId`.

`ProjectCatalogPort` owns:

- `getFeatures()`
- `list`
- `get`
- `create`
- `update`
- `archive`
- `restore`

Current implementation is `LocalReviewProjectCatalogAdapter`. Future embedded host implementation is `CanvasProjectCatalogAdapter`.

Project status is split:

- `lifecycle_status`: persisted, `active` or `archived`.
- `completion_status`: derived as `empty`, `in_progress`, or `completed`.

Archived projects are read-only and restorable. Unsupported catalog write capabilities return `PORT_OPERATION_NOT_SUPPORTED`.

## Domain Model And Invariants

Core aggregate model:

- `FinalCutReviewItem`: belongs to one project, has workflow status, current version, optional active finalization, and `lock_version`.
- `ReviewVersion`: belongs to one item, has version number, previous version, current flag, original media snapshot, playback asset, thumbnail, notes, and `lock_version`.
- `OriginalMediaSnapshot`: freezes `original_file_id`, filename, MIME, size, SHA-256, duration, dimensions, `fps_num`, `fps_den`, and media probe version. Frame rate is rational, never only float.
- `ReviewIssue`: belongs to exactly one project, item, and version. It has issue number, unresolved/resolved status, current revision, timestamp, frame number, and `lock_version`.
- `ReviewIssueRevision`: immutable content/annotation replacement revision. Editing creates a new revision.
- `ReviewAnnotationSet`: immutable snapshot tied to one issue, revision, version, timestamp, frame, canvas size, video size, and shapes.
- `ReviewThreadMessage`: belongs to one issue and version. Current V1 does not store personal names.
- `ReviewDecision`: `changes_requested` only.
- `FinalizationRecord`: active finalization freezes version and original media. V1 has no supersede command.
- `FinalCutPackageSnapshot`: belongs to `finalized-package`, not the review aggregate.

Global invariants include:

- Version number is unique only within one review item.
- Only one current version exists per review item.
- Historical original media references are immutable.
- Current version pointer changes only after upload completes.
- Finalized items reject all write commands.
- Finalization only checks the current version.
- Historical unresolved issues never block current-version finalization.
- Only one active finalization exists per item.
- Package creation reads only finalization data frozen at package snapshot creation.
- Media download always uses File ID, never physical paths.
- Database, repository, and application service all validate parent-child ancestry.

## State Machines

Review item workflow:

```text
Create V1 -> pending_review
pending_review -> in_review
in_review -> changes_requested
changes_requested -> pending_review
pending_review / in_review -> finalized
```

Rules:

- `pending_review -> in_review` requires explicit `StartReviewCommand` or implicit same-transaction transition when creating the first issue. Playback must be ready.
- Playing, seeking, switching versions, and GET requests never change workflow status.
- `in_review -> changes_requested` requires at least one unresolved current-version issue and a note.
- `changes_requested -> pending_review` happens after successful version upload.
- `pending_review -> pending_review` version upload is allowed only before review or for upload mistake replacement and requires `supersede_reason`.
- `in_review` cannot upload a new version.
- `finalized` cannot write anything.
- Finalization requires current version, zero unresolved current-version issues, playback ready, original file available, and hash verified.

Issue workflow:

```text
unresolved -> resolved
resolved -> unresolved
```

Only `/review` can resolve or reopen. Status affects only the issue's exact version.

## Files, Media, Download, And Package

File roles:

```text
project_cover
review_original
playback_proxy
thumbnail
package_temp
```

`review-media` owns upload sessions, hash verification, media probe, playback proxy generation, stream URLs, and playback readiness. `processing` or `failed` playback status blocks start review, issue creation, request changes, and finalization.

Uploads require multipart/resumable behavior, progress, retry, page-leave protection, MIME/extension/magic-bytes/size/SHA-256 validation, and at least 2 GB per file as configurable deployment value.

Finalized single download:

```text
review_item.active_finalization_id
-> finalization.version_id
-> finalization.original_media.original_file_id
-> FileStoragePort.download
```

It returns the original upload with original container/encoding, supports Range, never returns playback proxy, never exposes a permanent public URL, and never downloads historical non-finalized versions.

Project package is `/review` only. It contains current project active finalization originals only. It excludes historical versions, unfinished items, proxies, thumbnails, issues, annotations, JSON/CSV/PDF, and project files.

Package snapshot freezes `review_item_id`, `version_id`, `original_file_id`, `original_filename`, `sha256`, and `package_filename`. Missing or hash-mismatched source fails the whole package.

## HTTP, Commands, Queries, Events

HTTP routes are thin facades:

- Shared reads have one route set and do not depend on client-reported entry.
- Edit writes and review writes are separate route sets that inject `entry_source`.
- Both write route sets call the same command handlers.
- No domain service or repository is duplicated per entry.

Commands use `CommandEnvelope` with `command_id`, `command_type`, `contract_version`, optional expected aggregate version, and payload. `ExecutionContext` is passed separately by the server. `Idempotency-Key` and `command_id` must match or be gateway-mapped.

Queries must carry full context. `getVersion(versionId)`, `getIssue(issueId)`, and `getAnnotation(annotationId)` are forbidden. Read models are DTOs, never ORM entities or aggregate roots.

Domain events are written to Outbox in the same transaction as business data. Event consumers are idempotent by `event_id` and must call commands for mutations. Operation logs are separate from domain events.

## Host Integration

The module supports `standalone` and `embedded` render modes through `ReviewHostBridge`.

Manifest facts:

- `moduleId`: `final-cut-review`
- standalone routes: `/edit`, `/review`
- mount slot: `workspace.main`
- required host services: none
- optional host services: project catalog, principal context, authorization, HTTP client, event bus, file service, portal root, and theme.

Embedded rules:

- No standalone global top bar.
- Root container is `width:100%; height:100%`.
- Project source comes from host project catalog when injected.
- Authorization comes from host adapter when injected.
- HTTP, events, files, portal, and theme may be injected by host.
- Context changes cancel old requests and clear playback state.
- Host permission changes recalculate capability gates without changing domain model.

## Precise Annotation Playback

Precise playback is a shared review capability, not a page-local shortcut. Every entry point builds a `ReviewPlaybackTarget`:

```text
projectRefId
reviewItemId
versionId
issueId
revisionId
annotationSetId?
timestampMs
frameNumber
```

It is forbidden to locate playback by only `timeMs`, current selected version, current media URL, array index, display version number, filename, or rendered timecode text.

Playback sequence:

```text
read target issue
validate project/item/version/issue ancestry
switch to target version when needed
wait for target data and playback_ready
wait for loadedmetadata/canplay
verify media still belongs to target version
convert frame by frozen fps_num/fps_den
set real HTMLVideoElement.currentTime
wait for seeked
wait for requestVideoFrameCallback when available
pause
load current revision
load its AnnotationSet
render only that selected AnnotationSet
highlight card and timeline point
scroll card into view
```

Precise playback only promises same-version review-timeline frame accuracy. It must not map V1 issues, coordinates, or frame numbers onto V2.

Every playback request has a sequence or request id. Stale media events, stale frame callbacks, stale queries, and stale version loads cannot overwrite the latest selected target. Component unmount and project/item/version switch cancel pending requests and clear old playback state.

Annotation capture and replay use the contained video rectangle, not black stage bounds. Unselected marker display strategy must be globally consistent and must not default to showing every issue marker permanently.

## Architecture Verification

Required verification gates:

- Import guard for module direction.
- Contract schema validation.
- OpenAPI breaking-change check.
- Event schema compatibility check.
- Domain dependency scan.
- Frontend/backend generated type hash check.
- Capability profile route tests for `/edit` and `/review`.
- State-machine tests, ancestry tests, idempotency tests, package snapshot tests, and precise playback tests from SPEC section 40.

## Backend Runtime Implementation

The SPEC V1.3 backend now lives under `backend/app` and follows the same module boundaries as this architecture document:

```text
backend/app/modules/
├── review_contracts/        # generated Pydantic DTOs, capabilities, errors, manifest
├── project_catalog/         # local/host catalog boundary marker
├── final_cut_review/
│   ├── domain/              # pure enums, entities, invariants, timecode, errors
│   ├── application/         # ExecutionContext, ports, CommandBus, query service
│   └── infra/               # SQLAlchemy models, repository, Alembic metadata
├── review_access/           # entry policy, no-account auth, write guard adapters
├── review_media/            # upload sessions, magic/hash checks, Range parsing
├── finalized_package/       # package boundary marker; snapshot build in repository
├── review_integration/      # Outbox boundary marker
└── review_http/             # shared queries, edit facade, review facade, uploads
```

Domain code does not import FastAPI, SQLAlchemy, HTTP, storage, authorization, or host-platform modules. Application code depends on domain contracts and ports; concrete persistence and file work are in infra/media adapters.

The database target is PostgreSQL through SQLAlchemy and Alembic. Tests run against SQLite with the same SQLAlchemy metadata and foreign keys enabled. The initial migration is `backend/alembic/versions/20260619_0001_initial_final_cut_review.py`.

Runtime guarantees implemented and tested:

- Server-generated `ExecutionContext`.
- Entry capability intersection for `/edit` and `/review`.
- `WRITE_GUARD_MODE=none|shared_code|reverse_proxy`; shared code signs a short-lived HttpOnly cookie and never stores the code.
- Full ancestry checks for project/item/version/issue/package queries.
- Version-independent issues and annotations.
- Current Revision playback target with exact `project_ref_id`, `review_item_id`, `version_id`, `issue_id`, `revision_id`, `annotation_set_id`, `timestamp_ms`, and `frame_number`.
- State machine: `pending_review`, `in_review`, `changes_requested`, `finalized`.
- Idempotency records for create/conclusion commands.
- Optimistic locking through `If-Match`.
- Finalization freezes original file identity/hash/media snapshot.
- Project package snapshots are immutable and expire.
- No DELETE routes are registered.
