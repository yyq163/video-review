import type {
  Capability,
  EntryMode,
  ExecutionContext,
  FinalizationCleanupRetry,
  FinalizationRecord,
  FinalizationRevocation,
  IssueId,
  PackageResult,
  Project,
  ProjectRefId,
  ReviewFrameRate,
  ReviewAnnotationShape,
  ReviewIssue,
  ReviewItem,
  ReviewItemId,
  UploadProgress,
  ReviewVersion,
  ReviewWorkspace,
  ResponsibleEditor,
  StoredOriginalFile,
  VersionId,
} from '../contracts/types';

export interface QueryOptions {
  signal?: AbortSignal;
}

export interface MediaMetadata {
  readonly durationMs: number;
  readonly width: number;
  readonly height: number;
  readonly bitrateBps: number;
  readonly fpsNum: number;
  readonly fpsDen: number;
  readonly isVariableFrameRate: false;
  readonly probeVersion: 'mediainfo.js-0.3.7';
}

export interface MediaMetadataProbeOptions {
  readonly signal?: AbortSignal;
  readonly expectedFrameRate?: ReviewFrameRate;
}

export interface MediaMetadataPort {
  probe(blob: Blob, options?: MediaMetadataProbeOptions): Promise<MediaMetadata>;
}

export interface ReviewItemWithMetadata extends ReviewItem {
  readonly itemCode: string;
}

export interface ReviewProjectSummaryItem extends Omit<ReviewItemWithMetadata, 'createdAt' | 'updatedAt'> {
  readonly lockVersion: number;
  readonly currentVersion: {
    id: VersionId;
    versionNo: number;
    versionLabel: string;
    durationMs: number;
    fileSize: number;
    width: number;
    height: number;
    fpsNum: number;
    fpsDen: number;
    responsibleEditorId: string | null;
    responsibleEditorNameSnapshot: string | null;
    playbackStatus: 'pending' | 'ready' | 'failed';
    playbackUrl: string | null;
    thumbnailStatus: 'pending' | 'ready' | 'failed';
    thumbnailUrl: string | null;
  };
  readonly unresolvedCurrentVersionCount: number;
  readonly finalization: {
    id: FinalizationRecord['finalizationId'];
    status: 'active' | 'revoked';
    revokedAt: string | null;
  } | null;
  readonly revocationCleanupStatus: 'none' | 'pending' | 'failed' | 'complete';
  readonly bulkDelete: {
    eligible: boolean;
    locked: boolean;
    reason: string | null;
  };
}

export interface ReviewProjectSummary {
  readonly project: Project;
  readonly items: ReviewProjectSummaryItem[];
}

export interface TechnicalQcRunningEpisode {
  readonly reviewItemId: ReviewItemId;
  readonly episodeNo: number | null;
  readonly processedMs: number;
  readonly totalMs: number;
}

export interface TechnicalQcProgress {
  readonly episodes: TechnicalQcRunningEpisode[];
}

