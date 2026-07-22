# FRONTEND DESIGN

Source of truth: `FJ_Final_Cut_Review_SPEC_V1.3_Reviewed.md`.

## Product Boundary

- Frontend delivers the final cut review cockpit only: project browsing/management, item/version review, playback, annotation, issue threads, request changes, finalization, finalized original download, and review-side project package download.
- V1 must not add account/login/member/role pages, notification center, task center, delivery center, download center, mobile layout, AI features, delete UI, revoke-finalization UI, or cross-version automatic issue tracking.
- Entry surface is workflow context, not user identity. The server owns `ExecutionContext`, capability enforcement, write guard validation, resource ownership checks, and state machine checks.

## Entry Routes

- Standalone entry roots are exactly `/edit` and `/review`.
- `/edit` may expose shared child routes for project list, project detail, item detail, and workspace views, but only with edit profile actions.
- `/review` may expose the same shared child routes, but only with review profile actions.
- Header entry navigation is asymmetric on every list, detail, loading, error, and workspace surface: `/edit` renders only the current edit link; `/review` renders both edit and review links, marks review current, and permits return to `/edit/projects`.
- Navigation visibility is not an authorization boundary. Direct `/review` routes remain registered, and server-side capability enforcement remains mandatory.
- Shared read calls use the single query API namespace `/api/v1/final-cut-review/...`.
- Write calls use thin command adapters only:
  - edit commands call `/api/v1/final-cut-review/edit/...`.
  - review commands call `/api/v1/final-cut-review/review/...`.
- Frontend must not register or call any DELETE route.
- Upload UI uses the independent file upload API under `/api/v1/files/uploads/...`; aborting an incomplete upload session is not a business delete.
- Binary part PUT uses an upload-progress-capable transport compatible with Safari and Chrome. Init and complete keep retry-stable fetch/idempotency behavior; the PUT transport emits byte progress while the request is in flight, preserves credentials and request identity, and parses the same success/error envelope without logging response bodies.
- The existing single bottom-edge progress line is the only visual upload progress surface. It receives monotonic global bytes across dynamically sized parts; no wide white footer, duplicate progress card, or decorative indeterminate bar is introduced.
- A bounded in-memory operation registry is keyed by the file metadata signature so reselecting a settled failed operation during the mounted page can resume the known upload instead of creating a new session. Before a different `File` object adopts that failed operation, the client compares the already-uploaded prefix of the old and new files byte-for-byte in bounded chunks; only an exact prefix match may reuse the session. A mismatch fails closed without sending another part or completing a mixed file. While an operation is in flight or completed but not yet released, a different `File` object with the same metadata signature also fails closed and cannot share the first file's promise or `file_id`. Network/timeout/malformed-response outcomes remain uncertain and retry the same part; only an explicit abandon action may invoke the server abort endpoint.

## Shared Pages And Components

`/edit` and `/review` must reuse the same pages and core components:

- `ProjectListPage`
- `ProjectDetailPage`
- `ReviewItemPage`
- `ReviewWorkspacePage`
- `ReviewPlayer`
- `AnnotationOverlay`
- `AnnotationToolbar`
- `ReviewTimeline`
- `VersionRail`
- `VersionCompare`
- `IssuePanel`
- `UploadDialogs`
- `Finalization`
- `PackageDownload`

Each shared page receives route context, generated contract DTOs, query data, and an entry capability profile. The page must not branch into duplicated business logic per entry.

## Capability Gate

CapabilityGate is an experience gate only. Hidden or disabled UI never replaces server-side entry policy, write guard, principal authorization, parent/child resource validation, or domain state validation.

Edit profile UI allows:

- project read/create/update
- item read/create/update and conditional pre-review physical delete with native confirmation
- version read/upload/compare
- issue read
- finalization read
- finalized original download

Edit profile UI forbids:

- issue create/update/reply/resolve/reopen
- start review
- request changes
- finalization create
- finalized project package create/read/download
- project archive/restore/soft delete
- issue soft delete

Review profile UI allows:

- project read/archive/restore and conditional active-project soft delete
- item/version read
- version compare
- issue read/create/update/reply/resolve/reopen and current-version soft delete
- start review
- request changes
- finalization read/create
- finalized original download
- finalized project package create/read/download

Review profile UI forbids:

- project create/update
- item create/update/physical delete
- version upload

