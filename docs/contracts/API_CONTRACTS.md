# API CONTRACTS

Baseline source: `FJ_Final_Cut_Review_SPEC_V1.3_Reviewed.md` V1.4 normative amendment.
This file describes the current external and internal Contract V1 semantics.

## Single Source And Versioning

Contract source:

```text
contracts/final-cut-review/v1/
├── openapi.yaml
├── capabilities.yaml
├── errors.yaml
├── commands/
├── queries/
├── events/
└── module-manifest.json
```

Generated outputs:

- TypeScript DTOs and API client.
- Backend request/response schemas.
- Capability constants.
- Event payload schemas.
- Contract test fixtures.

Wire JSON uses `snake_case`. Generated TypeScript may expose camelCase. Hand-written duplicate DTO semantics are forbidden.

Versions:

- HTTP path: `/api/v1`.
- Contract: `1.0`.
- Module manifest: `1`.
- Event version: starts at `1` per event type.

Breaking changes require Contract V2. V1 can add optional fields, endpoints, capabilities, event types, and unknown-safe enum values only.

## Unified Envelope

Success:

```json
{
  "data": {},
  "meta": {
    "request_id": "uuid",
    "contract_version": "1.0"
  }
}
```

List:

```json
{
  "data": [],
  "meta": {
    "total_count": 100,
    "page": 1,
    "page_size": 20,
    "request_id": "uuid",
    "contract_version": "1.0"
  }
}
```

Error:

```json
{
  "error": {
    "code": "RESOURCE_STATE_CONFLICT",
    "message": "current state does not allow this operation",
    "http_status": 409,
    "details": {},
    "request_id": "uuid",
    "timestamp": "ISO-8601",
    "contract_version": "1.0"
  }
}
```

## Execution Context Contract

`ExecutionContext` is server-created and passed to application services separately from command payloads.

Fields:

```text
requestId
correlationId
causationId?
entrySource: edit | review | embedded | unspecified
principal.kind: anonymous | account | service
principal.id?
writeGuard.mode: none | shared_code | reverse_proxy
writeGuard.verified
client.ip?
client.userAgent?
host.hostProjectId?
host.hostModuleId?
```

The reverse-proxy write marker is an infrastructure-only header. Browser
clients must not emit it, it is not included in CORS request headers, the proxy
must strip any inbound value before adding its own marker, and trusted proxy
hosts default to an empty fail-closed allowlist.

Client bodies and trusted headers must not submit:

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

## Capabilities

`/edit` profile:

```text
review.project.read
review.project.create
review.project.update
review.item.read
review.item.create
review.item.update
review.item.delete
review.version.read
review.version.upload
review.version.compare
review.issue.read
review.issue.resolve
review.finalization.read
review.download.finalized_original
```

`/review` profile:

```text
review.project.read
review.project.archive
review.project.restore
review.project.delete
review.item.read
review.version.read
review.version.compare
review.issue.read
review.issue.create
review.issue.update
review.issue.reply
review.issue.reopen
review.issue.delete
review.finalization.read
review.finalization.create
review.download.finalized_original
review.package.create
review.package.read
review.package.download
```

`review.session.start` and `review.session.request_changes` remain recognized only as legacy wire capabilities/command shapes. They are not granted to either current entry profile. `StartReview` and `RequestChanges` routes may remain present for rolling compatibility, but current principals receive `403 ENTRY_CAPABILITY_DENIED` and no current UI exposes them.

No general-purpose physical delete capability exists in V1. `review.item.delete` is the sole, edit-entry-only physical-delete exception for a duplicate item before review starts; it deletes the item row, its single unreviewed version, upload sessions, any file object not referenced by another version/finalization, and the referenced storage-root blob after the database transaction commits. Audit events keep aggregate ids but must not retain foreign keys to deleted rows. `review.project.delete` is a review-entry soft delete command: it sets `deleted_at`, hides the project from normal list/detail/workspace/media/package query surfaces, keeps descendants and media for audit, and rejects all later write commands. `review.issue.delete` is a review-entry issue soft delete.

## Command Envelope

```text
commandId
commandType
contractVersion: 1.0
expectedAggregateVersion?
payload
```

The client sends business payload plus concurrency/idempotency data only. The server supplies `ExecutionContext`.

HTTP `Idempotency-Key` must match `command_id` or be gateway-mapped. Each endpoint maps to one fixed `command_type`; client mismatch is rejected.

Command types:

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

`StartReview` and `RequestChanges` in the command registry are compatibility-only. The first `CreateReviewIssue` performs the start transition in the same transaction.

