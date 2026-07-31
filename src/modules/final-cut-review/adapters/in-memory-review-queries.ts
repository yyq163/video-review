import type {
  FinalizationRecord,
  IssueId,
  Project,
  ProjectRefId,
  ReviewIssue,
  ReviewItemId,
  ReviewVersion,
  ReviewWorkspace,
  VersionId,
} from '../contracts/types';
import { invariant } from '../core/errors';
import type {
  ReviewItemWithMetadata,
  ReviewProjectSummary,
} from '../ports';
import { cloneFinalization, cloneIssue, cloneVersion } from './in-memory-review-clones';
import type { InMemoryReviewStore } from './in-memory-review-store';

export class InMemoryReviewQueries {
  constructor(private readonly store: InMemoryReviewStore) {}

  readonly listProjects = async (): Promise<Project[]> =>
    [...this.store.projects.values()]
      .filter((project) => !project.deletedAt)
      .map((project) => {
        const items = [...this.store.items.values()].filter(
          (item) => item.projectRefId === project.projectRefId,
        );
        return {
          ...project,
          completionStatus:
            items.length === 0
              ? 'empty'
              : items.every((item) => item.status === 'finalized')
                ? 'completed'
                : 'in_progress',
        };
      });

  readonly getProjectSummary = async (
    projectRefId: ProjectRefId,
  ): Promise<ReviewProjectSummary> => {
    const project = this.store.getProject(projectRefId);
    this.store.assertProjectVisible(project);
    const items = [...this.store.items.values()]
      .filter((item) => item.projectRefId === projectRefId)
      .sort((left, right) => left.episode.localeCompare(right.episode, 'zh-CN', { numeric: true }))
      .map((item) => {
        const versions = this.store.getVersionsForItem(projectRefId, item.reviewItemId);
        const currentVersion = versions.find(
          (version) => version.versionId === item.currentVersionId,
        );
        invariant(currentVersion, '当前版本不存在', 'VERSION_NOT_FOUND');
        const currentIssues = this.store.getIssuesForVersion(
          projectRefId,
          item.reviewItemId,
          item.currentVersionId,
        );
        const activeFinalization = item.activeFinalizationId
          ? this.store.finalizations.get(item.activeFinalizationId) ?? null
          : null;
        const bulkDeleteEligible =
          project.status !== 'archived' &&
          item.status === 'pending_review' &&
          !item.activeFinalizationId &&
          versions.length === 1 &&
          currentIssues.length === 0;
        return {
          ...item,
          lockVersion: 1,
          currentVersion: {
            id: currentVersion.versionId,
            versionNo: currentVersion.versionNo,
            versionLabel: currentVersion.label,
            durationMs: currentVersion.durationMs,
            fileSize: currentVersion.size,
            playbackStatus: currentVersion.playbackStatus,
            playbackUrl:
              currentVersion.playbackStatus === 'ready'
                ? currentVersion.playbackUrl
                : null,
            thumbnailStatus: currentVersion.thumbnailUrl ? 'ready' as const : 'pending' as const,
            thumbnailUrl: currentVersion.thumbnailUrl,
          },
          unresolvedCurrentVersionCount: currentIssues.filter(
            (issue) => issue.status === 'unresolved' && !issue.deletedAt,
          ).length,
          finalization: activeFinalization
            ? {
                id: activeFinalization.finalizationId,
                status: activeFinalization.status ?? 'active',
                revokedAt: activeFinalization.revokedAt ?? null,
              }
            : null,
          revocationCleanupStatus: 'none' as const,
          bulkDelete: {
            eligible: bulkDeleteEligible,
            locked: false,
            reason: bulkDeleteEligible ? null : '当前条目不符合删除条件',
          },
        };
      });
    return {
      project: {
        ...project,
        completionStatus:
          items.length === 0
            ? 'empty'
            : items.every((item) => item.status === 'finalized')
              ? 'completed'
              : 'in_progress',
      },
      items,
    };
  };

  readonly getWorkspace = async (input: {
    projectRefId: ProjectRefId;
    reviewItemId: ReviewItemId;
    versionId?: VersionId;
  }): Promise<ReviewWorkspace> => {
    const project = this.store.getProject(input.projectRefId);
    this.store.assertProjectVisible(project);
    const item = this.store.getItem(input.projectRefId, input.reviewItemId);
    const versions = this.store.getVersionsForItem(input.projectRefId, input.reviewItemId);
    const targetVersionId = input.versionId ?? item.currentVersionId;
    const currentVersion = versions.find((version) => version.versionId === targetVersionId);
    invariant(currentVersion, '版本不存在或已越界', 'VERSION_NOT_FOUND');
    const currentIssues = this.store.getIssuesForVersion(
      input.projectRefId,
      input.reviewItemId,
      currentVersion.versionId,
    );
    const unresolvedCurrentVersionCount = this.store
      .getIssuesForVersion(
        input.projectRefId,
        input.reviewItemId,
        item.currentVersionId,
      )
      .filter((issue) => issue.status === 'unresolved' && !issue.deletedAt).length;
    const activeFinalization = item.activeFinalizationId
      ? this.store.finalizations.get(item.activeFinalizationId) ?? null
      : null;

    return {
      project: { ...project },
      item: { ...item, unresolvedCurrentVersionCount },
      versions,
      currentVersion,
      currentIssues,
      historicalIssues: [],
      activeFinalization: activeFinalization ? cloneFinalization(activeFinalization) : null,
    };
  };

  readonly getVersionIssues = async (input: {
    projectRefId: ProjectRefId;
    reviewItemId: ReviewItemId;
    versionId: VersionId;
  }): Promise<ReviewIssue[]> =>
    this.store.getIssuesForVersion(input.projectRefId, input.reviewItemId, input.versionId);

  readonly getIssueDetail = async (input: {
    projectRefId: ProjectRefId;
    reviewItemId: ReviewItemId;
    versionId: VersionId;
    issueId: IssueId;
  }): Promise<ReviewIssue> =>
    cloneIssue(
      this.store.getIssue(
        input.projectRefId,
        input.reviewItemId,
        input.versionId,
        input.issueId,
      ),
    );

  readonly getActiveFinalizations = (projectRefId: ProjectRefId): FinalizationRecord[] =>
    [...this.store.items.values()]
      .filter((item) => item.projectRefId === projectRefId && item.activeFinalizationId)
      .flatMap((item) => {
        const finalization = item.activeFinalizationId
          ? this.store.finalizations.get(item.activeFinalizationId)
          : null;
        return finalization ? [cloneFinalization(finalization)] : [];
      });

  readonly getAllProjectVersions = (projectRefId: ProjectRefId): ReviewVersion[] =>
    [...this.store.versions.values()]
      .filter((version) => version.projectRefId === projectRefId)
      .map(cloneVersion);

  readonly getAllProjectItems = (projectRefId: ProjectRefId): ReviewItemWithMetadata[] =>
    [...this.store.items.values()]
      .filter((item) => item.projectRefId === projectRefId)
      .map((item) => ({ ...item }));
}