Embedded profile is supplied by `ReviewHostBridge` context. If host context does not provide an entry profile, embedded mode defaults to read-only until capabilities are injected.

## Query Keys

Query keys must include stable ownership IDs:

```ts
["fj-review", "projects", query]
["fj-review", "project", projectRefId]
["fj-review", "items", projectRefId, query]
["fj-review", "item", projectRefId, reviewItemId]
["fj-review", "versions", projectRefId, reviewItemId]
["fj-review", "version", projectRefId, reviewItemId, versionId]
["fj-review", "issues", projectRefId, reviewItemId, versionId, query]
["fj-review", "finalization", projectRefId, reviewItemId]
["fj-review", "package", projectRefId, packageId]
```

Forbidden query keys: keys based only on `versionNo`, `itemCode`, `issueId`, filename, timecode text, or array index.

## Context Switching

When `projectRefId`, `reviewItemId`, or `versionId` changes, the workspace must:

1. Pause the old video.
2. Clear the old media URL.
3. Clear old saved annotation display.
4. Clear temporary drawing.
5. Clear the old issue list.
6. Cancel old requests.
7. Cancel old uploads.
8. Reset timecode and selected issue.
9. Load the new context.

Old responses may write state only after rechecking `projectRefId`, `reviewItemId`, and `versionId`.

## Host Embedding

The module manifest must expose `moduleId: "final-cut-review"`, contract version `1.0`, standalone routes `{ edit: "/edit", review: "/review" }`, mount slot `workspace.main`, and the generated capability list.

`ReviewHostBridge` must support:

- `mode: "standalone" | "embedded"`
- `mount(container, initialProjectRefId?)`
- `unmount()`
- optional context change subscription
- optional host project catalog
- optional principal context and authorization adapter
- optional host HTTP client, event bus, file service, portal root, navigation, and theme tokens

Embedded rules:

- Do not render an independent global top bar.
- Root container is `width: 100%; height: 100%`.
- Project catalog may come from the host.
- Authorization may come from the host.
- Host permission changes recalculate CapabilityGate state without changing domain models.
- Project changes cancel old requests and clear old playback state.
- Dialogs, popovers, and menus use host `portalRoot` when provided.

## Style And Layout

- Root class: `.fj-review-root`.
- All module classes: `.fj-review-*`.
- CSS variables: `--fj-review-*`.
- Do not reset global `html`, `body`, `button`, `input`, `video`, or `canvas`.
- Theme tokens follow SPEC 34.1, including root/background/panel/input/border/text/accent/danger/warning/success variables.
- Workbench layout: 40px top bar; main body `minmax(0, 1fr) + 340px` issue panel; player area plus 150px version rail.
- At 1366px and wider, player, version rail, and issue panel are visible together.
- Below 1280px, issue panel is a drawer and version rail may collapse.
- Mobile layout is out of scope.
- Icon buttons require `aria-label` and tooltip, focus must be visible, status must not rely on color alone, hit targets are at least 28x28, keyboard operation and `prefers-reduced-motion` are supported.

## Player And Timecode

`ReviewPlayer` uses real `HTMLVideoElement` playback with `object-fit: contain` and supports:

- play/pause
- progress seek
- previous/next frame
- previous/next issue
- timecode input seek
- `HH:MM:SS:FF`
- volume/mute
- 0.5x, 0.75x, 1x, 1.25x, 1.5x, 2x
- fit window, original ratio, fullscreen

Keyboard shortcuts:

- Space: play/pause
- Left/Right: previous/next frame
- Shift+Left/Right: previous/next second
- C: create current timecode issue
- 1/2/3/4/5: pen/arrow/rect/circle/text
- Esc: cancel drawing
- Ctrl/Cmd+Enter: submit issue

Timecode uses the current `ReviewVersion.originalMedia.fpsNum/fpsDen`. MVP does not implement SMPTE Drop Frame text.

## Annotation And Issues

Annotation tools:

- select
- pen
- arrow
- rectangle
- circle
- text
- undo/redo
- red, cyan-green, yellow, custom color
- line width

Layer order:

```text
video
-> saved selected AnnotationSet layer
-> temporary drawing layer
-> annotation toolbar
-> playback controls
```

