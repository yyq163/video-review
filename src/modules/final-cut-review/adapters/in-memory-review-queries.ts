import type {
  FinalizationRecord,
  Project,
  ProjectRefId,
  ReviewItemId,
  ReviewVersion,
  ReviewWorkspace,
  VersionId,
} from '../contracts/types';
import { invariant } from '../core/errors';
import type { ReviewItemWithMetadata, ReviewProjectDetail } from '../ports';
import { cloneFinalization, cloneIssue, cloneVersion } from './in-memory-review-clones';
import type { InMemoryReviewStore } from './in-memory-review-store';

export class InMemoryReviewQueries {
  constructor(private readonly store: InMemoryReviewStore) {}

  readonly listProjects = async (): Promise<Project[]> =>
    [...this.store.projects.values()]
      .filter((project) => !project.deletedAt)
      .map((project) => ({ ...project }));

  readonly getProjectDetail = async (projectRefId: ProjectRefId): Promise<ReviewProjectDetail> => {
    const project = this.store.getProject(projectRefId);
    this.store.assertProjectVisible(project);
    const items = [...this.store.items.values()]
      .filter((item) => item.projectRefId === projectRefId)
      .sort((left, right) => left.episode.localeCompare(right.episode, 'zh-CN'))
      .map((item) => ({ ...item }));
    const versionsByItem: ReviewProjectDetail['versionsByItem'] = {};
    const issuesByVersion: ReviewProjectDetail['issuesByVersion'] = {};

    for (const item of items) {
      versionsByItem[item.reviewItemId] = this.store.getVersionsForItem(projectRefId, item.reviewItemId);
      for (const version of versionsByItem[item.reviewItemId]) {
        issuesByVersion[version.versionId] = this.store.getIssuesForVersion(
          projectRefId,
          item.reviewItemId,
          version.versionId,
        );
      }
    }

    return {
      project: { ...project },
      items,
      versionsByItem,
      issuesByVersion,
      finalizations: this.getActiveFinalizations(projectRefId),
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
    const historicalIssues = [...this.store.issues.values()]
      .filter(
        (issue) =>
          issue.projectRefId === input.projectRefId &&
          issue.reviewItemId === input.reviewItemId &&
          issue.versionId !== currentVersion.versionId &&
          !issue.deletedAt,
      )
      .sort((left, right) => left.versionId.localeCompare(right.versionId) || left.timestampMs - right.timestampMs)
      .map(cloneIssue);
    const activeFinalization = item.activeFinalizationId
      ? this.store.finalizations.get(item.activeFinalizationId) ?? null
      : null;

    return {
      project: { ...project },
      item: { ...item },
      versions,
      currentVersion,
      currentIssues,
      historicalIssues,
      activeFinalization: activeFinalization ? cloneFinalization(activeFinalization) : null,
    };
  };

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
