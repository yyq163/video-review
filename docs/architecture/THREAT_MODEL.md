# THREAT MODEL

Baseline source: `FJ_Final_Cut_Review_SPEC_V1.3_Reviewed.md`.
Threats are evaluated against SPEC V1.3 module boundaries, not only the current browser mock.

## Scope

In scope:

- Standalone `/edit` and `/review` entries.
- Embedded host mode through `ReviewHostBridge`.
- No-account current mode and future principal authorization adapters.
- Optional write guard modes: `none`, `shared_code`, and `reverse_proxy`.
- Project catalog adapters.
- Review items, versions, issues, revisions, annotation sets, thread messages, decisions, finalizations, packages, and domain events.
- Upload sessions, media probe, playback stream, original downloads, and temporary ZIP packages.
- Precise annotation playback and coordinate restoration.

Out of scope for V1:

- Login, account management, member/role UI, notification center, task center UI, delivery center UI, download center, revoke/overwrite flows, cross-version issue tracking, automatic fix judgment, and permanent public file links.

## Trust Boundaries

- Browser and client storage are untrusted.
- Entry URL determines only `entry_source`; it is not identity.
- `ExecutionContext` is created server-side.
- `ProjectCatalogPort`, `PrincipalAuthorizationPort`, `WriteGuardPort`, `FileStoragePort`, `MediaPort`, `FinalizedPackagePort`, and `ReviewHostBridge` are adapter boundaries.
- File IDs are the only allowed media references across API boundaries. Physical paths are internal infrastructure data.
- Host-injected project catalog, authorization, HTTP, event bus, portal root, file service, and theme are optional services and must not change core domain rules.

## Threats And Required Controls

