# DESIGN DELIVERY

Status: partial inventory, not accepted as complete for the current baseline SPEC.

Baseline SPEC:

- `FJ_Final_Cut_Review_SPEC_V1.3_Reviewed.md`

The Figma file below is an existing editable design asset. It must not be treated
as final design delivery until it is refreshed and re-QA'd against SPEC V1.3
Reviewed, including Chapter 40 precise annotation playback. Historical prompts,
reference screenshots, and prior Figma QA are non-normative.

Figma file:

- URL: `<redacted-figma-design-url>`
- File Key: `<redacted-figma-file-key>`
- Name: `帧界成片审阅台 · Final Cut Review · SPEC V1.3`

## Existing Page Inventory

The existing Figma file records the required page sequence, but page existence is
not completion evidence for the current SPEC:

1. `00 · 封面与目录`
2. `01 · 视觉参考`
3. `02 · Foundations`
4. `03 · Components`
5. `04 · 剪辑入口 Edit`
6. `05 · 审阅入口 Review`
7. `06 · 状态与流程`
8. `07 · Responsive`
9. `08 · Contracts & Handoff`

## Existing Foundations

- Local variable collections: `FJ Review / Theme`, `FJ Review / Spacing`, `FJ Review / Radius`, `FJ Review / Size`.
- Text styles: `FJ Review / Display`, `Heading`, `Panel Title`, `Body`, `Body Strong`, `Caption`, `Mono`, `Button`.
- Theme follows SPEC V1.3 dark dense review workstation tokens.

## Existing Components

The existing file records local `FJ Review /` components and component sets.

Key component node IDs:

- Components board: `13:2`
- Button: `13:101`
- Icon Button: `13:114`
- Status Tag: `13:136`
- Timeline Issue Marker: `13:143`
- Annotation Tool Button: `13:192`

## Existing Key Screens

- Review Workspace 1920: `15:2`
- Review Workspace 1366: `16:2`
- Edit Project Detail: `17:41`
- Review Project Detail: `18:19`
- Contracts & Handoff: `20:2`

## Existing QA Evidence

Historical screenshots were stored in the private pre-clean Git history and are
intentionally excluded from the public source tree. They proved only the older
baseline frames captured at that time; they do not prove Chapter 40 coverage.

- `reference-page-filled.png`
- `cover.png`
- `foundations.png`
- `components.png`
- `review-workspace-1920.png`
- `review-workspace-1366.png`
- `edit-project-detail.png`
- `review-project-detail.png`
- `states.png`
- `responsive.png`
- `contracts-handoff.png`

## Prototype

The existing file records core same-page prototype reactions for Edit and Review
flow frames. Figma rejected cross-page navigation, so cross-entry flow is
represented by same-page chains and route mapping in Contracts & Handoff.
Runtime header navigation follows the approved asymmetric rule: Edit surfaces
show only `剪辑入口`; Review surfaces show `剪辑入口` plus the active `成片审阅`
entry so reviewers can return to Edit.

## Notes

Reference images are only on `01 · 视觉参考`, locked, and not used as formal page backgrounds.

## SPEC V1.3 Chapter 40 Delta

The updated SPEC copy from 2026-06-19 adds precise annotation playback. This design delivery remains the baseline V1.3 Figma delivery, and downstream implementation documents must now treat the following as required additions:

- Issue card, issue timecode, timeline marker, previous issue, and next issue all trigger the same precise playback flow.
- Selected issue, selected timeline marker, playback loading, retryable playback failure, and historical-version read-only states are required design states.
- Only the selected Issue current Revision AnnotationSet is visible after precise playback; mixed all-issue annotation overlays are forbidden.
- Contracts & Handoff must expose `ReviewPlaybackTarget`, rational frame rate, normalized video coordinates, and stale playback request cancellation.

## Open Acceptance Gaps

The following gaps block any claim that design delivery is complete for the
current SPEC:

- No refreshed Figma screenshot proves Issue Card, issue timecode, timeline marker, previous issue, and next issue all trigger the same precise playback flow.
- No refreshed frame proves historical-version issue click first switches to the owning version before seek and shows that version's current AnnotationSet.
- No refreshed frame proves only the selected Issue current Revision AnnotationSet is visible, with other issues, other versions, and old Revisions hidden.
- No refreshed state proves playback loading, retryable failure, stale request cancellation, and consecutive-click race behavior.
- No refreshed responsive QA proves 1920 and 1366 coordinate replay against actual video bounds, or full-screen coordinate recovery.
- No refreshed handoff proves the complete route capability split: `/edit` has no review writes; `/review` has no project create/update, item create/update, or version upload/append controls; and project archive/restore/delete plus packaging are review-only.

Before claiming design delivery complete, update the relevant Figma frames and
capture new QA evidence in the private release-evidence workflow, outside the public Git tree.
