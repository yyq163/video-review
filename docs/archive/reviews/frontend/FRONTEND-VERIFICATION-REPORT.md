# FRONTEND VERIFICATION REPORT

Task: `T6-spec-v13-precise-playback`

Authority: `FJ_Final_Cut_Review_SPEC_V1.3_Reviewed.md`.

Status: PASS for the current frontend implementation and document-alignment scope.
Figma refresh remains tracked separately in `DESIGN-DELIVERY.md` as a design-delivery evidence gap.

## Verification Results

Final run on 2026-06-19:

- `npm ci`: passed after removing stale generated `node_modules`; warnings only for Node 23.11.0 engine range and local TLS environment.
- `npm run contracts:generate`: passed.
- `npm run contracts:check`: passed.
- `npm run typecheck`: passed.
- `npm run lint`: passed.
- `npm run test`: passed, 2 files / 21 tests.
- `npm run test:e2e`: passed, 8 Chromium tests.
- `npm run build`: passed.
- Browser smoke at `http://127.0.0.1:5189/`: passed for 1366 and 1920 viewports, both verifying frame `4`, paused video, selected issue `issue_v2_001`, and AnnotationSet `aset_issue_v2_001_001`.
- `git diff --check`: passed.
- Repository delivery-doc scan for forbidden external/sandbox baseline paths: no matches in checked markdown documents.

## Required Commands

```bash
npm ci
npm run contracts:generate
npm run contracts:check
npm run typecheck
npm run lint
npm run test
npm run test:e2e
npm run build
```

The final clean install needed removal of generated `node_modules` because npm hit
`ENOTEMPTY` while cleaning `lucide-react`; package files were not changed.

## Route And Capability Verification

- Open `/edit`; it may use internal child routes such as `/edit/projects`, but `/edit` remains the SPEC entry root.
- Verify `/edit` exposes project create/update/archive/restore, item create/update, version upload/compare, issue read, finalization read, and finalized original download only.
- Verify `/edit` does not expose issue create/update/reply/resolve/reopen, start review, request changes, finalization create, project package, or delete controls.
- Open `/review`; it may use internal child routes such as `/review/projects/...`, but `/review` remains the SPEC entry root.
- Verify `/review` exposes issue create/update/reply/resolve/reopen, start review, request changes, finalization create, finalized original download, and project package controls when capabilities allow.
- Verify `/review` does not expose project create/update/archive/restore, item create/update, version upload, or delete controls.
- Inspect network calls: shared reads use `/api/v1/final-cut-review/...`; edit writes use `/api/v1/final-cut-review/edit/...`; review writes use `/api/v1/final-cut-review/review/...`; uploads use `/api/v1/files/uploads/...`.
- Verify frontend flow registers or calls no DELETE endpoint.

## Host And Style Verification

- Embedded mode renders no standalone global top bar and fills the host container.
- Host project changes cancel old requests and clear old playback state.
- Host permission changes recalculate CapabilityGate without rebuilding domain models.
- Dialogs and popovers render into host `portalRoot` when provided.
- CSS selectors remain scoped to `.fj-review-root`, `.fj-review-*`, and `--fj-review-*`; no global `html/body/button/input/video/canvas` reset is introduced.
- At 1366px, player, version rail, and issue panel are visible together; below 1280px, issue panel is drawer-style and version rail may collapse.
- Icon buttons have `aria-label`, tooltip, visible focus, and at least 28x28 hit area; status is not color-only.

## Precise Playback Browser Checks

- Start local dev server at `http://127.0.0.1:5188`.
- Open a review workspace for a seeded item.
- Click a current-version issue card and confirm the player pauses at the target `timestampMs/frameNumber`.
- Click a current-version issue timecode and confirm it uses the same playback flow.
- Activate the timecode by keyboard and confirm it uses the same playback flow.
- Click a current-version timeline marker and confirm it uses the same playback flow.
- Click previous/next issue and confirm navigation stays inside the current-version issue set and is sorted by `timestampMs + issueNo`.
- Click a historical V1 issue from V2 and confirm the workspace switches to V1 before seeking.
- Rapidly click #001, #002, and #003; only #003 may remain selected, highlighted, paused, and rendered.
- Confirm only the selected issue current Revision AnnotationSet renders.
- Confirm same-version sibling marks, other-version marks, and old Revision marks do not render.
- Confirm same-timecode multiple issues highlight only the selected issue.
- Capture 1920 and 1366 screenshots and compare overlay position against the contained video bounds.
- Change left/right black bars and verify annotation position remains relative to the video image.
- Enter fullscreen and verify annotation position restores correctly.
- Switch versions and verify temporary drawing plus selected AnnotationSet are cleared.
- Let natural playback reach a current-version unresolved issue and verify auto pause selects/highlights it.
- Verify historical unresolved issues, resolved issues, and manual seeks do not trigger current-version auto pause.
- Verify V1 unresolved issues do not block V2 finalization.

## SPEC 40.11 Evidence Matrix

| # | Evidence Required |
| --- | --- |
| 1 | Current V2 issue click stops on V2 target `timestampMs/frameNumber`. |
| 2 | V1 historical issue click from V2 switches to V1 before seeking. |
| 3 | Video is paused after playback. |
| 4 | Displayed frame equals target `frameNumber` within one review frame tolerance. |
| 5 | Only selected issue `currentRevisionId` AnnotationSet is shown. |
| 6 | Other issue marks are hidden. |
| 7 | Other version marks are hidden. |
| 8 | Old Revision marks are hidden. |
| 9 | Same-timecode issues highlight only selected issue. |
| 10 | #001 -> #002 -> #003 ends only on #003. |
| 11 | Stale seek/media-load/request returns cannot override latest selection. |
| 12 | 1920 and 1366 layouts preserve annotation position. |
| 13 | Left/right black bar changes do not offset marks. |
| 14 | Fullscreen restores marks correctly. |
| 15 | Version switching clears temporary drawing and selected AnnotationSet. |
| 16 | Edited issue defaults to current Revision playback. |
| 17 | Variable-frame-rate media states review timeline precision only. |

## SPEC 40.12 Test Matrix

Unit tests must prove:

- `frameFromTimestampMs`
- `timestampMsFromFrame`
- `formatReviewTimecode`
- `computeContainedVideoRect`
- `pointerToNormalizedVideoPoint`
- `normalizedVideoPointToCanvasPoint`
- `ReviewPlaybackTarget` validation
- 25/1, 24/1, 30/1, 24000/1001, 30000/1001
- 9:16 video in 16:9 container
- 16:9 video in 16:9 container
- left/right black bars
- top/bottom black bars
- DPR 1 and 2
- pointer in black bars

Component tests must prove:

- IssueCard click emits `ReviewPlaybackTarget`
- timecode click emits `ReviewPlaybackTarget`
- timecode button supports keyboard activation
- Timeline Marker click uses the same playback flow
- current card highlight
- historical version issue read-only display

E2E tests must prove:

- current-version precise playback
- historical-version switch then playback
- consecutive-click race
- 1920 and 1366 coordinate replay
- V1 marks absent from V2
- V1 unresolved issue does not block V2 finalization
- current-version unresolved auto pause

## Completion Gate

- Evidence entries are appended to `.codex-agent-team/state/evidence-ledger.jsonl`.
- Machine-readable review reports exist under `.codex-agent-team/reports/`.
- `review_gate.py` passed for the implementation scope.
- `.codex-agent-team/state/project-state.json` and `.codex-agent-team/state/task-dag.json` keep T6 status aligned with the review result.