| Threat | Control |
| --- | --- |
| Client spoofs capability, role, principal, or write-guard status | Reject trusted fields in request body/header; server creates `ExecutionContext`; route facade maps capability. |
| Entry confusion between `/edit` and `/review` | Separate thin write facades inject fixed `entry_source`; both call same command handlers; EntryPolicy enforces profile intersection. |
| `shared_code` leakage or verification DoS | Treat the optional shared write code as a deployment write guard, not a user password. Send it only to the verification endpoint; issue a short-lived HttpOnly SameSite cookie; never store code/hash in localStorage, sessionStorage, DB, logs, errors, or responses. Bound the field/configuration to 256 characters and the request body to 4 KiB before validation; expire stale failure keys and cap active keys at 4096. |
| No-account mode treated as authorization bypass | `NoAccountAuthorizationAdapter` only means no account dimension; EntryPolicy, WriteGuard, ancestry checks, and state machine still run. |
| Cross-project or cross-item data leakage | Every command/query carries and validates full ancestry; mismatched parent-child relation returns `RESOURCE_NOT_FOUND`; DB, repository, and application service all enforce ownership. |
| Cross-version issue or annotation leakage | Issues, revisions, messages, and annotation sets are keyed by `version_id`; V2 starts with empty issue/annotation sets; historical unresolved issues do not block current finalization. |
| Unauthorized edit/review write action | CapabilityGate is UX-only; server rejects missing capability with `ENTRY_CAPABILITY_DENIED` or future `PRINCIPAL_PERMISSION_DENIED`. |
| State-machine bypass | Domain state machine blocks non-ready playback writes, `in_review` version upload, finalized item writes, finalization with unresolved current-version issues, and request changes without unresolved issues. |
| Hidden destructive path | No HTTP DELETE route is registered. Project and issue deletion are explicit review-entry soft-delete commands; item physical deletion is an edit-entry command restricted to one unreviewed version, zero issues/finalization and `confirmed: true`. Business FKs use RESTRICT and outbox/audit history is retained. |
| Physical item delete releases upload quota before part cleanup | Delete an upload row only when cleanup is confirmed and no parts remain. Otherwise detach its file binding and retain part references, cleanup state and reservation for maintenance retry. |
| File path exposure or traversal | API uses File ID only; normalize paths internally; do not expose storage directories through Nginx; reject path traversal. |
| Malicious upload | Validate MIME, extension, magic bytes, size, SHA-256, upload completion, and media probe result before creating a review version. |
| Managed-root symlink substitution or probe TOCTOU | Reopen every managed-root path component with directory FDs and `O_NOFOLLOW`, reject cross-device traversal, create outputs with `O_EXCL`, and retain the original read/write FD through hashing, ffprobe and publication. Verify the staging and published device/inode against that FD, and clean up only names still bound to the validated inode. Never close and reopen an untrusted pathname between validation and probe/publication. |
| Manifestless delete-quarantine directory name rebinding | Never remove a manifestless quarantine directory by pathname. A locked directory FD proven empty is a benign retained shell and does not degrade maintenance; any remaining file, directory, symlink, or unverifiable state still fails closed. This avoids an unavoidable check-to-`rmdir` name-rebinding window because POSIX provides no directory-FD-bound removal operation. |
| Playback proxy confused with original | Review playback may use proxy, but finalization and download always use `original_file_id`. |
| Finalization drift | `FinalizationRecord` freezes version and original media snapshot; active finalization is single per item. |
| Package drift or partial package | Package snapshot freezes file list/hash/filename in a transaction; build reads snapshot only; any missing or hash-mismatched source fails the whole package. |
| Package poison row, worker crash, or package-volume exhaustion | Commit a bounded attempt claim and identity-bearing delayed lease before ZIP I/O, close the business Session, pin one session advisory lock to a dedicated physical PostgreSQL connection, build/hash/fsync without a row lock, publish in a fresh short transaction only for the same lease, fail expired max-attempt claims, skip not-yet-due rows, enforce queue/file/source-byte and estimated-then-actual ZIP-volume quotas, and isolate maintenance failures. |
| Damaged ready ZIP is discounted before physical deletion | Count its full physical bytes during replacement admission until identity-bound unlink succeeds and reclaim accounting commits. A queued or failed post-commit delete never releases quota. |
| Stale package worker publishes or deletes a replacement lease output | Persist a random build lease identity, reject publication after takeover, and delete abandoned output only when its device/inode still matches the stale worker artifact. |
| Active package download outlives its cleanup lease | Renew the exact lease identity during digest verification and before streamed chunks; renewal failure aborts the stream, while close/error stops the heartbeat before identity-bound release. Maintenance skips every active renewed lease. |
| Upload reservation leak after process crash or cleanup failure | Persist global/principal reservations, retain them until physical part cleanup is confirmed, retry completed/aborted cleanup after bounded backoff, and convert only TTL-stale finalizing rows with expired leases into abort cleanup. |
| Upload disk overcommit or stale maintenance race during PUT/finalize | Reserve twice declared bytes for parts plus completed staging, enforce low-water against the peak, cap PUT by remaining declared bytes before candidate creation, commit a short activity renewal before body I/O, require TTL greater than body timeout plus margin, and treat active finalization/published file IDs as orphan-scan references. |
| Different operation takes over an expired upload finalization lease | Persist hashed idempotency key and canonical request hash with the lease; permit takeover only when both match and return `IDEMPOTENCY_CONFLICT` otherwise. |
| Range I/O amplification | Full downloads verify a digest on the streamed pinned FD; partial Range responses rely on immutable published identity and startup audit and must not pre-hash the whole source for a small range. |
| Permanent public download URL | Use short-lived download/package token only; no permanent public URL and no download center/history list in V1. |
| Optimistic-lock lost update | Mutating aggregate requests require expected version through `If-Match` or command envelope; conflict returns `OPTIMISTIC_LOCK_CONFLICT`. |
| Idempotency replay or mutation conflict | Required commands use `Idempotency-Key`; same key/body returns prior result; same key/different body returns `IDEMPOTENCY_CONFLICT`. |
| Transaction split leaves inconsistent state | Required multi-entity operations are single transaction with outbox where applicable. |
| Event consumer mutates core directly | Consumers are idempotent by `event_id`; mutations must call formal commands. |
| Operation log leaks payloads, secrets, or identifiers beyond the audit purpose | Collect only bounded server-derived command metadata. Never store request/command payloads, response bodies, comment or annotation content, filenames, physical paths, raw idempotency keys, authorization headers, cookies, write-guard values, download/package tokens, account tokens, secrets, full URLs/query strings, or raw exception text. Store only the SHA-256 idempotency-key digest. |
| Operation log becomes a general request-surveillance stream | Limit collection to the reviewed command-route map and identified command validation/failure stages. Query, health, media stream, upload-transfer, download-session, and write-guard-session traffic remains out of scope unless a later contract explicitly opts it in. |
| Operation log is exposed or silently erased | Expose no browser, host-bridge, or public HTTP read/list/export API. Require explicit database `SELECT` for direct reads, keep human access operator-only, retain rows across business deletion and backup/restore, and add no automatic TTL/cleanup without a reviewed retention change. Successful rows share the business transaction; failed commands write through an independent short audit transaction and report a bounded diagnostic if that audit write fails. |
| Reused request ID or concurrent commit failures suppress/duplicate uncertain audit | Treat request ID as correlation only. Bind the operation identity hash to command identity, command type, resource and principal; serialize success and uncertain completion on one identity-scoped transaction advisory lock, check committed `ok` after acquiring it, and enforce one non-null-identity `unknown` row with a database partial unique index. Pre-commit failures remain `error`. |
| Rolling downgrade loses uncertain-outcome meaning | Quiesce new writers before downgrading migration `20260714_0017`; map residual `unknown` rows to `error` with `COMMIT_OUTCOME_UNKNOWN` before restoring the legacy check. Operators must treat the mapped row as unresolved, never as proof of rollback. |
| Runtime configuration or backup leaks credentials | Private Compose env must be a regular non-symlink file with no group/other permission bits. Secret sources are read from stable no-follow descriptors into fixed, mode-0600 snapshots under an ignored, owner-only project directory that survives container restart; failed temporary snapshot writes are removed. Wrappers never source or print credentials into evidence; backups and restored test databases remain local with restrictive permissions. |
| Container environment or inspection leaks database/write-guard secrets | Inject delivery credentials through service-scoped Compose secrets; container environments carry only `*_FILE` paths. Secret readers reject ambiguous direct/file sources, symlinks, non-regular/empty/oversized/multiline/non-UTF-8 files, and never include values in errors. |
| Root ownership initialization leaves broad Linux capabilities on application containers | Drop all capabilities on migrate/backend/maintenance/package-worker and add only `DAC_READ_SEARCH`/`CHOWN` for restricted legacy-tree traversal and ownership repair plus `SETGID`/`SETUID` for `gosu`; require UID/GID 10001 and an empty effective capability set after exec. |
| Runtime database role escalates to DDL or owner | Keep admin, owner/migrator, and runtime roles distinct. Owner and runtime are non-superuser, cannot create databases/roles, use `NOINHERIT`, and have no memberships. Only the one-shot migrate service receives privileged credentials. Database, schema, tables, sequences and migration history belong to owner; runtime gets only `CONNECT`, schema `USAGE`, table DML and required sequence grants, with database/schema `CREATE` and `TEMP` revoked. |
| Runtime corrupts migration history or sequence state without DDL | Revoke all DML on `alembic_version` while retaining `SELECT`; grant sequences only `USAGE, SELECT`, never `UPDATE`, so runtime cannot call `setval()`. Apply the same default sequence privileges and verify with real role tests. |
| Database/volume identity mismatch after restore or cutover | Keep ownership state in a root-owned volume, bind one canonical storage root to one database, perform startup association audit, require explicit restore revalidation, and prove a fresh source/restore database pair preserves business rows, upload metadata and a real data-volume blob association before cleanup. |
| Two runtime writers bind one database to different managed roots | Hold a database-scoped session advisory writer lock on one dedicated physical connection for the full process lifespan, bind its identity to the canonical roots, and fail startup before serving when the lock cannot be acquired. |
| Maintenance or package worker bypasses the active storage-root owner or races root takeover | Before each worker database/storage cycle, hold the transaction fence as a shared session advisory lock on a dedicated participant connection, then require one backend PID to own both the database writer and current root-specific contract locks exclusively. Bind all worker Session commits to that participant through the `before_commit` fence, confirm shared unlock on the acquiring connection, and let an old participant or in-flight worker transaction block every new exclusive takeover until it releases. Missing ownership fails before writes or physical deletion; SQLite is test-only behind `ALLOW_SQLITE_FOR_TESTS=true`. |
| XSS in comments or text annotations | Render user text as text, not HTML; apply output escaping and CSP. |
| Browser object URL leak | Revoke generated URLs during cleanup when practical. |
| Stale async response overwrites newer context | Project/item/version switches cancel old requests/uploads and validate IDs before writing state. |
| Stale playback media event overwrites latest target | Every precise playback request has a sequence/request id; old `loadedmetadata`, `canplay`, `seeked`, frame callbacks, and query responses are ignored. |
| Wrong-version precise playback | `ReviewPlaybackTarget` includes project/item/version/issue/revision/annotation IDs; history issue click must switch to its version before seek. |
| Wrong-frame playback | Use frozen `fps_num/fps_den` from target `ReviewVersion.originalMedia`; do not infer from display timecode or filename; tolerate at most one review frame in browser seek. |
| Annotation leakage during playback | Render only selected issue + current revision + current annotation set + current version. Do not flat-map all annotations or filter only by timestamp. |
| Black-bar coordinate drift | Capture and replay relative to contained video rectangle, not the player stage or black bars; test 1920 and 1366, full screen, DPR 1/2, pillarbox and letterbox. |
| Host context confusion | On host context change, cancel old requests, clear playback/annotations, reload new context, and recalculate capabilities without changing domain model. |
| Deployment exposure | Prefer private network/VPN/trusted gateway and TLS; clean trusted proxy headers; set `X-Content-Type-Options: nosniff`; do not expose object storage directly. |

