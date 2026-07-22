export type EntryMode = 'edit' | 'review';

export type Capability =
  | 'review.project.read'
  | 'review.project.create'
  | 'review.project.update'
  | 'review.project.archive'
  | 'review.project.restore'
  | 'review.project.delete'
  | 'review.item.read'
  | 'review.item.create'
  | 'review.item.update'
  | 'review.item.delete'
  | 'review.version.read'
  | 'review.version.upload'
  | 'review.version.compare'
  | 'review.issue.read'
  | 'review.issue.create'
  | 'review.issue.update'
  | 'review.issue.reply'
  | 'review.issue.resolve'
  | 'review.issue.reopen'
  | 'review.issue.delete'
  | 'review.session.start'
  | 'review.session.request_changes'
  | 'review.finalization.read'
  | 'review.finalization.create'
  | 'review.download.finalized_original'
  | 'review.package.create'
  | 'review.package.read'
  | 'review.package.download';

export type ProjectRefId = string;
export type ReviewItemId = string;
export type VersionId = string;
export type IssueId = string;
export type RevisionId = string;
export type AnnotationSetId = string;
export type OriginalFileId = string;
export type FinalizationId = string;

export type ReviewItemStatus = 'pending_review' | 'in_review' | 'changes_requested' | 'finalized';
export type IssueStatus = 'unresolved' | 'resolved';
export type IssueSeverity = 'normal' | 'blocking';
export type AnnotationTool = 'pen' | 'arrow' | 'rect' | 'circle' | 'text';

export interface ReviewFrameRate {
  fpsNum: number;
  fpsDen: number;
}

export interface NormalizedPoint {
  x: number;
  y: number;
}

export interface NormalizedBounds {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface ReviewAnnotationShape {
  shapeId: string;
  tool: AnnotationTool;
  color: string;
  lineWidth: number;
  fontSize?: number;
  points?: NormalizedPoint[];
  bounds?: NormalizedBounds;
  text?: string;
}

export interface ReviewAnnotationSet {
  annotationSetId: AnnotationSetId;
  projectRefId: ProjectRefId;
  reviewItemId: ReviewItemId;
  versionId: VersionId;
  issueId: IssueId;
  revisionId: RevisionId;
  timestampMs: number;
  frameNumber: number;
  canvasWidth: number;
  canvasHeight: number;
  videoWidth: number;
  videoHeight: number;
  shapes: ReviewAnnotationShape[];
  createdAt: string;
}

export interface ReviewIssueRevision {
  revisionId: RevisionId;
  projectRefId: ProjectRefId;
  reviewItemId: ReviewItemId;
  versionId: VersionId;
  issueId: IssueId;
  revisionNo: number;
  content: string;
  annotationSetId?: AnnotationSetId;
  timestampMs: number;
  frameNumber: number;
  createdAt: string;
}

export interface OriginalMediaSnapshot {
  originalFileId: OriginalFileId;
  originalFilename: string;
  mimeType: string;
  fileSize: number;
  sha256: string;
  durationMs: number;
  width: number;
  height: number;
  fpsNum: number;
  fpsDen: number;
  mediaProbeVersion: string;
}

export interface Project {
  projectRefId: ProjectRefId;
  name: string;
  code: string;
  description: string;
  status: 'active' | 'archived';
  deletedAt: string | null;
  createdAt: string;
  updatedAt: string;
}

export type UploadProgressStage = 'validating' | 'initiated' | 'uploading' | 'binding' | 'completed';

export interface UploadProgress {
  stage: UploadProgressStage;
  percent: number;
  bytesSent?: number;
  totalBytes?: number;
}

export interface ReviewItem {
  reviewItemId: ReviewItemId;
  projectRefId: ProjectRefId;
  title: string;
  episode: string;
  currentVersionId: VersionId;
  activeFinalizationId: FinalizationId | null;
  status: ReviewItemStatus;
  createdAt: string;
  updatedAt: string;
}

export interface ReviewVersion {
  versionId: VersionId;
  projectRefId: ProjectRefId;
  reviewItemId: ReviewItemId;
  versionNo: number;
  label: string;
  originalFileId: OriginalFileId;
  originalMedia: OriginalMediaSnapshot;
  sha256: string;
  fileName: string;
  mimeType: string;
  size: number;
  durationMs: number;
  width: number;
  height: number;
  fpsNum: number;
  fpsDen: number;
  playbackAssetId: string;
  playbackUrl: string;
  status: ReviewItemStatus;
  versionNote?: string | null;
  changeSummary?: string | null;
  uploadedAt: string;
  requestedChangesAt: string | null;
}

export interface ReviewThreadMessage {
  messageId: string;
  projectRefId: ProjectRefId;
  reviewItemId: ReviewItemId;
  versionId: VersionId;
  issueId: IssueId;
  body: string;
  createdAt: string;
}

export interface ReviewIssue {
  issueId: IssueId;
  issueNo: number;
  projectRefId: ProjectRefId;
  reviewItemId: ReviewItemId;
  versionId: VersionId;
  status: IssueStatus;
  severity: IssueSeverity;
  currentRevisionId: RevisionId;
  timestampMs: number;
  frameNumber: number;
  lockVersion: number;
  body: string;
  annotationSetId?: AnnotationSetId;
  currentRevision: ReviewIssueRevision;
  currentAnnotationSet: ReviewAnnotationSet | null;
  revisions: ReviewIssueRevision[];
  replies: ReviewThreadMessage[];
  deletedAt?: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface ReviewPlaybackTarget {
  projectRefId: ProjectRefId;
  reviewItemId: ReviewItemId;
  versionId: VersionId;
  issueId: IssueId;
  revisionId: RevisionId;
  annotationSetId?: AnnotationSetId;
  timestampMs: number;
  frameNumber: number;
}

export interface FinalizationRecord {
  finalizationId: FinalizationId;
  projectRefId: ProjectRefId;
  reviewItemId: ReviewItemId;
  versionId: VersionId;
  originalFileId: OriginalFileId;
  sha256: string;
  fileName: string;
  originalMedia: OriginalMediaSnapshot;
  frozenAt: string;
}

export interface StoredOriginalFile {
  originalFileId: OriginalFileId;
  fileName: string;
  mimeType: string;
  size: number;
  sha256: string;
  durationMs: number;
  width: number;
  height: number;
  fpsNum: number;
  fpsDen: number;
  playbackUrl: string;
  blob?: Blob;
}

export interface ProjectDetail {
  project: Project;
  items: ReviewItem[];
  versionsByItem: Record<ReviewItemId, ReviewVersion[]>;
  issuesByVersion: Record<VersionId, ReviewIssue[]>;
  finalizations: FinalizationRecord[];
}

export interface ReviewWorkspace {
  project: Project;
  item: ReviewItem;
  versions: ReviewVersion[];
  currentVersion: ReviewVersion;
  currentIssues: ReviewIssue[];
  historicalIssues: ReviewIssue[];
  activeFinalization: FinalizationRecord | null;
}

export interface FinalCutPackageSnapshot {
  packageId: string;
  projectRefId: ProjectRefId;
  packageFilename: string;
  createdAt: string;
  entries: Array<{
    projectRefId: ProjectRefId;
    reviewItemId: ReviewItemId;
    versionId: VersionId;
    finalizationId: FinalizationId;
    originalFileId: OriginalFileId;
    sha256: string;
    fileName: string;
  }>;
}

export interface PackageResult extends FinalCutPackageSnapshot {
  fileName: string;
  blob?: Blob;
}

export interface ExecutionContext {
  entryMode: EntryMode;
  requestId: string;
  createdAt: string;
}
