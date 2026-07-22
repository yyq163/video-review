import type {
  OriginalMediaSnapshot,
  ReviewAnnotationSet,
  ReviewIssue,
  ReviewIssueRevision,
  ReviewVersion,
  StoredOriginalFile,
} from '../contracts/types';
import { createDemoVideoBlob, demoVideoMimeType } from './demo-video';

export function makeSeedFile(input: {
  originalFileId: string;
  fileName: string;
  sha256: string;
}): StoredOriginalFile {
  const blob = createDemoVideoBlob();
  return {
    originalFileId: input.originalFileId,
    fileName: input.fileName,
    mimeType: demoVideoMimeType,
    size: blob.size,
    sha256: input.sha256,
    durationMs: 480,
    width: 1280,
    height: 720,
    fpsNum: 25,
    fpsDen: 1,
    playbackUrl: '',
    blob,
  };
}

function originalMedia(file: StoredOriginalFile): OriginalMediaSnapshot {
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

export function makeVersion(input: {
  versionId: string;
  projectRefId: string;
  reviewItemId: string;
  versionNo: number;
  file: StoredOriginalFile;
  status: ReviewVersion['status'];
  uploadedAt: string;
  requestedChangesAt: string | null;
}): ReviewVersion {
  const media = originalMedia(input.file);
  return {
    versionId: input.versionId,
    projectRefId: input.projectRefId,
    reviewItemId: input.reviewItemId,
    versionNo: input.versionNo,
    label: `V${input.versionNo}`,
    originalFileId: input.file.originalFileId,
    originalMedia: media,
    sha256: input.file.sha256,
    fileName: input.file.fileName,
    mimeType: input.file.mimeType,
    size: input.file.size,
    durationMs: media.durationMs,
    width: media.width,
    height: media.height,
    fpsNum: media.fpsNum,
    fpsDen: media.fpsDen,
    playbackAssetId: `playback_${input.versionId}`,
    playbackUrl: input.file.playbackUrl,
    status: input.status,
    versionNote: null,
    changeSummary: null,
    uploadedAt: input.uploadedAt,
    requestedChangesAt: input.requestedChangesAt,
  };
}

export function makeIssue(input: {
  issueId: string;
  issueNo: number;
  projectRefId: string;
  reviewItemId: string;
  versionId: string;
  timestampMs: number;
  frameNumber: number;
  status: ReviewIssue['status'];
  severity: ReviewIssue['severity'];
  body: string;
  shape: ReviewAnnotationSet['shapes'][number];
  createdAt: string;
}): ReviewIssue {
  const revisionId = `rev_${input.issueId}_001`;
  const annotationSetId = `aset_${input.issueId}_001`;
  const revision: ReviewIssueRevision = {
    revisionId,
    projectRefId: input.projectRefId,
    reviewItemId: input.reviewItemId,
    versionId: input.versionId,
    issueId: input.issueId,
    revisionNo: 1,
    content: input.body,
    annotationSetId,
    timestampMs: input.timestampMs,
    frameNumber: input.frameNumber,
    createdAt: input.createdAt,
  };
  const annotationSet: ReviewAnnotationSet = {
    annotationSetId,
    projectRefId: input.projectRefId,
    reviewItemId: input.reviewItemId,
    versionId: input.versionId,
    issueId: input.issueId,
    revisionId,
    timestampMs: input.timestampMs,
    frameNumber: input.frameNumber,
    canvasWidth: 1280,
    canvasHeight: 720,
    videoWidth: 1280,
    videoHeight: 720,
    shapes: [input.shape],
    createdAt: input.createdAt,
  };
  return {
    issueId: input.issueId,
    issueNo: input.issueNo,
    projectRefId: input.projectRefId,
    reviewItemId: input.reviewItemId,
    versionId: input.versionId,
    status: input.status,
    severity: input.severity,
    currentRevisionId: revisionId,
    timestampMs: input.timestampMs,
    frameNumber: input.frameNumber,
    lockVersion: 1,
    body: input.body,
    annotationSetId,
    currentRevision: revision,
    currentAnnotationSet: annotationSet,
    revisions: [revision],
    replies: [],
    createdAt: input.createdAt,
    updatedAt: input.createdAt,
  };
}