`SoftDeleteProject.payload` 必须同时包含 `project_ref_id` 与常量 `confirmed: true`。浏览器二次确认只负责取得
用户意图，不能代替服务端合同门禁；缺失或 `false` 必须在命令执行前返回 `422 VALIDATION_ERROR`。

Representative payload requirements:

- `CreateReviewItem`: `projectRefId`, `itemCode`, optional `episodeNo`, `title`, `originalFileId`, optional `versionNote`.
- The V1 frontend creates one stable row per selected `File` and submits rows sequentially. Each row keeps its own title, episode and failure. Explicit failures continue; successes are removed; uncertain results stop the batch. List-refetch failure after a successful command never changes the command result or causes a second upload.
- The HTTP frontend keeps the completed upload result, created item/version and one stable `CreateReviewItem` command/idempotency ID for the lifetime of the same mounted-page V1 `File` + project + title + episode operation. A lost command response is uncertain and reuses the operation identity; a post-success project-list refresh failure only shows `文件已上传成功，待审列表暂时刷新失败，请刷新页面查看。`.
- `UploadReviewVersion`: `projectRefId`, `reviewItemId`, `originalFileId`, optional `versionNote`, optional `changeSummary`. The optional wire field `supersedeReason` remains accepted for backward compatibility but is not required and is not a gate.
- The HTTP frontend applies the same mounted-page operation identity to V2/V3 append: one `File` + project + item + version metadata tuple keeps its completed upload and one stable `UploadReviewVersion` command ID. A lost command response is retried with that command ID and cannot allocate another version; changing any tuple field starts a distinct operation.
- `CreateReviewIssue`: `projectRefId`, `reviewItemId`, `versionId`, `content`, `timestampMs`, `frameNumber`, optional annotation.
- `FinalizeVersion`: `projectRefId`, `reviewItemId`, `versionId`, `confirmed: true`.

## Query Contracts

Queries return DTO read models, not ORM objects or aggregate roots.

Every object lookup uses full context:

```text
projectRefId
reviewItemId when applicable
versionId when applicable
issueId when applicable
```

Forbidden:

```text
getVersion(versionId)
getIssue(issueId)
getAnnotation(annotationId)
```

Statistics must distinguish current-version unresolved/resolved storage values from historical-version counts. User-facing labels are “未修改/已修改”. Issue query order is unresolved first, resolved second, then `timestamp_ms`, then `issue_no`; both entry UIs and playback navigation use the same ordering. Current-workspace queries poll every 2.5 seconds only while mounted; historical-version queries do not poll. No SSE/WebSocket contract is introduced.

## HTTP Routes

Shared read API:

```http
GET /api/v1/final-cut-review/projects
GET /api/v1/final-cut-review/projects/{project_ref_id}
GET /api/v1/final-cut-review/projects/{project_ref_id}/items
GET /api/v1/final-cut-review/projects/{project_ref_id}/items/{review_item_id}
GET /api/v1/final-cut-review/projects/{project_ref_id}/items/{review_item_id}/versions
GET /api/v1/final-cut-review/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}
GET /api/v1/final-cut-review/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/issues
GET /api/v1/final-cut-review/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/issues/{issue_id}
GET /api/v1/final-cut-review/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/issues/{issue_id}/revisions
GET /api/v1/final-cut-review/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/issues/{issue_id}/messages
GET /api/v1/final-cut-review/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/stream
GET /api/v1/final-cut-review/projects/{project_ref_id}/items/{review_item_id}/finalization
GET /api/v1/final-cut-review/projects/{project_ref_id}/items/{review_item_id}/finalized-original/download
```

Edit write facade:

```http
POST  /api/v1/final-cut-review/edit/projects
PATCH /api/v1/final-cut-review/edit/projects/{project_ref_id}
POST  /api/v1/final-cut-review/edit/projects/{project_ref_id}/items
PATCH /api/v1/final-cut-review/edit/projects/{project_ref_id}/items/{review_item_id}
POST  /api/v1/final-cut-review/edit/projects/{project_ref_id}/items/{review_item_id}/delete
POST  /api/v1/final-cut-review/edit/projects/{project_ref_id}/items/{review_item_id}/versions
POST  /api/v1/final-cut-review/edit/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/issues/{issue_id}/resolve
```

`CreateProject` accepts optional `description` (maximum 2000 characters) as ordinary project metadata. `ProjectDTO.description` is always present. `external_project_id` remains an optional host-system identity and is never a substitute for description; repeated descriptions across local projects are valid.

