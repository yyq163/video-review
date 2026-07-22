import type {
  Capability,
  EntryMode,
  ExecutionContext,
  FinalizationRecord,
  IssueId,
  PackageResult,
  Project,
  ProjectDetail,
  ProjectRefId,
  ReviewAnnotationShape,
  ReviewIssue,
  ReviewItem,
  ReviewItemId,
  UploadProgress,
  ReviewVersion,
  ReviewWorkspace,
  StoredOriginalFile,
  VersionId,
} from '../contracts/types';

export interface QueryOptions {
  signal?: AbortSignal;
}

export interface ReviewItemWithMetadata extends ReviewItem {
  readonly itemCode: string;
}

export type ReviewProjectDetail = Omit<ProjectDetail, 'items'> & {
  items: ReviewItemWithMetadata[];
};

export interface ReviewQueryPort {
  listProjects(options?: QueryOptions): Promise<Project[]>;
  getProjectDetail(projectRefId: ProjectRefId, options?: QueryOptions): Promise<ReviewProjectDetail>;
  getWorkspace(
    params: {
      projectRefId: ProjectRefId;
      reviewItemId: ReviewItemId;
      versionId?: VersionId;
    },
    options?: QueryOptions,
  ): Promise<ReviewWorkspace>;
}

export interface ReviewCommandPort {
  createProject(input: { name: string; code: string; description: string }, context: ExecutionContext): Promise<Project>;
  updateProject(
    input: {
      projectRefId: ProjectRefId;
      name: string;
      code: string;
      description: string;
    },
    context: ExecutionContext,
  ): Promise<Project>;
  archiveProject(input: { projectRefId: ProjectRefId }, context: ExecutionContext): Promise<Project>;
  restoreProject(input: { projectRefId: ProjectRefId }, context: ExecutionContext): Promise<Project>;
  deleteProject(input: { projectRefId: ProjectRefId; confirmed: true }, context: ExecutionContext): Promise<Project>;
  createReviewItemWithVersion(
    input: {
      projectRefId: ProjectRefId;
      title: string;
      episode: string;
      file: File;
      onProgress?: (progress: UploadProgress) => void;
    },
    context: ExecutionContext,
  ): Promise<{ item: ReviewItem; version: ReviewVersion }>;
  updateReviewItem(
    input: {
      projectRefId: ProjectRefId;
      reviewItemId: ReviewItemId;
      title: string;
      episode: string;
    },
    context: ExecutionContext,
  ): Promise<ReviewItemWithMetadata>;
  deleteReviewItem(
    input: { projectRefId: ProjectRefId; reviewItemId: ReviewItemId; confirmed: true },
    context: ExecutionContext,
  ): Promise<ReviewItem>;
  appendVersion(
    input: {
      projectRefId: ProjectRefId;
      reviewItemId: ReviewItemId;
      file: File;
      versionNote?: string;
      changeSummary?: string;
      supersedeReason?: string;
      onProgress?: (progress: UploadProgress) => void;
    },
    context: ExecutionContext,
  ): Promise<ReviewVersion>;
  startReview(
    input: {
      projectRefId: ProjectRefId;
      reviewItemId: ReviewItemId;
      versionId: VersionId;
    },
    context: ExecutionContext,
  ): Promise<ReviewVersion>;
  createIssue(
    input: {
      projectRefId: ProjectRefId;
      reviewItemId: ReviewItemId;
      versionId: VersionId;
      timestampMs: number;
      frameNumber: number;
      body: string;
      severity: 'normal' | 'blocking';
      shapes: ReviewAnnotationShape[];
      canvasWidth: number;
      canvasHeight: number;
      videoWidth: number;
      videoHeight: number;
    },
    context: ExecutionContext,
  ): Promise<ReviewIssue>;
  replyToIssue(
    input: {
      projectRefId: ProjectRefId;
      reviewItemId: ReviewItemId;
      versionId: VersionId;
      issueId: IssueId;
      body: string;
    },
    context: ExecutionContext,
  ): Promise<ReviewIssue>;
  editIssue(
    input: {
      projectRefId: ProjectRefId;
      reviewItemId: ReviewItemId;
      versionId: VersionId;
      issueId: IssueId;
      body: string;
      timestampMs: number;
      frameNumber: number;
      shapes: ReviewAnnotationShape[];
      canvasWidth: number;
      canvasHeight: number;
      videoWidth: number;
      videoHeight: number;
    },
    context: ExecutionContext,
  ): Promise<ReviewIssue>;
  resolveIssue(
    input: {
      projectRefId: ProjectRefId;
      reviewItemId: ReviewItemId;
      versionId: VersionId;
      issueId: IssueId;
    },
    context: ExecutionContext,
  ): Promise<ReviewIssue>;
  reopenIssue(
    input: {
      projectRefId: ProjectRefId;
      reviewItemId: ReviewItemId;
      versionId: VersionId;
      issueId: IssueId;
    },
    context: ExecutionContext,
  ): Promise<ReviewIssue>;
  deleteIssue(
    input: {
      projectRefId: ProjectRefId;
      reviewItemId: ReviewItemId;
      versionId: VersionId;
      issueId: IssueId;
    },
    context: ExecutionContext,
  ): Promise<ReviewIssue>;
  requestChanges(
    input: {
      projectRefId: ProjectRefId;
      reviewItemId: ReviewItemId;
      versionId: VersionId;
    },
    context: ExecutionContext,
  ): Promise<ReviewVersion>;
  finalizeCurrentVersion(
    input: {
      projectRefId: ProjectRefId;
      reviewItemId: ReviewItemId;
      versionId: VersionId;
      confirmed: true;
    },
    context: ExecutionContext,
  ): Promise<FinalizationRecord>;
}