## Required Error Behavior

- Parent-child mismatch must be indistinguishable from absence and return `RESOURCE_NOT_FOUND`.
- State conflicts return SPEC error codes such as `RESOURCE_STATE_CONFLICT`, `PLAYBACK_NOT_READY`, `VERSION_NOT_CURRENT`, `REVIEW_IN_PROGRESS`, `REVIEW_ITEM_FINALIZED`, `UNRESOLVED_ISSUES_EXIST`, `NO_UNRESOLVED_ISSUE`, `FILE_HASH_MISMATCH`, `UPLOAD_INCOMPLETE`, `PACKAGE_SOURCE_MISSING`, `PACKAGE_NOT_READY`, and `PACKAGE_EXPIRED`.
- Unsupported adapter operations return `PORT_OPERATION_NOT_SUPPORTED`.
- Storage outages return `STORAGE_UNAVAILABLE`.

## Security Acceptance Checks

- Client-forged `capabilities`, `principal_id`, and `write_guard_verified` are ignored or rejected.
- `/edit` can only resolve current-version issues; it cannot create/update/reopen issues, start review, request changes, finalize, or package.
- `/review` can create/update/reopen current-version issues but cannot resolve them, create/update projects, or upload versions; archive/restore is explicitly allowed by the product contract. Both current entry profiles reject legacy start-review and request-changes commands.
- History unresolved issue does not block current version finalization.
- Single download and package download return originals, not proxies.
- Package content does not drift after snapshot creation.
- Package claims survive worker interruption, retries are delayed and bounded, a poison row does not starve later eligible rows, and physical ZIP bytes cannot exceed the configured package-volume quota.
- Upload complete rejects intermediate or leaf symlink replacement and ffprobe consumes only the inherited pinned descriptor.
- Small Range requests do not trigger a full-file pre-hash.
- Private Compose env is regular, non-symlink and mode-restricted; evidence/logs contain no credentials or raw connection strings.
- Delivery Compose container environments contain no database password or write-guard secret values; service-scoped secret mounts and fail-closed `*_FILE` readers are covered by config/static tests and runtime inspection when Docker is available.
- Application image services drop all capabilities and add back only `DAC_READ_SEARCH`, `CHOWN`, `SETGID`, and `SETUID`; runtime inspection proves the post-`gosu` process is UID/GID 10001 with `CapEff=0`.
- Existing-volume bootstrap is idempotent, transfers legacy runtime-owned objects to owner, and leaves backend, maintenance and package-worker without admin/owner environment variables; runtime CRUD passes while `CREATE TABLE`, `ALTER TABLE` and `DROP TABLE` fail.
- PostgreSQL 0015 upgrades pre-existing operation-log rows with explicit legacy attribution; 0016 restores compatibility defaults for controlled application rollback. The non-owner/no-DDL runtime role can select and append operation rows through the identity sequence but cannot update, delete, or truncate them.
- Compose restart/down-up and backup/restore preserve database identity, business rows, upload metadata and file associations under the same canonical storage root.
- Logs contain only the documented bounded command metadata, a one-way User-Agent fingerprint, and error codes; fixtures and evidence confirm that no payloads, raw User-Agent values, raw idempotency keys, credentials, cookies, paths, URLs, or tokens are persisted or exported.
- The built top-level HTML retains a CSP meta fallback restricted to the configured API origin. The frontend host adds header-only `frame-ancestors 'none'`, `X-Frame-Options: DENY`, and `X-Content-Type-Options: nosniff`; the default build reads an actual HTML response and rejects header/meta drift, wildcard connect/media sources, or missing anti-framing controls.
- Precise playback never renders another version, another issue, or an old revision's annotation set.