Review write facade:

```http
POST  /api/v1/final-cut-review/review/projects/{project_ref_id}/archive
POST  /api/v1/final-cut-review/review/projects/{project_ref_id}/restore
POST  /api/v1/final-cut-review/review/projects/{project_ref_id}/soft-delete
POST  /api/v1/final-cut-review/review/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/issues
PATCH /api/v1/final-cut-review/review/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/issues/{issue_id}
POST  /api/v1/final-cut-review/review/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/issues/{issue_id}/soft-delete
POST  /api/v1/final-cut-review/review/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/issues/{issue_id}/messages
POST  /api/v1/final-cut-review/review/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/issues/{issue_id}/reopen
POST  /api/v1/final-cut-review/review/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/finalize
POST  /api/v1/final-cut-review/review/projects/{project_ref_id}/finalized-originals/packages
GET   /api/v1/final-cut-review/review/projects/{project_ref_id}/finalized-originals/packages/{package_id}
POST  /api/v1/final-cut-review/review/projects/{project_ref_id}/finalized-originals/packages/{package_id}/download-session
GET   /api/v1/final-cut-review/review/projects/{project_ref_id}/finalized-originals/packages/{package_id}/download
```

Legacy `/start` and `/request-changes` route shapes remain registered only for rolling compatibility and capability-deny current entry profiles. Review-side resolve is likewise not a current capability; only the edit resolve facade and review reopen facade are authorized.

The frontend first prepares a package and renders `preparing`, `ready`,
`downloading`, or `failed` state. It must not download automatically when the
package becomes ready. The POST commits a durable `preparing` snapshot before
returning HTTP 202. A dedicated single-concurrency package worker claims committed
rows through a database-global lock; per-project preparing uniqueness, a bounded
global queue, and a package-volume byte quota prevent request-driven fan-out.
Before opening a worker business Session or touching package storage, the package
worker must acquire a runtime participant lease on a dedicated PostgreSQL
connection. That lease holds the database transaction fence in shared session mode
and is valid only after the same backend PID is observed holding both the writer
lock and the current storage-root contract lock in exclusive mode. Every worker
commit is fenced through the bound runtime context. Missing ownership, lost lease
identity, or unconfirmed same-connection release fails closed without publication,
failure-state writes, or physical cleanup. SQLite has no delivery exemption; it is
allowed only when `ALLOW_SQLITE_FOR_TESTS=true`.
Quota accounting includes every package whose physical storage reclamation has
not been confirmed, including terminal `failed` rows. A build failure may clear
its reservation only after the random candidate/ZIP is safely removed or proven
absent through the managed no-follow path contract; an unlink failure retains the
conservative reservation for retry. Migration `20260713_0011` backfills legacy
failed rows with a conservative ZIP-overhead reservation. A damaged reusable
`ready` ZIP remains fully charged during replacement admission; queued post-commit
deletion is not reclaim evidence and cannot be subtracted before identity-bound
unlink succeeds and the reclaim accounting transaction commits.
Before ZIP I/O, the worker commits a durable attempt claim and identity-bearing delayed lease while
holding a session advisory lock on one dedicated physical PostgreSQL connection;
business-session commits cannot return that lock connection to the pool, and unlock
must succeed on the same connection. The claim transaction then closes before ZIP
creation, source hashing, output hashing and `fsync`. Publication uses a new short
row-locked transaction and succeeds only for the same build lease identity; a worker
from a superseded lease cannot publish or delete the replacement lease's output.
Timeout, unexpected exception, or process death
therefore leaves a bounded next-at retry instead of an immediately eligible poison
row, so later packages advance. A caught failure recomputes `next_build_attempt_at`
from the failure-recording time instead of retaining a claim-time delay that may
already have expired during long ZIP I/O. Unsafe managed paths and contract/state failures are
terminal without retry. Exhausted caught failures and expired max-attempt
claims become terminal `failed`; process restart alone never creates a duplicate
package.
A `preparing` response is polled through the package GET
with both a bounded poll count and a 10-second per-request timeout; `failed`,
`expired`, or a timeout never produces a ready UI state. Only a `ready` snapshot
carries `sha256`, `download_token`, and `download_token_expires_at`. The frontend
removes an unused token at that expiry and never caches failed/expired tokens.
Browser-native download exchanges the short-lived token
through `X-Package-Download-Token` at `download-session`; the backend returns a
120-second, path-scoped, HttpOnly, SameSite=Strict one-shot cookie. The following
native GET atomically consumes that session into one active database lease before
hashing or streaming. The streaming response renews that exact lease identity at
a bounded interval until close or failure; renewal failure terminates the stream,
and response close/failure stops the heartbeat and releases only its own lease.
An obsolete lease cannot renew a replacement lease. Replay and concurrent downloads
are rejected, and a short completion cooldown applies. The token must never be placed in a URL query string,
response URL, log, evidence file, or DOM state because those surfaces can be
recorded. When the request arrives from a configured trusted proxy, the proxy
must provide a valid `X-Forwarded-Proto` value; a missing or invalid value is
rejected rather than issuing a potentially non-Secure cookie for a TLS request.
Idempotency records for `PrepareFinalizedPackage` never persist either token
field. A replay resolves the referenced package again and signs a new token only
while the package remains ready and unexpired; an expired replay returns
`PACKAGE_EXPIRED`.

