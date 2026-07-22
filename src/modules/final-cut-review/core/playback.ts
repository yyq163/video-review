import type { ReviewIssue, ReviewPlaybackTarget, ReviewVersion } from '../contracts/types';
import { timestampMsFromFrame, validatePlaybackTargetDrift } from './timecode';

export function playbackTargetFromIssue(issue: ReviewIssue): ReviewPlaybackTarget {
  return {
    projectRefId: issue.projectRefId,
    reviewItemId: issue.reviewItemId,
    versionId: issue.versionId,
    issueId: issue.issueId,
    revisionId: issue.currentRevisionId,
    annotationSetId: issue.annotationSetId,
    timestampMs: issue.timestampMs,
    frameNumber: issue.frameNumber,
  };
}

export function getIssueAnnotationSet(issue: ReviewIssue, target: ReviewPlaybackTarget) {
  if (
    issue.projectRefId !== target.projectRefId ||
    issue.reviewItemId !== target.reviewItemId ||
    issue.versionId !== target.versionId ||
    issue.issueId !== target.issueId ||
    issue.currentRevisionId !== target.revisionId
  ) {
    return null;
  }
  if (target.annotationSetId && issue.currentAnnotationSet?.annotationSetId !== target.annotationSetId) {
    return null;
  }
  return issue.currentAnnotationSet;
}

export function sortedIssuesForPlayback(issues: ReviewIssue[]): ReviewIssue[] {
  return [...issues].sort((left, right) => {
    const statusOrder = Number(left.status === 'resolved') - Number(right.status === 'resolved');
    return statusOrder || left.timestampMs - right.timestampMs || left.issueNo - right.issueNo;
  });
}

export function targetTimeMsForVersion(target: ReviewPlaybackTarget, version: ReviewVersion): number {
  validatePlaybackTargetDrift({
    timestampMs: target.timestampMs,
    frameNumber: target.frameNumber,
    fpsNum: version.fpsNum,
    fpsDen: version.fpsDen,
  });
  return timestampMsFromFrame(target.frameNumber, version.fpsNum, version.fpsDen);
}

export class PlaybackRequestSequencer {
  private current = 0;

  next(): number {
    this.current += 1;
    return this.current;
  }

  isCurrent(sequence: number): boolean {
    return sequence === this.current;
  }

  cancel(): void {
    this.current += 1;
  }
}
