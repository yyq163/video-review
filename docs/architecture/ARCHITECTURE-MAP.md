# ARCHITECTURE MAP

Baseline source: `FJ_Final_Cut_Review_SPEC_V1.3_Reviewed.md`.
This map records the required SPEC module shape. The current runnable repo may use mock adapters, but mocks must not collapse the target boundaries.

## Current Frontend Tree

```text
src/
├── app/
│   ├── router.tsx
│   └── query-client.ts
└── modules/final-cut-review/
    ├── contracts-generated/
    ├── core/
    │   ├── timecode.ts
    │   ├── coordinates.ts
    │   ├── playback.ts
    │   ├── errors.ts
    │   ├── file-names.ts
    │   └── sha256.ts
    ├── ports/
    ├── adapters/
    ├── host/
    ├── entry/
    ├── pages/
    ├── components/
    └── styles/
```

Current mock composition:

```text
createReviewRuntime()
  -> InMemoryReviewRepository
  -> MockFileStorageAdapter
  -> MockFinalizedPackageAdapter
  -> NoAccountPermissionAdapter / NoAccountPrincipalAuthorizationAdapter
  -> NoAccountEntryPolicyAdapter / SimpleWriteGuardAdapter
  -> MockReviewApiAdapter
  -> React Query hooks and shared pages
```

These local mock names are runnable-demo adapter names. SPEC target adapter roles remain
`StaticEntryPolicyAdapter`, `NoAccountAuthorizationAdapter`, `NoWriteGuardAdapter`,
`SharedCodeWriteGuardAdapter`, and `ReverseProxyWriteGuardAdapter`; mocks are replaceable
implementations of ports and are not the architecture boundary.

## Required Contract Source Tree

```text
contracts/final-cut-review/v1/
├── openapi.yaml
├── capabilities.yaml
├── errors.yaml
├── commands/
│   ├── project.commands.json
│   ├── review-item.commands.json
│   ├── issue.commands.json
│   ├── finalization.commands.json
│   └── package.commands.json
├── queries/
├── events/
└── module-manifest.json
```

Generated artifacts:

```text
TypeScript DTOs/client
backend request/response schemas
capability constants
event schemas
contract fixtures
```

No frontend or backend file may define a second semantic DTO source for a contract-owned shape.

## Required Backend Module Map

```text
src/modules/
├── review_contracts/
├── project_catalog/
├── final_cut_review/
│   ├── domain/
│   │   ├── aggregates.py
│   │   ├── entities.py
│   │   ├── enums.py
│   │   ├── commands.py
│   │   ├── events.py
│   │   ├── invariants.py
│   │   └── errors.py
│   ├── application/
│   │   ├── command_handlers.py
│   │   ├── query_services.py
│   │   ├── ports.py
│   │   └── transaction.py
│   ├── infra/
│   │   ├── sqlalchemy_models.py
│   │   ├── repositories.py
│   │   └── migrations/
│   └── tests/
├── review_access/
├── review_media/
├── finalized_package/
├── review_integration/
└── review_http/
    ├── query_routes.py
    ├── edit_command_routes.py
    ├── review_command_routes.py
    ├── context_dependencies.py
    └── generated_schemas.py
```

Minimum application ports:

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

## Logical Dependency Map

```text
review-contracts
  -> final-cut-review-core
  -> final-cut-review-application
  -> ports
  -> adapters
  -> review-http / review-ui / review-integration
```

Forbidden reverse edges:

```text
domain -> framework/storage/http/ui/access-control/host/localStorage
application -> concrete file path or storage backend
ui -> repository/database/trusted identity
integration consumer -> direct aggregate mutation
```

## Command Flow

```text
HTTP route facade
  -> create server-side ExecutionContext
  -> map fixed route to capability and command_type
  -> validate Idempotency-Key / command_id
  -> EntryPolicyPort
  -> WriteGuardPort
  -> PrincipalAuthorizationPort
  -> Application Command Handler
  -> repository ancestry check
  -> domain state machine
  -> transaction
  -> outbox event
  -> unified envelope
```

Edit and review facades inject different `entry_source` values but converge on the same command handlers.

## Query Flow