export interface ReviewQueryPort {
  listProjects(options?: QueryOptions): Promise<Project[]>;
  listResponsibleEditors(options?: QueryOptions): Promise<ResponsibleEditor[]>;
  getProjectSummary(projectRefId: ProjectRefId, options?: QueryOptions): Promise<ReviewProjectSummary>;
  getTechnicalQcProgress(projectRefId: ProjectRefId, options?: QueryOptions): Promise<TechnicalQcProgress>;
  getReviewItem(
    params: { projectRefId: ProjectRefId; reviewItemId: ReviewItemId },
    options?: QueryOptions,
  ): Promise<ReviewItemWithMetadata>;
  getVersion(
    params: {
      projectRefId: ProjectRefId;
      reviewItemId: ReviewItemId;
      versionId: VersionId;
    },
    options?: QueryOptions,
  ): Promise<ReviewVersion>;
  getVersionIssues(
    params: {
      projectRefId: ProjectRefId;
      reviewItemId: ReviewItemId;
      versionId: VersionId;
    },
    options?: QueryOptions,
  ): Promise<ReviewIssue[]>;
  getIssueDetail(
    params: {
      projectRefId: ProjectRefId;
      reviewItemId: ReviewItemId;
      versionId: VersionId;
      issueId: IssueId;
    },
    options?: QueryOptions,
  ): Promise<ReviewIssue>;
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
  createProject(
    input: { name: string; code: string; description: string; frameRate?: ReviewFrameRate },
    context: ExecutionContext,
  ): Promise<Project>;
  updateProject(
    input: {
      projectRefId: ProjectRefId;
      name: string;
      code: string;
      description: string;
    },
    context: ExecutionContext,
  ): Promise<Project>;
  lockProjectFrameRate(
    input: { projectRefId: ProjectRefId; frameRate: ReviewFrameRate },
    context: ExecutionContext,
  ): Promise<Project>;
  createResponsibleEditor(
    input: { displayName: string },
    context: ExecutionContext,
  ): Promise<ResponsibleEditor>;
  renameResponsibleEditor(
    input: { responsibleEditorId: string; displayName: string; lockVersion: number },
    context: ExecutionContext,
  ): Promise<ResponsibleEditor>;
  archiveProject(input: { projectRefId: ProjectRefId }, context: ExecutionContext): Promise<Project>;
  restoreProject(input: { projectRefId: ProjectRefId }, context: ExecutionContext): Promise<Project>;
  deleteProject(input: { projectRefId: ProjectRefId; confirmed: true }, context: ExecutionContext): Promise<Project>;
  finalizeProject(
    input: { projectRefId: ProjectRefId; confirmed: true },
    context: ExecutionContext,
  ): Promise<ReviewProjectSummary>;
  revokeProjectFinalization(
    input: { projectRefId: ProjectRefId; confirmed: true },
    context: ExecutionContext,
  ): Promise<ReviewProjectSummary>;
  createReviewItemWithVersion(
    input: {
      projectRefId: ProjectRefId;
      title: string;
      episode: string;
      file: File;
      responsibleEditorId?: string;
      responsibleEditorNameSnapshot?: string;
      localProbe?: MediaMetadata;
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
  deleteReviewItemsBatch(
    input: {
      projectRefId: ProjectRefId;
      reviewItemIds: ReviewItemId[];
      confirmed: true;
    },
    context: ExecutionContext,
  ): Promise<{
    projectRefId: ProjectRefId;
    results: Array<{
      projectRefId: ProjectRefId;
      reviewItemId: ReviewItemId;
      outcome: 'deleted' | 'failed' | 'outcome_unknown';
      errorCode: string | null;
    }>;
  }>;
  appendVersion(
    input: {
      projectRefId: ProjectRefId;
      reviewItemId: ReviewItemId;
      file: File;
      responsibleEditorId?: string;
      responsibleEditorNameSnapshot?: string;
      localProbe?: MediaMetadata;
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
  finalizeCurrentVersion(
    input: {
      projectRefId: ProjectRefId;
      reviewItemId: ReviewItemId;
      versionId: VersionId;
      confirmed: true;
    },
    context: ExecutionContext,
  ): Promise<FinalizationRecord>;
  revokeFinalization(
    input: {
      projectRefId: ProjectRefId;
      reviewItemId: ReviewItemId;
      confirmed: true;
    },
    context: ExecutionContext,
  ): Promise<FinalizationRevocation>;
  retryFinalizationCleanup(
    input: {
      projectRefId: ProjectRefId;
      reviewItemId: ReviewItemId;
      lockVersion: number;
    },
    context: ExecutionContext,
  ): Promise<FinalizationCleanupRetry>;
}

export interface FileStoragePort {
  storeOriginal(file: File, metadata?: MediaMetadata): Promise<StoredOriginalFile>;
  getOriginal(originalFileId: string): Promise<StoredOriginalFile>;
  seedOriginal(record: StoredOriginalFile): StoredOriginalFile;
}

export interface FinalizedPackagePort {
  createProjectPackage(input: {
    unifiedName: string;
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
  createProjectFinalizedPackage(
    input: { projectRefId: ProjectRefId; unifiedName: string },
    context: ExecutionContext,
    options?: QueryOptions,
  ): Promise<PackageResult>;
  downloadProjectFinalizedPackage(
    result: PackageResult,
    context: ExecutionContext,
    options?: QueryOptions,
  ): Promise<void>;
}
