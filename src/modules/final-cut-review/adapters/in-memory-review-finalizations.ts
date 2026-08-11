import type {
  FinalizationCleanupRetry,
  FinalizationRecord,
  FinalizationRevocation,
  ProjectRefId,
  ReviewItemId,
  VersionId,
} from '../contracts/types';
import { invariant } from '../core/errors';
import { createUuid } from '../core/uuid';
import { cloneFinalization, nowIso } from './in-memory-review-clones';
import type { InMemoryReviewStore } from './in-memory-review-store';

interface ReviewTransitionInput {
  projectRefId: ProjectRefId;
  reviewItemId: ReviewItemId;
  versionId: VersionId;
}

export class InMemoryReviewFinalizations {
  constructor(private readonly store: InMemoryReviewStore) {}

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

  readonly revokeFinalization = async (input: {
    projectRefId: ProjectRefId;
    reviewItemId: ReviewItemId;
  }): Promise<FinalizationRevocation> => {
    this.store.assertProjectWritable(input.projectRefId);
    const item = this.store.getItem(input.projectRefId, input.reviewItemId);
    invariant(item.status === 'finalized' && item.activeFinalizationId, '当前条目未定稿', 'INVALID_STATUS_TRANSITION');
    const finalization = this.store.finalizations.get(item.activeFinalizationId);
    invariant(finalization, '定稿记录不存在', 'RESOURCE_NOT_FOUND');
    const timestamp = nowIso();
    const revoked = { ...finalization, status: 'revoked' as const, revokedAt: timestamp };
    const updatedItem = {
      ...item,
      activeFinalizationId: null,
      status: 'in_review' as const,
      updatedAt: timestamp,
    };
    const version = this.store.getVersion(input.projectRefId, input.reviewItemId, item.currentVersionId);
    this.store.finalizations.set(revoked.finalizationId, revoked);
    this.store.items.set(updatedItem.reviewItemId, updatedItem);
    this.store.versions.set(version.versionId, { ...version, status: 'in_review' });
    this.store.emitChange();
    return {
      finalization: cloneFinalization(revoked),
      reviewItem: { ...updatedItem },
      cleanupStatus: 'complete',
      invalidatedPackageIds: [],
    };
  };

  readonly retryFinalizationCleanup = async (input: {
    projectRefId: ProjectRefId;
    reviewItemId: ReviewItemId;
    lockVersion: number;
  }): Promise<FinalizationCleanupRetry> => {
    this.store.assertProjectWritable(input.projectRefId);
    const item = this.store.getItem(input.projectRefId, input.reviewItemId);
    invariant(input.lockVersion === 1, '条目版本已变化，请刷新后重试', 'OPTIMISTIC_LOCK_CONFLICT');
    invariant(
      item.status !== 'finalized' && item.activeFinalizationId === null,
      '当前分集仍处于定稿状态，不能重试撤回清理',
      'INVALID_STATUS_TRANSITION',
    );
    const revokedFinalization = [...this.store.finalizations.values()].find(
      (finalization) =>
        finalization.projectRefId === input.projectRefId &&
        finalization.reviewItemId === input.reviewItemId &&
        finalization.status === 'revoked',
    );
    invariant(revokedFinalization, '当前分集没有已撤回的定稿记录', 'RESOURCE_NOT_FOUND');
    return {
      projectRefId: input.projectRefId,
      reviewItemId: input.reviewItemId,
      cleanupStatus: 'complete',
      requeuedPackageIds: [],
    };
  };
}