```text
Shared GET route
  -> ExecutionContext with read semantics
  -> QueryService
  -> full context query
  -> repository validates project/item/version/issue ancestry
  -> read DTO
  -> unified envelope
```

Query keys and queries must include complete ancestry:

```text
projectRefId
reviewItemId
versionId
issueId when applicable
```

Using only `versionNo`, `itemCode`, `issueId`, filename, or display timecode as identity is forbidden.

## Project Catalog Flow

```text
/edit project writes
  -> ProjectCatalogPort
  -> LocalReviewProjectCatalogAdapter now
  -> CanvasProjectCatalogAdapter later
```

Review core stores `project_ref_id` and never depends on local project table structure. `getFeatures()` controls unavailable catalog writes and returns `PORT_OPERATION_NOT_SUPPORTED` server-side when unsupported.

## Media And Upload Flow

```text
upload init / parts / status / complete / abort
  -> FileStoragePort
  -> MIME/ext/magic-bytes/size/SHA-256 validation
  -> MediaPort.probe(file_id)
  -> OriginalMediaSnapshot with rational fps
  -> ReviewVersion creation only after upload/probe/hash complete
  -> async playback proxy when needed
```

Playback readiness is exposed as DTO status: `processing`, `ready`, or `failed`. Non-ready versions block start review, issue creation, request changes, and finalization.

`abort` only terminates unfinished temporary upload sessions. It is not a business delete.

## Finalization And Package Flow

Single finalized-original download:

```text
review_item.active_finalization_id
  -> finalization.version_id
  -> finalization.original_media.original_file_id
  -> FileStoragePort.download
```

Project package:

```text
PrepareFinalizedPackage command
  -> freeze active finalizations for current project
  -> snapshot review_item_id/version_id/original_file_id/original_filename/sha256/package_filename
  -> build ZIP from snapshot only
  -> ready/failed/expired package status
  -> short-lived download token
```

ZIP filename:

```text
{project_code}_{project_name}_定稿原片_{YYYYMMDD-HHmm}.zip
```

Package contents:

```text
{item_code}_{safe_title}_{version_label}_{original_filename}
```

No historical versions, non-finalized versions, proxies, thumbnails, issues, annotations, JSON, CSV, PDF, or project material are included.

## Domain Event Flow

```text
Command Handler transaction
  -> business rows
  -> outbox event rows
  -> publisher retry loop
  -> idempotent consumers by event_id
```

Operation log is separate from domain event outbox. It records request/correlation IDs, entry source, principal ref when available, IP, user agent, capability, result, and error code.

## Host Integration Map

```text
ReviewModuleManifest
  -> standaloneRoutes: /edit, /review
  -> mountSlots: workspace.main
  -> optionalHostServices:
       project_catalog
       principal_context
       authorization
       http_client
       event_bus
       file_service
       portal_root
       theme
```

Embedded mount flow:

```text
ReviewHostBridge.mount(container, initialProjectRefId)
  -> host project catalog when injected
  -> host authorization when injected
  -> host context change listener
  -> cancel old requests and playback on context switch
  -> recalculate capability gates on host permission change
```

## Precise Playback Composition

```text
Issue card / timecode / timeline point / previous / next
  -> build ReviewPlaybackTarget from contract data
  -> sequence or playback_request_id guard
  -> validate full ancestry
  -> switch version when target.versionId differs
  -> wait target version data
  -> wait playback_ready
  -> ReviewPlayer controls real HTMLVideoElement
  -> loadedmetadata / canplay / seeked / optional frame callback
  -> pause
  -> render selected issue current Revision AnnotationSet only
  -> highlight selected card and marker
```

Frame conversion:

```text
frame_number = floor(timestamp_ms * fps_num / (1000 * fps_den))
timestamp_ms = floor(frame_number * 1000 * fps_den / fps_num)
```

Coordinate composition:

```text
pointerToNormalizedVideoPoint()
  -> computeContainedVideoRect()
  -> normalized video-space point
  -> normalizedVideoPointToCanvasPoint()
  -> overlay inside actual video bounds
```

Precision playback must not:

```text
map V1 issue to V2
overlay V1 markers on V2
derive target from current media URL
use fixed setTimeout instead of media events
show all version markers by default
let stale events override the latest target
```
