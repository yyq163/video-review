import type {
  FinalizationRecord,
  IssueId,
  OriginalMediaSnapshot,
  ProjectRefId,
  ReviewAnnotationSet,
  ReviewAnnotationShape,
  ReviewIssue,
  ReviewIssueRevision,
  ReviewItemId,
  ReviewVersion,
  StoredOriginalFile,
  VersionId,
} from '../contracts/types';
import { createUuid } from '../core/uuid';

export function nowIso(): string {
  return new Date().toISOString();
}

export function cloneShape(shape: ReviewAnnotationShape): ReviewAnnotationShape {
  return {
    ...shape,
    points: shape.points?.map((point) => ({ ...point })),
    bounds: shape.bounds ? { ...shape.bounds } : undefined,
  };
}

function cloneAnnotationSet(annotationSet: ReviewAnnotationSet): ReviewAnnotationSet {
  return {
    ...annotationSet,
    shapes: annotationSet.shapes.map(cloneShape),
  };
}

function cloneRevision(revision: ReviewIssueRevision): ReviewIssueRevision {
  return { ...revision };
}

export function buildAnnotationSet(input: {
  annotationSetId: string;
  projectRefId: ProjectRefId;
  reviewItemId: ReviewItemId;
  versionId: VersionId;
  issueId: IssueId;
  revisionId: string;
  timestampMs: number;
  frameNumber: number;
  canvasWidth: number;
  canvasHeight: number;
  videoWidth: number;
  videoHeight: number;
  shapes: ReviewAnnotationShape[];
  createdAt: string;
}): ReviewAnnotationSet {
  return {
    annotationSetId: input.annotationSetId,
    projectRefId: input.projectRefId,
    reviewItemId: input.reviewItemId,
    versionId: input.versionId,
    issueId: input.issueId,
    revisionId: input.revisionId,
    timestampMs: input.timestampMs,
    frameNumber: input.frameNumber,
    canvasWidth: input.canvasWidth,
    canvasHeight: input.canvasHeight,
    videoWidth: input.videoWidth,
    videoHeight: input.videoHeight,
    shapes: input.shapes.map((shape) => ({
      ...cloneShape(shape),
      shapeId: shape.shapeId || `shape_${createUuid()}`,
    })),
    createdAt: input.createdAt,
  };
}

export function cloneIssue(issue: ReviewIssue): ReviewIssue {
  return {
    ...issue,
    deletedAt: issue.deletedAt ?? null,
    currentRevision: cloneRevision(issue.currentRevision),
    currentAnnotationSet: issue.currentAnnotationSet ? cloneAnnotationSet(issue.currentAnnotationSet) : null,
    revisions: issue.revisions.map(cloneRevision),
    replies: issue.replies.map((reply) => ({ ...reply })),
  };
}

export function cloneVersion(version: ReviewVersion): ReviewVersion {
  return { ...version, originalMedia: { ...version.originalMedia } };
}

export function cloneFinalization(finalization: FinalizationRecord): FinalizationRecord {
  return { ...finalization, originalMedia: { ...finalization.originalMedia } };
}

export function originalMediaFromFile(file: StoredOriginalFile): OriginalMediaSnapshot {
  return {
    originalFileId: file.originalFileId,
    originalFilename: file.fileName,
    mimeType: file.mimeType,
    fileSize: file.size,
    sha256: file.sha256,
    durationMs: file.durationMs,
    width: file.width,
    height: file.height,
    fpsNum: file.fpsNum,
    fpsDen: file.fpsDen,
    mediaProbeVersion: 'mock-probe-v1',
  };
}