export interface FileStoragePort {
  storeOriginal(file: File): Promise<StoredOriginalFile>;
  getOriginal(originalFileId: string): Promise<StoredOriginalFile>;
  seedOriginal(record: StoredOriginalFile): StoredOriginalFile;
}

export interface FinalizedPackagePort {
  createProjectPackage(input: {
    project: Project;
    items: ReviewItem[];
    versions: ReviewVersion[];
    finalizations: FinalizationRecord[];
    fileStorage: FileStoragePort;
  }): Promise<PackageResult>;
}

export interface EntryPolicyPort {
  canEnter(mode: EntryMode): boolean;
  createContext(mode: EntryMode): ExecutionContext;
}

export interface ReviewPermissionAdapter {
  can(mode: EntryMode, capability: Capability): boolean;
  list(mode: EntryMode): Capability[];
}

export interface PrincipalAuthorizationPort {
  assertAuthorized(context: ExecutionContext, capability: Capability): void;
}

export interface WriteGuardPort {
  assertCapability(mode: EntryMode, capability: Capability): void;
  assertSameProject(expected: ProjectRefId, actual: ProjectRefId, label: string): void;
  assertSameItem(expected: ReviewItemId, actual: ReviewItemId, label: string): void;
  assertSameVersion(expected: VersionId, actual: VersionId, label: string): void;
}

export interface ReviewHostBridge {
  entryMode: EntryMode;
  getAuthorizationAdapter?(): PrincipalAuthorizationPort | undefined;
  notify(message: string): void;
  downloadBlob(blob: Blob, fileName: string): void;
  downloadUrl(url: string, fileName: string): void;
}

export interface ReviewApiPort extends ReviewQueryPort, ReviewCommandPort {
  readonly mode: EntryMode;
  readonly entryPolicy: EntryPolicyPort;
  downloadFinalizedOriginal(
    input: {
      projectRefId: ProjectRefId;
      reviewItemId: ReviewItemId;
    },
    context: ExecutionContext,
  ): Promise<StoredOriginalFile>;
  createProjectFinalizedPackage(projectRefId: ProjectRefId, context: ExecutionContext): Promise<PackageResult>;
  downloadProjectFinalizedPackage(result: PackageResult, context: ExecutionContext): Promise<void>;
}