File upload API:

```http
POST /api/v1/files/uploads/init
PUT  /api/v1/files/uploads/{upload_id}/parts/{part_no}
GET  /api/v1/files/uploads/{upload_id}
POST /api/v1/files/uploads/{upload_id}/complete
POST /api/v1/files/uploads/{upload_id}/abort
```

Browser `File.type` is advisory. A client may normalize an empty or platform-specific MIME for an allowed `.mp4`, `.m4v`, `.mov`, or `.qt` name, but the backend remains authoritative: it validates the allowed extension/MIME pair, a bounded structurally complete ISO-BMFF `ftyp` box, and then a restricted ffprobe result containing exactly one valid video stream with positive duration, dimensions, and frame rate. A short hard-coded brand allowlist is not a security boundary and must not reject an otherwise valid ISO-BMFF video before ffprobe; malformed or forged containers still fail closed.

Binary PUT responses use the normal JSON envelope. Safari/Chrome clients must report upload-byte progress while the body is being sent and may choose smaller parts dynamically, provided every part remains within the configured byte ceiling and the file uses no more than 256 parts. A network error, timeout, abort event, malformed envelope, or lost response is an uncertain result: the client retains the upload id and retries the same part. It must not create a replacement session or call server abort unless the operator explicitly abandons that upload.

`part_no` is bounded to `1..256`, and deployment configuration may lower but
never raise that ceiling; the server rejects an out-of-range part before
creating or writing a staging file. Before reading a PUT body, the server uses
an independent short database session to validate upload identity, owner, status,
received parts and the remaining declared byte allowance under a short row lock,
renews the session activity timestamp, commits, and closes the connection before
reading the request body.
Known Content-Length overflow is rejected before creating a candidate, and the
stream writer's byte cap is no larger than that remaining allowance. The session
then closes before body I/O.
After the body is durable it takes the row lock and repeats the authoritative
owner/status/part validation before publishing metadata. Body read, file write,
flush and `fsync` share one total timeout and run through a bounded dedicated I/O
executor; principal/session/process admission bounds the executor queue.

Upload init requires a retry-stable `Idempotency-Key`; the reservation, upload
session and completed idempotency response commit atomically, and an ambiguous
commit is recovered through an independent read. It serializes PostgreSQL quota
decisions with a transaction advisory lock and persists a peak reservation of
twice the declared size, covering
simultaneous part files and the completed staging file. Filesystem low-water
admission uses the same peak. Global and principal session
and byte quotas count initiated, receiving and finalizing uploads plus completed
or aborted sessions whose part-file cleanup is not yet confirmed. Reservation is
released only after every referenced part is safely removed or confirmed absent.
Init also fails closed at the configured filesystem low-water mark. Complete
commits a short `finalizing` lease, releases the database connection for
multi-gigabyte concatenate/hash/ffprobe/fsync work, then publishes through a new
short row-locked transaction. An expired lease can be reclaimed only with the
same hashed idempotency key, canonical completion-request hash and deterministic
final file id. A different key or request hash returns `IDEMPOTENCY_CONFLICT`
and cannot take over the lease; an obsolete worker cannot publish after a newer
lease wins. Failed completed/aborted part cleanup is
retried after a bounded delay without releasing reservation; a completed row
records cleanup confirmation only after all referenced parts are absent. A
finalizing row may enter abort cleanup only after both its lease has expired and
the upload TTL has elapsed. Lease expiry opens takeover but does not invalidate the
current publisher until a replacement lease wins. Orphan cleanup treats every
active upload `finalization_file_id` and published `file_id` as a live reference.
If claim commit acknowledgement and its first independent observation are both
unavailable, an immediate retry may resume the still-active lease only when the
principal fingerprint, idempotency key hash and canonical request hash all match;
it reuses the same lease and deterministic file ID. A different identity remains
rejected. Competing exact retries converge through exclusive canonical-file
publication and the row-locked completion transaction.
The configured upload TTL must exceed the total PUT body timeout plus cleanup
safety margin.
For a no-account LAN profile in which clients share one trusted principal, the
configured principal session quota and PUT admission limit are each at least 10.
The delivery admission tuple is `16/1/64`: 16 PUTs for distinct upload ids may
share that principal, but a single upload id can own only one in-flight candidate.
Thus a same-session retry is rejected before request-body I/O and candidate
creation without reducing the required 10-client capacity.
The inactivity TTL for initiated/receiving sessions is strictly less than one day and
strictly greater than the PUT timeout plus safety margin. Byte reservations and filesystem
low-water admission remain authoritative, so concurrency never implies capacity
beyond the host's real free storage.