After drawing, the UI must pause video, record precise version/time/frame/video dimensions/canvas dimensions, focus the issue input, and create an immutable `AnnotationSet` only when the issue is submitted.

Coordinates are normalized against the actual contained video rectangle, not the black stage. Values are clamped to `[0, 1]`, and canvas rendering accounts for `devicePixelRatio`.

Issue rules:

- Only review entry can create, edit, reply, resolve, reopen, request changes, or finalize.
- Edit entry can read issues, replies, status, and annotations.
- Issue creation requires current version, content, timecode, and frame number; annotation is optional.
- Creating the first issue may implicitly start review in the same transaction.
- Editing issue text or marks creates a new immutable `ReviewIssueRevision`.
- Resolved issues must be reopened before editing.
- Replies are text-only, version-bound, issue-bound, and read-only in edit entry.
- Current-version issue soft delete is review-entry only and requires confirmation; revisions, annotations, messages, and audit history remain physically retained. Historical-version issue delete is never exposed.
- No attachments, mentions, or notifications are exposed.

## Timeline And Version Rules

- Current version unresolved markers are red; resolved markers are cyan-green; selected markers are enlarged or highlighted.
- The current-version timeline must not mix historical issue markers.
- Clicking a historical issue must explicitly switch to its owning `versionId`.
- Version compare is manual only and must not infer matching, fixes, leftovers, new issues, or timecode mapping.

## Precise Annotation Playback

All playback entry points use the same `ReviewPlaybackTarget`: issue card, issue timecode, timeline marker, previous issue, next issue, and automatic pause selection.

```ts
interface ReviewPlaybackTarget {
  projectRefId: string;
  reviewItemId: string;
  versionId: string;
  issueId: string;
  revisionId: string;
  annotationSetId?: string;
  timestampMs: number;
  frameNumber: number;
}
```

Forbidden target shortcuts:

- local `timeMs` only
- current selected version
- current player URL
- array index
- display version label
- filename
- timecode text

Precise playback sequence:

1. Read the target issue.
2. Verify `projectRefId`, `reviewItemId`, and `versionId`.
3. Switch to target `versionId` when needed.
4. Wait for target version data and playback-ready media.
5. Wait for `loadedmetadata` and `canplay`.
6. Verify current player media still belongs to target `versionId`.
7. Convert `frameNumber` to target time with frozen `fpsNum/fpsDen`.
8. Set `video.currentTime`.
9. Wait for `seeked`.
10. Wait for `requestVideoFrameCallback` when available.
11. Pause the video.
12. Load the target current revision and its `annotationSetId`.
13. Render only that AnnotationSet.
14. Highlight the issue card and timeline marker.
15. Scroll the issue card into view.

Fixed `setTimeout` delays are not valid substitutes for media events.

Selected annotations must match selected Issue, current Revision, current AnnotationSet, and current `versionId`. The default unselected strategy is no saved issue annotation overlay.

Previous/next issue navigation is scoped to the current version issue list, sorted by `timestampMs + issueNo`, and disabled at list boundaries.

Automatic pause applies only to unresolved issues in the current version during natural forward playback. Historical unresolved issues, resolved issues, and manual seeks must not trigger current-version auto pause.

Each playback request carries a `playbackRequestId` or sequence guard. Newer requests invalidate older media listeners, query writes, seek callbacks, and frame callbacks. Component unmount and project/item/version switches cancel pending playback and clear selected AnnotationSet state.

## Frontend Test Obligations

Unit tests must cover:

- `frameFromTimestampMs`
- `timestampMsFromFrame`
- `formatReviewTimecode`
- `computeContainedVideoRect`
- `pointerToNormalizedVideoPoint`
- `normalizedVideoPointToCanvasPoint`
- `ReviewPlaybackTarget` validation
- frame rates 24/1, 25/1, 30/1, 24000/1001, and 30000/1001
- 9:16 video in 16:9 container
- 16:9 video in 16:9 container
- left/right letterboxing
- top/bottom letterboxing
- DPR 1 and 2
- pointer in black bars

Component tests must cover issue card, timecode, keyboard trigger, timeline marker, shared playback flow, selected highlight, and historical read-only display.

E2E tests must cover current-version playback, historical switch before playback, consecutive-click race, 1920/1366 coordinate restore, V1 marks absent from V2, V1 unresolved issues not blocking V2 finalization, and current-version unresolved auto pause.
