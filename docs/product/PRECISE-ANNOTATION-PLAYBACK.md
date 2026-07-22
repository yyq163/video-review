# Precise Annotation Playback

Source of truth: `FJ_Final_Cut_Review_SPEC_V1.3_Reviewed.md`.

This document records the implementation contract for version-scoped precise annotation playback. It does not change the SPEC V1.3 product boundary: no login, no delete, no cross-version issue tracking, no cross-version timecode mapping, no automatic fix judgment, no download center, and no change to finalization rules.

## Definition

When a user activates an issue card, issue timecode, timeline issue point, previous issue, or next issue, the system must return to that issue's own `projectRefId`, `reviewItemId`, `versionId`, `timestampMs`, `frameNumber`, current revision, and AnnotationSet.

Precision is guaranteed only within the same review-version timeline. Variable frame rate media promises review timeline frame precision only, not source encoded PTS precision.

## ReviewPlaybackTarget

Every precise playback entrypoint must create the full contract:

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

This contract belongs to the unified review contract layer, not to a local UI component.

Forbidden shortcuts:

- passing only `timeMs`
- deriving target from currently selected version
- deriving target from current player URL
- deriving target from array index
- deriving target from display version label
- deriving target from filename
- deriving target from timecode text

## Entrypoints

All entrypoints use the same playback pipeline:

- issue card click
- issue timecode click
- issue card Enter/Space
- timeline issue marker click
- previous issue
- next issue
- current-version automatic pause on unresolved issue

## Playback Sequence

```text
read target issue
-> verify projectRefId / reviewItemId / versionId
-> invalidate older playback request
-> switch to target.versionId when current version differs
-> wait for target version data
-> wait for target version media playback_ready
-> wait for video loadedmetadata / canplay
-> verify player media still belongs to target.versionId
-> convert frameNumber with target version fpsNum/fpsDen
-> set HTMLVideoElement.currentTime
-> wait for seeked
-> wait for requestVideoFrameCallback when available
-> pause video
-> load target current revision
-> load that revision annotationSetId
-> render only that AnnotationSet
-> highlight issue card
-> highlight timeline issue marker
-> scroll issue card into view
```

The implementation must control the real `HTMLVideoElement`. Updating React state or text timecode alone is not enough. Fixed `setTimeout` delays are not a valid replacement for `loadedmetadata`, `canplay`, `seeking`, `seeked`, `error`, or `requestVideoFrameCallback`.

## Frame And Timecode Rules

Use the frozen rational FPS from `ReviewVersion.originalMedia`:

```ts
interface ReviewFrameRate {
  fpsNum: number;
  fpsDen: number;
}
```

Frame calculation:

```text
frame_number = floor(timestamp_ms * fps_num / (1000 * fps_den))
timestamp_ms = floor(frame_number * 1000 * fps_den / fps_num)
```

Required pure functions:

```ts
frameFromTimestampMs(timestampMs: number, fpsNum: number, fpsDen: number): number
timestampMsFromFrame(frameNumber: number, fpsNum: number, fpsDen: number): number
formatReviewTimecode(frameNumber: number, fpsNum: number, fpsDen: number): string
```

Required frame rates:

- 24/1
- 25/1
- 30/1
- 24000/1001
- 30000/1001

MVP does not implement SMPTE Drop Frame text.

## Version Rules

Current-version playback:

```text
current view V2
click V2 issue
-> stay on V2
-> seek to V2 timestampMs/frameNumber
-> display V2 current Revision AnnotationSet
```

Historical-version playback:

```text
current view V2
click V1 historical issue
-> switch to V1
-> load V1 video
-> seek to V1 timestampMs/frameNumber
-> display V1 current Revision AnnotationSet
```

Forbidden behavior:

- render V1 issues directly over V2 video
- map V1 timecode onto V2
- apply V1 coordinates to V2
- infer whether V2 fixed V1 automatically
- let V1 unresolved issues block V2 finalization

## Annotation Display Rules

Precise playback may render saved marks only when all bindings match:

```text
selected Issue
+ current Revision
+ current AnnotationSet
+ current versionId
```

Forbidden display:

- other issues in the same version
- other versions
- old revisions
- flat-mapped marks from all issues
- marks filtered only by timecode without checking `issueId`, `revisionId`, and `versionId`

The default unselected strategy for this implementation is: show no saved issue annotation overlay when no issue is selected.

## Coordinate Rules

Pointer capture and replay use normalized coordinates within the actual contained video rectangle, not the black stage area:

```text
scale = min(container_width / video_width, container_height / video_height)
display_width = video_width * scale
display_height = video_height * scale
offset_x = (container_width - display_width) / 2
offset_y = (container_height - display_height) / 2
normalized_x = (pointer_x - offset_x) / display_width
normalized_y = (pointer_y - offset_y) / display_height
```

Coordinates are clamped to `[0, 1]`, and canvas rendering accounts for `devicePixelRatio`.

## Race Rules

Each playback request must generate `playback_request_id` or an increasing sequence number.

Rules:

- new request invalidates old requests
- old `loadedmetadata`, `canplay`, `seeked`, and frame callbacks cannot write current state
- old version data responses cannot write current state
- component unmount cancels pending playback and event listeners
- project/item/version switches clear old playback state, temporary drawing, and selected AnnotationSet

Rapid clicks:

```text
#001 -> #002 -> #003
```

Final state must be only:

```text
#003 versionId / timestampMs / frameNumber / AnnotationSet
```

## UI Requirements

Issue cards:

- card click triggers precise playback
- timecode click triggers precise playback
- Enter/Space triggers precise playback
- selected issue is highlighted
- playback pending state is visible
- playback failure supports retry

Timeline markers:

- show only current-version issue points
- click uses the same precise playback flow
- hover shows issue number, timecode, status, and text summary
- selected marker is enlarged or highlighted

Previous/next issue:

- navigate only within the current-version issue list
- sort by `timestampMs + issueNo`
- disable at the first/last issue
- use the same precise playback flow

Historical issues:

- may appear as read-only references
- click first switches to the issue's version
- read-only reason is explicit

## Automatic Pause

Natural playback reaching a current-version unresolved issue point must:

```text
auto pause
-> select that issue
-> run AnnotationSet loading and highlight logic from precise playback
```

Forbidden behavior:

- historical unresolved issues trigger current-version auto pause
- resolved issues trigger auto pause
- manual seek is mistaken for natural playback

The same natural forward pass triggers each issue once. After the user manually seeks before that point, the issue may trigger again.

## Acceptance Checklist

The implementation is not complete until all SPEC 40.11 checks pass:

1. Current V2 issue click stops on V2 target `timestampMs/frameNumber`.
2. V1 historical issue click from V2 switches to V1 before seeking.
3. Video is paused after playback.
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
15. Version switching clears previous-version temporary drawing and selected AnnotationSet.
16. After issue edit, default playback uses current Revision, not an old Revision.
17. Variable frame rate media promises review timeline frame precision only, not source encoded PTS precision.

## Required Tests

Unit tests:

- `frameFromTimestampMs`
- `timestampMsFromFrame`
- `formatReviewTimecode`
- `computeContainedVideoRect`
- `pointerToNormalizedVideoPoint`
- `normalizedVideoPointToCanvasPoint`
- `ReviewPlaybackTarget` validation

Unit coverage cases:

- 25/1
- 24/1
- 30/1
- 24000/1001
- 30000/1001
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