Current Compose delivery is intentionally one backend replica with one Uvicorn
worker because PUT admission is process-local. Horizontal workers or replicas
require an external distributed admission coordinator before these topology
guards may be changed. Package preparation also fails with
`FILE_TOO_LARGE` before archive creation when configured file-count or total
original-byte limits are exceeded. Video originals are stored in ZIP without
recompression.

`abort` only terminates incomplete temporary upload sessions. It does not delete already bound business files.

No DELETE route is registered. Unknown DELETE returns HTTP 405.

## Headers

| Header | Use |
| --- | --- |
| `X-Request-ID` | request tracing |
| `Idempotency-Key` | create and conclusion commands |
| `If-Match` | optimistic-lock updates |
| `Content-Type` | JSON or upload protocol |

Headers must not provide trusted capability or principal data.

## Error Registry

Required error codes:

```text
VALIDATION_ERROR 422
RESOURCE_NOT_FOUND 404
ENTRY_CAPABILITY_DENIED 403
PRINCIPAL_PERMISSION_DENIED 403
WRITE_GUARD_REQUIRED 403
WRITE_GUARD_INVALID 403
RESOURCE_STATE_CONFLICT 409
PORT_OPERATION_NOT_SUPPORTED 409
PLAYBACK_NOT_READY 409
VERSION_NOT_CURRENT 409
REVIEW_IN_PROGRESS 409
REVIEW_ITEM_FINALIZED 409
UNRESOLVED_ISSUES_EXIST 409
NO_UNRESOLVED_ISSUE 409
VERSION_FILE_NOT_READY 409
FILE_HASH_MISMATCH 409
UPLOAD_INCOMPLETE 409
IDEMPOTENCY_CONFLICT 409
OPTIMISTIC_LOCK_CONFLICT 409
PACKAGE_NO_FINALIZED_FILES 409
PACKAGE_SOURCE_MISSING 409
PACKAGE_NOT_READY 409
PACKAGE_EXPIRED 410
FILE_TYPE_NOT_ALLOWED 422
FILE_TOO_LARGE 413
STORAGE_UNAVAILABLE 503
```

Parent-child mismatch returns `RESOURCE_NOT_FOUND`.

## Events

Event envelope fields:

```text
eventId
eventType
eventVersion
occurredAt
aggregateType
aggregateId
aggregateVersion
sequence
projectRefId
reviewItemId?
versionId?
issueId?
finalizationId?
packageId?
correlationId
causationId?
metadata.entrySource
metadata.principalKind
metadata.principalId?
metadata.requestId
payload
```

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

Outbox event writes are transactional with business data. Consumers use `event_id` idempotency and must call commands to mutate review data.

## Operation Log Contract

`operation_logs` is an internal audit record, separate from domain events and
HTTP access logs. V1 collects one record for each registered final-cut-review
command route that reaches a success or failure decision, including route-level
request validation once the fixed command type is known. Query, health, media
stream, file-transfer, download-session, and write-guard-session endpoints are
outside this collection boundary unless a later reviewed contract explicitly
adds them. A successful record commits in the business transaction. A rejected
or failed command is rolled back first and its audit record is committed through
an independent session; failure of that secondary write is reported without
replacing the original command error.

