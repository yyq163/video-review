# FRONTEND AUDIT REPORT

Task: `T6-spec-v13-precise-playback`

Scope: frontend source and documentation inspection against SPEC V1.3 sections 15-17, 24, 28-29, 34-40. This report records the strict audit and final verification status for the current frontend implementation scope.

## SPEC Alignment Checklist

- Routes: standalone entry roots must remain `/edit` and `/review`; child routes are shared by entry context.
- Shared pages: `/edit` and `/review` must reuse `ProjectListPage`, `ProjectDetailPage`, `ReviewItemPage`, `ReviewWorkspacePage`, and the same player/annotation/timeline/version/issue/finalization/package components.
- Capability Gate: edit entry exposes only edit-profile abilities; review entry exposes only review-profile abilities; embedded defaults to read-only without host profile; server enforcement remains authoritative.
- API: reads use one shared `/api/v1/final-cut-review/...` namespace; edit/review writes are thin facades; file upload remains under `/api/v1/files/uploads/...`; no DELETE route may be registered or called.
- Player/timecode: implementation must use real `HTMLVideoElement`, `object-fit: contain`, rational `fpsNum/fpsDen`, frame stepping, timecode input, SPEC shortcuts, and no SMPTE Drop Frame in MVP.
- Annotation: tools, layer order, immutable AnnotationSet submission, normalized contained-video coordinates, DPR scaling, and no black-bar coordinate pollution are required.
- Issues/replies: review entry writes; edit entry read-only; issue revisions are immutable; resolved issue edits require reopen; replies are text-only and version-bound.
- Host embedding: embedded mode must remove standalone top bar, fill host container, support host project catalog/auth/http/event/file/portal/theme injection, clear state on host project changes, and recalculate CapabilityGate on host permission changes.
- Style isolation: only `.fj-review-root`, `.fj-review-*`, and `--fj-review-*`; no global element resets.
- Context switching: project/item/version changes pause video, clear media, clear annotations/drawing/issues/uploads/timecode/selection, cancel requests, and reject stale responses unless all IDs match.

## Current Alignment Observed

- `ReviewPlaybackTarget` exists in `src/modules/final-cut-review/contracts-generated/types.ts`.
- `src/modules/final-cut-review/core/timecode.ts` defines rational frame conversion helpers: `frameFromTimestampMs`, `timestampMsFromFrame`, and `formatReviewTimecode`.
- `src/modules/final-cut-review/core/coordinates.ts` defines contained-video coordinate conversion helpers.
- `src/modules/final-cut-review/core/playback.ts` builds playback targets, checks current AnnotationSet binding, derives target time by version FPS, and includes a request sequencer.
- `ReviewWorkspacePage.tsx` contains pending target and sequence state and switches versions before playback.
- `ReviewPlayer.tsx` exposes playback to target and references real video events and `requestVideoFrameCallback`.
- `IssuePanel.tsx` exposes playback pending and error status.

## SPEC 40 Acceptance Audit

| # | Required Check | Audit Requirement |
| --- | --- | --- |
| 1 | V2 issue click seeks V2 target | E2E and component coverage must prove target `versionId` is retained. |
| 2 | V1 historical click switches to V1 first | E2E must assert version switch before seek. |
| 3 | Playback pauses after replay | Unit/component or E2E must inspect paused video state. |
| 4 | Displayed frame matches target within one review frame | E2E or browser smoke must compare frame/time display. |
| 5 | Current Revision AnnotationSet only | Component/E2E must assert selected revision binding. |
| 6 | Other issue marks hidden | Component/E2E must assert no sibling issue marks. |
| 7 | Other version marks hidden | E2E must assert V1 marks absent from V2. |
| 8 | Old Revision marks hidden | Unit/component fixture must include old revision. |
| 9 | Same-timecode issues highlight only selected | Component fixture must include same timestamp with different issue IDs. |
| 10 | #001 -> #002 -> #003 ends on #003 | E2E race test required. |
| 11 | Stale seek/load/request cannot override latest | Unit race guard and E2E rapid-click coverage required. |
| 12 | 1920/1366 relative coordinates match | E2E screenshot or DOM coordinate assertion required. |
| 13 | Letterbox changes do not offset marks | Unit coordinate tests and browser check required. |
| 14 | Fullscreen restores marks correctly | Browser/manual smoke or E2E required. |
| 15 | Version switch clears old temporary drawing and selected AnnotationSet | Component/E2E state reset assertion required. |
| 16 | Edited issue replays current Revision | Component fixture with old/current revisions required. |
| 17 | VFR precision statement is explicit | UI/test expectation must not claim encoded PTS precision. |

## Required Test Coverage Audit

- Unit coverage must include `frameFromTimestampMs`, `timestampMsFromFrame`, `formatReviewTimecode`, `computeContainedVideoRect`, `pointerToNormalizedVideoPoint`, `normalizedVideoPointToCanvasPoint`, `ReviewPlaybackTarget` validation, all required frame rates, 9:16/16:9 container cases, left/right bars, top/bottom bars, DPR 1/2, and pointer-in-black-bar behavior.
- Component coverage must include IssueCard click, timecode click, keyboard activation, Timeline Marker click, shared playback flow, current card highlight, and historical read-only display.
- E2E coverage must include current-version precise playback, historical switch-and-playback, consecutive-click race, 1920/1366 coordinate replay, V1 marks absent from V2, V1 unresolved not blocking V2 finalization, and current-version unresolved auto pause.

## Completed Verification

- Full command set passed on the current dirty worktree: `npm ci`, `contracts:generate`, `contracts:check`, `typecheck`, `lint`, `test`, `test:e2e`, and `build`.
- Vitest passed 2 files / 21 tests; Playwright passed 8 Chromium E2E tests, including direct timeline-marker playback and edited-issue current Revision playback.
- Browser smoke passed at 1366 and 1920 viewports with actual rendered video state: frame `4`, paused, issue `issue_v2_001`, AnnotationSet `aset_issue_v2_001_001`.
- Final T6 review JSON exists and `review_gate.py` passed with no blocking findings.

## Risks To Review Closely

- Media event race handling can pass unit tests but fail under rapid version switches.
- `requestVideoFrameCallback` availability differs by browser; fallback must still wait for `seeked` and pause.
- Timecode display for 24000/1001 and 30000/1001 is non-drop-frame only; this must be explicit in UI/test expectations.
- Current AnnotationSet filtering must reject stale `annotationSetId` and stale `revisionId`, not only stale `versionId`.
- Host embedded mode must not accidentally keep standalone navigation or global CSS side effects.
