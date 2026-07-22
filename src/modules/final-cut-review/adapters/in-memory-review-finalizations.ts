import type {
  FinalizationRecord,
  ProjectRefId,
  ReviewItemId,
  ReviewVersion,
  VersionId,
} from '../contracts/types';
import { invariant } from '../core/errors';
import { createUuid } from '../core/uuid';
import { cloneFinalization, cloneVersion, nowIso } from './in-memory-review-clones';
import type { InMemoryReviewStore } from './in-memory-review-store';

interface ReviewTransitionInput {
  projectRefId: ProjectRefId;
  reviewItemId: ReviewItemId;
  versionId: VersionId;
}

export class InMemoryReviewFinalizations {
  constructor(private readonly store: InMemoryReviewStore) {}

  readonly requestChanges = async (input: ReviewTransitionInput): Promise<ReviewVersion> => {
    this.store.assertProjectWritable(input.projectRefId);
    const item = this.store.getItem(input.projectRefId, input.reviewItemId);
    invariant(item.currentVersionId === input.versionId, '只能对当前版本要求修改', 'NOT_CURRENT_VERSION');
    invariant(item.status !== 'finalized', '已定稿后不能要求修改', 'FINALIZED_READONLY');
    invariant(item.status === 'in_review', '只有审阅中版本可以要求修改', 'INVALID_STATUS_TRANSITION');
    const version = this.store.getVersion(input.projectRefId, input.reviewItemId, input.versionId);
    const unresolved = this.store
      .getIssuesForVersion(input.projectRefId, input.reviewItemId, input.versionId)
      .filter((issue) => issue.status === 'unresolved');
    invariant(unresolved.length > 0, '要求修改必须存在当前版本未解决意见', 'NO_UNRESOLVED_ISSUES');
    const timestamp = nowIso();
    const nextVersion: ReviewVersion = {
      ...version,
      status: 'changes_requested',
      requestedChangesAt: timestamp,
    };
    this.store.versions.set(version.versionId, nextVersion);
    this.store.items.set(item.reviewItemId, { ...item, status: 'changes_requested', updatedAt: timestamp });
    this.store.emitChange();
    return cloneVersion(nextVersion);
  };

  readonly finalizeCurrentVersion = async (input: ReviewTransitionInput): Promise<FinalizationRecord> => {
    this.store.assertProjectWritable(input.projectRefId);
    const item = this.store.getItem(input.projectRefId, input.reviewItemId);
    invariant(item.currentVersionId === input.versionId, '只能定稿当前版本', 'NOT_CURRENT_VERSION');
    invariant(
      item.status === 'pending_review' || item.status === 'in_review' || item.status === 'changes_requested',
      '当前状态不能定稿',
      'INVALID_STATUS_TRANSITION',
    );
    const version = this.store.getVersion(input.projectRefId, input.reviewItemId, input.versionId);
    invariant(!item.activeFinalizationId, '当前成片已有定稿记录', 'ACTIVE_FINALIZATION_EXISTS');

    const timestamp = nowIso();
    const finalization: FinalizationRecord = {
      finalizationId: `fin_${createUuid()}`,
      projectRefId: input.projectRefId,
      reviewItemId: input.reviewItemId,
      versionId: input.versionId,
      originalFileId: version.originalFileId,
      sha256: version.sha256,
      fileName: version.fileName,
      originalMedia: { ...version.originalMedia },
      frozenAt: timestamp,
    };
    this.store.finalizations.set(finalization.finalizationId, finalization);
    this.store.versions.set(version.versionId, { ...version, status: 'finalized' });
    this.store.items.set(item.reviewItemId, {
      ...item,
      activeFinalizationId: finalization.finalizationId,
      status: 'finalized',
      updatedAt: timestamp,
    });
    this.store.emitChange();
    return cloneFinalization(finalization);
  };
}