| Field | Contract |
| --- | --- |
| `id` | Internal auto-incrementing row identifier. |
| `request_id` | Bounded request correlation ID, maximum 64 characters; never an operation deduplication key. |
| `entry_source` | `edit`, `review`, `embedded`, or `unspecified`. |
| `command_type` | Fixed server-side command name, maximum 128 characters. |
| `capability` | Server-mapped capability, maximum 128 characters, nullable. |
| `principal_kind` | Server-derived principal kind, maximum 32 characters. |
| `principal_id` | Server-derived principal identifier, maximum 128 characters, nullable. |
| `client_ip` | Observed client address, maximum 64 characters, nullable. |
| `user_agent` | `sha256:<lowercase hex>` fingerprint of the observed User-Agent, nullable; the raw header is never stored. |
| `idempotency_key_hash` | Lowercase SHA-256 hex of the accepted idempotency key, exactly 64 characters, nullable; the raw key is never stored. |
| `operation_identity_hash` | Lowercase SHA-256 identity derived from command identity when available, plus command type, resource and principal; exactly 64 characters for current writers and nullable only for legacy rows. |
| `resource_type` | Deepest resource type derived from trusted route parameters, maximum 32 characters; `request` when no resource is identified. |
| `resource_id` | Corresponding route resource ID, maximum 128 characters, nullable. |
| `result` | `ok`, `error`, or `unknown`; `unknown` means commit was attempted but no committed `ok` outcome can be proven. |
| `error_code` | Stable public error code, maximum 64 characters, nullable. |
| `failure_stage` | Bounded processing stage for failed attempts, maximum 32 characters, nullable. |
| `created_at` | Database creation timestamp. |

Migration `20260714_0015` backfills pre-existing rows with
`command_type=LegacyOperation`, `principal_kind=anonymous`, and
`resource_type=request`. Migration `20260714_0016` installs matching persistent
server defaults on both fresh and already-upgraded databases so a controlled
rollback to the previous application image remains write-compatible. Current
writers provide explicit attribution and never rely on the defaults. Migration
`20260714_0017` adds `operation_identity_hash`, permits `result=unknown`, and
enforces one concurrent `unknown` row per non-null operation identity. An uncertain
writer and the original success transaction serialize on the same identity-scoped
transaction advisory lock before the uncertain writer checks for committed `ok`; a
visible `ok` suppresses `unknown`, while conflicting uncertain inserts are reduced
to exactly one by the database unique index. Pre-commit failures remain `error`. A rolling downgrade must
quiesce `0017` writers before schema downgrade; downgrade conservatively maps every
remaining `unknown` row to `error` with `COMMIT_OUTCOME_UNKNOWN`, then removes the
identity column/index and restores the `ok|error` check. This mapping communicates
uncertainty to the old schema and is not evidence that the command failed.

Collection is metadata-only. The operation log must never contain a command or
request payload, response body, comment or annotation content, filename,
physical path, raw idempotency key, `Authorization` value, cookie, write-guard
code/session value, download/package token, account token, secret, full URL,
query string, raw User-Agent, or raw exception text. Values that are admitted are
normalized to printable text and truncated to the limits above. The runtime role
may select and insert operation rows, but cannot update, delete, or truncate them.
Do not copy operation-log rows
into test evidence or support reports without a separate redaction review.

V1 has no automatic operation-log TTL or cleanup job. Rows survive project,
item, issue, version, file, and package lifecycle deletion and follow the
database backup/restore retention boundary. Deletion or archival requires an
explicit operator retention procedure and a reviewed schema/policy change.
There is no browser, host-bridge, or public HTTP list/read/export contract.
Database reads are limited to roles explicitly granted `SELECT`; the current
split-role deployment grants database access only to the non-owner runtime and
owner/migrator or administration roles. That database grant does not create an
application capability, and direct human reads are operator-only.

## Port Contracts

Required implementation ports:

- `ProjectCatalogPort`
- `FinalCutReviewQueryPort`
- `ReviewCommandPort`
- `EntryPolicyPort`
- `WriteGuardPort`
- `PrincipalAuthorizationPort`
- `ReviewRepositoryPort`
- `FileStoragePort`
- `MediaPort`
- `FinalizedPackagePort`
- `EventOutboxPort`
- `OperationLogPort`
- `ReviewHostBridge`

SPEC current access-control adapters are:

- `StaticEntryPolicyAdapter`
- `NoAccountAuthorizationAdapter`
- `NoWriteGuardAdapter`
- `SharedCodeWriteGuardAdapter`
- `ReverseProxyWriteGuardAdapter`

The runnable local frontend may additionally wrap those roles with demo adapters:

- `MockReviewApiAdapter`
- `InMemoryReviewRepository`
- `MockFileStorageAdapter`
- `MockFinalizedPackageAdapter`
- `NoAccountPermissionAdapter`
- `SimpleWriteGuardAdapter`

Mocks must enforce the same invariants where the demo exercises them.

