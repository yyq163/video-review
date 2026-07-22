import type {
  FinalizationRecord,
  IssueId,
  Project,
  ProjectRefId,
  ReviewIssue,
  ReviewItem,
  ReviewItemId,
  ReviewVersion,
  StoredOriginalFile,
  VersionId,
} from '../contracts/types';
import { ReviewDomainError, invariant } from '../core/errors';
import type { ReviewItemWithMetadata } from '../ports';
import {
  cloneFinalization,
  cloneIssue,
  cloneVersion,
  originalMediaFromFile,
} from './in-memory-review-clones';
import type {
  InMemoryReviewRepositoryOptions,
  InMemoryReviewRepositorySnapshot,
} from './in-memory-review-types';

export class InMemoryReviewStore {
  readonly projects = new Map<ProjectRefId, Project>();
  readonly items = new Map<ReviewItemId, ReviewItemWithMetadata>();
  readonly versions = new Map<VersionId, ReviewVersion>();
  readonly issues = new Map<IssueId, ReviewIssue>();
  readonly finalizations = new Map<string, FinalizationRecord>();

  constructor(
    seed: Partial<InMemoryReviewRepositorySnapshot>,
    private readonly options: InMemoryReviewRepositoryOptions,
  ) {
    for (const project of seed.projects ?? []) {
      this.projects.set(project.projectRefId, { ...project, deletedAt: project.deletedAt ?? null });
    }
    for (const item of seed.items ?? []) {
      const persistedItemCode = (item as ReviewItem & { itemCode?: unknown }).itemCode;
      const itemCode = typeof persistedItemCode === 'string' && persistedItemCode ? persistedItemCode : item.episode;
      this.items.set(item.reviewItemId, { ...item, itemCode });
    }
    for (const version of seed.versions ?? []) this.versions.set(version.versionId, cloneVersion(version));
    for (const issue of seed.issues ?? []) this.issues.set(issue.issueId, cloneIssue(issue));
    for (const finalization of seed.finalizations ?? []) {
      this.finalizations.set(finalization.finalizationId, cloneFinalization(finalization));
    }
  }

  readonly snapshot = (): InMemoryReviewRepositorySnapshot => ({
    projects: [...this.projects.values()].map((project) => ({ ...project })),
    items: [...this.items.values()].map((item) => ({ ...item })),
    versions: [...this.versions.values()].map(cloneVersion),
    issues: [...this.issues.values()].map(cloneIssue),
    finalizations: [...this.finalizations.values()].map(cloneFinalization),
  });

  readonly relinkOriginalFiles = (files: StoredOriginalFile[]): void => {
    const filesById = new Map(files.map((file) => [file.originalFileId, file]));
    for (const [versionId, version] of this.versions) {
      const file = filesById.get(version.originalFileId);
      if (!file) continue;
      this.versions.set(versionId, {
        ...version,
        playbackUrl: file.playbackUrl,
        originalMedia: originalMediaFromFile(file),
      });
    }
  };

  getProject(projectRefId: ProjectRefId): Project {
    const project = this.projects.get(projectRefId);
    if (!project) throw new ReviewDomainError('项目不存在或已越界', 'PROJECT_NOT_FOUND');
    return project;
  }

  assertProjectVisible(project: Project): void {
    invariant(!project.deletedAt, '项目不存在或已越界', 'PROJECT_NOT_FOUND');
  }

  assertProjectWritable(projectRefId: ProjectRefId): Project {
    const project = this.getProject(projectRefId);
    invariant(!project.deletedAt, '项目已删除', 'PROJECT_DELETED_READONLY');
    invariant(project.status !== 'archived', '归档项目只读，恢复后才能修改', 'PROJECT_ARCHIVED_READONLY');
    return project;
  }

  getItem(projectRefId: ProjectRefId, reviewItemId: ReviewItemId): ReviewItemWithMetadata {
    const item = this.items.get(reviewItemId);
    if (!item) throw new ReviewDomainError('成片不存在或已越界', 'ITEM_NOT_FOUND');
    invariant(item.projectRefId === projectRefId, '成片不属于当前项目', 'PROJECT_SCOPE_MISMATCH');
    return item;
  }

  getVersion(projectRefId: ProjectRefId, reviewItemId: ReviewItemId, versionId: VersionId): ReviewVersion {
    const version = this.versions.get(versionId);
    if (!version) throw new ReviewDomainError('版本不存在或已越界', 'VERSION_NOT_FOUND');
    invariant(version.projectRefId === projectRefId, '版本不属于当前项目', 'PROJECT_SCOPE_MISMATCH');
    invariant(version.reviewItemId === reviewItemId, '版本不属于当前成片', 'ITEM_SCOPE_MISMATCH');
    return version;
  }

  getIssue(
    projectRefId: ProjectRefId,
    reviewItemId: ReviewItemId,
    versionId: VersionId,
    issueId: IssueId,
    options: { includeDeleted?: boolean } = {},
  ): ReviewIssue {
    const issue = this.issues.get(issueId);
    if (!issue) throw new ReviewDomainError('意见不存在或已越界', 'ISSUE_NOT_FOUND');
    invariant(issue.projectRefId === projectRefId, '意见不属于当前项目', 'PROJECT_SCOPE_MISMATCH');
    invariant(issue.reviewItemId === reviewItemId, '意见不属于当前成片', 'ITEM_SCOPE_MISMATCH');
    invariant(issue.versionId === versionId, '意见不属于当前版本', 'VERSION_SCOPE_MISMATCH');
    invariant(options.includeDeleted || !issue.deletedAt, '意见不存在或已越界', 'ISSUE_NOT_FOUND');
    return issue;
  }

  getVersionsForItem(projectRefId: ProjectRefId, reviewItemId: ReviewItemId): ReviewVersion[] {
    this.getItem(projectRefId, reviewItemId);
    return [...this.versions.values()]
      .filter((version) => version.projectRefId === projectRefId && version.reviewItemId === reviewItemId)
      .sort((left, right) => left.versionNo - right.versionNo)
      .map(cloneVersion);
  }

  getIssuesForVersion(projectRefId: ProjectRefId, reviewItemId: ReviewItemId, versionId: VersionId): ReviewIssue[] {
    this.getVersion(projectRefId, reviewItemId, versionId);
    return [...this.issues.values()]
      .filter(
        (issue) =>
          issue.projectRefId === projectRefId &&
          issue.reviewItemId === reviewItemId &&
          issue.versionId === versionId &&
          !issue.deletedAt,
      )
      .sort((left, right) =>
        (left.status === 'unresolved' ? 0 : 1) - (right.status === 'unresolved' ? 0 : 1) ||
        left.timestampMs - right.timestampMs ||
        left.issueNo - right.issueNo,
      )
      .map(cloneIssue);
  }

  getIssuesForItem(
    projectRefId: ProjectRefId,
    reviewItemId: ReviewItemId,
    options: { includeDeleted?: boolean } = {},
  ): ReviewIssue[] {
    this.getItem(projectRefId, reviewItemId);
    return [...this.issues.values()]
      .filter(
        (issue) =>
          issue.projectRefId === projectRefId &&
          issue.reviewItemId === reviewItemId &&
          (options.includeDeleted || !issue.deletedAt),
      )
      .sort((left, right) => left.issueNo - right.issueNo)
      .map(cloneIssue);
  }

  emitChange(): void {
    this.options.onChange?.();
  }
}