## File And Package Contracts

File roles:

```text
project_cover
review_original
playback_proxy
thumbnail
package_temp
```

`OriginalMediaSnapshot` freezes original file ID, filename, MIME, file size, SHA-256, duration, width, height, `fpsNum`, `fpsDen`, and media probe version.

`PlaybackStatus` values are `processing`, `ready`, and `failed`.

Single finalized download returns original file only, with HTTP Range support and no permanent public URL. The HTTP frontend starts a browser-native download and must not materialize the original as a JavaScript `Blob`.

Project package includes only active finalization original files for the current project. Package snapshot freezes review item, version, original file, filename, hash, package filename, and the completed ZIP SHA-256. Source missing or hash mismatch fails the whole package. Package creation uses exclusive no-follow output creation, streams each pinned regular source into the archive, verifies every source hash, hashes the completed ZIP through the same output descriptor, flushes and `fsync`s the package and parent directory, and never overwrites an existing path. Original and package downloads verify the expected digest through the same pinned descriptor that is streamed to the browser; a replacement or mismatch returns `FILE_HASH_MISMATCH`. Maintenance marks an expired package row `expired` only after the package file was removed or already absent; unlink failures retain the prior state for retry.

## Module Manifest And Host Bridge

Manifest:

```text
manifestVersion: 1
moduleId: final-cut-review
contractVersion: 1.0
standaloneRoutes.edit: /edit
standaloneRoutes.review: /review
mountSlots: workspace.main
requiredHostServices: []
optionalHostServices:
  project_catalog
  principal_context
  authorization
  http_client
  event_bus
  file_service
  portal_root
  theme
```

`ReviewHostBridge` exposes `mount`, `unmount`, optional context change subscription, optional project catalog, optional principal context, optional authorization adapter, optional HTTP client, optional event bus, navigation, portal root, and theme tokens.

Embedded mode must not change core domain rules.

## Precise Playback Contracts

Required contract types:

```text
ReviewFrameRate: fpsNum, fpsDen
ReviewPlaybackTarget:
  projectRefId
  reviewItemId
  versionId
  issueId
  revisionId
  annotationSetId?
  timestampMs
  frameNumber
```

Playback target must be contract data, not UI-local inference.

Forbidden playback locators:

```text
timeMs only
current selected version
current player URL
array index
display version number
filename
rendered timecode text
```

Frame math:

```text
frame_number = floor(timestamp_ms * fps_num / (1000 * fps_den))
timestamp_ms = floor(frame_number * 1000 * fps_den / fps_num)
```

Required pure functions:

```text
frameFromTimestampMs(timestampMs, fpsNum, fpsDen)
timestampMsFromFrame(frameNumber, fpsNum, fpsDen)
formatReviewTimecode(frameNumber, fpsNum, fpsDen)
```

Required frame rates:

```text
24/1
25/1
30/1
24000/1001
30000/1001
```

MVP does not implement SMPTE Drop Frame. Variable-frame-rate files only promise review-timeline frame precision, not source packet PTS equivalence.

Precise playback flow:

```text
issue card / timecode / timeline marker / previous / next
-> ReviewPlaybackTarget
-> full ancestry validation
-> switch to target version when needed
-> wait target data
-> wait playback_ready
-> loadedmetadata / canplay
-> verify media belongs to target version
-> seek real HTMLVideoElement
-> seeked
-> optional requestVideoFrameCallback
-> pause
-> render selected current Revision AnnotationSet only
-> highlight selected issue and marker
```

Precise playback must not:

- Overlay V1 issue or coordinates on V2.
- Map V1 timecode to V2.
- Infer whether a V1 issue is fixed in V2.
- Show annotations from another issue, version, or old revision.
- Use fixed `setTimeout` in place of media events.
- Allow stale media events or stale queries to override the latest request.

Auto-pause applies only to current-version unresolved issues during natural playback. It does not trigger for historical issues, resolved issues, or manual seek misclassification.

## Contract Test Requirements

Required tests:

- OpenAPI and JSON Schema generation.
- Capability profile route mapping for `/edit` and `/review`.
- No general-purpose HTTP DELETE capability or endpoint. The only deletion
  commands are the review-entry project tombstone, current-version issue
  tombstone, and the edit-entry pre-review duplicate-item physical-delete
  exception defined by this contract.
- Unified envelope success/list/error shape.
- Forbidden trusted security fields rejected or ignored.
- Full-context query ancestry validation.
- Idempotency conflict and optimistic-lock conflict.
- Error registry code/status mapping.
- Event schema compatibility.
- Package snapshot immutability.
- Precise playback target validation.
- Frame/timecode functions for required frame rates.
- Coordinate conversion for 9:16 and 16:9 videos, black bars, DPR 1/2, 1920 and 1366 viewports, and full screen.
- Current-version playback, historical-version switch-then-playback, rapid-click race, V1 marker isolation from V2, and current-version auto-pause.

## Backend Contract Source And Generated Artifacts

The backend uses `contracts/final-cut-review/v1` as the single contract source:

```text
contracts/final-cut-review/v1/
├── openapi.yaml
├── capabilities.yaml
├── errors.yaml
├── commands/*.json
├── queries/queries.yaml
├── events/events.yaml
└── module-manifest.json
```

`backend/scripts/generate_contracts.py` validates that:

- contract version is `1.0`;
- every command capability exists in the capability registry;
- the module manifest capability list matches the registry;
- OpenAPI contains no DELETE path operation.

Generated artifacts:

```text
backend/app/modules/review_contracts/generated.py
src/modules/final-cut-review/contracts-generated/backend-contract.ts
```

All external JSON uses snake_case. HTTP success and error responses use the unified envelope:

```json
{
  "data": {},
  "meta": {
    "contract_version": "1.0",
    "request_id": "11111111-1111-4111-8111-111111111111"
  }
}
```

```json
{
  "error": {
    "code": "RESOURCE_STATE_CONFLICT",
    "message": "当前状态不允许执行此操作",
    "http_status": 409,
    "details": {},
    "request_id": "11111111-1111-4111-8111-111111111111",
    "timestamp": "ISO-8601",
    "contract_version": "1.0"
  }
}
```

Write endpoints accept `CommandEnvelope` and reject route/command mismatches. `Idempotency-Key` must match `command_id` when present. Updates use `If-Match` as the expected `lock_version`.

Implemented backend route groups:

- Shared reads: `/api/v1/final-cut-review/projects...`
- Edit facade: `/api/v1/final-cut-review/edit/...`
- Review facade: `/api/v1/final-cut-review/review/...`
- Uploads: `/api/v1/files/uploads/...`
- Manifest: `/api/v1/final-cut-review/module-manifest`
- Write guard session: `/api/v1/final-cut-review/write-guard/session`

No DELETE endpoint is registered.

## 2026-07-09 Issue Soft-delete Contract

- Command: `SoftDeleteReviewIssue`
- Capability: `review.issue.delete`
- Review facade route: `POST /api/v1/final-cut-review/review/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/issues/{issue_id}/soft-delete`
- Payload: `project_ref_id`, `review_item_id`, `version_id`, `issue_id`
- Locking: `If-Match` is required and must match the issue `lock_version`.
- Event: `review.issue.deleted`
- Semantics: soft delete only. `review_issues.deleted_at` is set; issue revisions, annotations, thread messages, outbox and operation log records are retained. Shared read endpoints hide soft-deleted issues and return `RESOURCE_NOT_FOUND` for direct issue reads.
- Boundary: only review entry can execute the command; edit entry and historical/non-current versions cannot delete issues. No HTTP DELETE operation is added.

## 2026-07-09 Pre-review Duplicate Item Delete Contract

- Command: `DeleteReviewItem`
- Capability: `review.item.delete`
- Edit facade route: `POST /api/v1/final-cut-review/edit/projects/{project_ref_id}/items/{review_item_id}/delete`
- Payload: `project_ref_id`, `review_item_id`, `confirmed: true`
- Locking: `If-Match` is required and must match the item `lock_version`.
- Event: `review.item.deleted`
- Semantics: physical delete only for duplicate cleanup before review starts and only after an explicit second confirmation. The command deletes the `review_items` row, its single unreviewed `review_versions` row, and the unreferenced file object plus storage-root-contained blob. An upload session whose part cleanup is already confirmed and empty may be deleted in the same transaction. Otherwise its `file_id` is detached while `received_parts`, cleanup state and reserved bytes remain until maintenance confirms physical part removal; deletion must never release quota or orphan a failed cleanup. Outbox and operation log records remain, with foreign-key pointers detached from the deleted item/version.
- Boundary: runtime must enforce `pending_review`, exactly one version, zero issues, zero finalization records, and no `active_finalization_id`. Existing outbox events for the deleted item must detach `review_item_id`, `version_id`, `issue_id`, and `finalization_id` foreign-key columns while preserving aggregate id, sequence, request/correlation metadata, and event payload. No HTTP DELETE operation is added.
