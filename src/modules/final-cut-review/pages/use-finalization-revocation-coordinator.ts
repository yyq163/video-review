import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { FinalizationRevocation, ReviewItem } from '../contracts/types';
import type { ReviewItemWithMetadata, ReviewProjectSummaryItem } from '../ports';
import { actionError } from '../components/shared';
import { useReviewMutations } from '../entry/use-review-queries';
import {
  clearRevokeFinalizationOperation,
  createRevokeFinalizationReconciliationState,
  getPendingRevokeFinalizationOperation,
  getRevokeFinalizationProtectionState,
  nextRevokeFinalizationReconciliationStep,
  REVOKE_FINALIZATION_RECONCILIATION_INTERVAL_MS,
  RevokeFinalizationResultUncertainError,
  type RevokeFinalizationReconciliationState,
} from '../adapters/http-review-finalization-operation';

type ReviewMutations = ReturnType<typeof useReviewMutations>;

export function revocationSuccessMessage(
  item: Pick<ReviewItem, 'episode'>,
  result: FinalizationRevocation,
): string {
  if (result.cleanupStatus === 'complete') {
    return `第 ${item.episode} 集已撤回定稿，关联包已失效并完成清理。`;
  }
  if (result.cleanupStatus === 'failed') {
    return `第 ${item.episode} 集已撤回定稿；关联包已失效，物理清理失败并等待受控重试。`;
  }
  return `第 ${item.episode} 集已撤回定稿；关联包已失效，物理清理正在进行。`;
}

export function useFinalizationRevocationCoordinator(input: {
  projectRefId: string;
  items: ReviewProjectSummaryItem[];
  mutations: ReviewMutations;
  refreshProjectSummary(): Promise<unknown>;
  reportError(message: string | null): void;
  reportMessage(message: string): void;
}) {
  const {
    items,
    mutations,
    projectRefId,
    refreshProjectSummary,
    reportError,
    reportMessage,
  } = input;
  const [protectionVersion, setProtectionVersion] = useState(0);
  const reconciliationStatesRef = useRef(
    new Map<string, RevokeFinalizationReconciliationState>(),
  );
  const [authorityRequiredIds, setAuthorityRequiredIds] = useState(
    () => new Set<string>(),
  );
  const authorityRequiredIdsRef = useRef(authorityRequiredIds);
  const updateAuthorityRequired = useCallback(
    (reviewItemId: string, required: boolean) => {
      const current = authorityRequiredIdsRef.current;
      if (current.has(reviewItemId) === required) return;
      const next = new Set(current);
      if (required) next.add(reviewItemId);
      else next.delete(reviewItemId);
      authorityRequiredIdsRef.current = next;
      setAuthorityRequiredIds(next);
    },
    [],
  );
  const protectionById = useMemo(() => {
    void protectionVersion;
    return new Map(
      items.map((item) => [
        item.reviewItemId,
        getRevokeFinalizationProtectionState(projectRefId, item.reviewItemId),
      ]),
    );
  }, [items, projectRefId, protectionVersion]);
  const uncertainIds = useMemo(
    () =>
      new Set(
        items
          .filter(
            (item) =>
              item.status === 'finalized' &&
              item.finalization?.status === 'active' &&
              (protectionById.get(item.reviewItemId) !== 'clear' ||
                authorityRequiredIds.has(item.reviewItemId)),
          )
          .map((item) => item.reviewItemId),
      ),
    [authorityRequiredIds, items, protectionById],
  );
  const uncertainKey = [...uncertainIds].sort().join('|');
  const {
    getReviewItemAuthority,
    lockReviewItemRevocationUncertain,
    revokeFinalization,
    syncReviewItemAuthority,
  } = mutations;
  const replayRevokeFinalization = revokeFinalization.mutateAsync;

  useEffect(() => {
    const activeFinalizedIds = new Set(
      items
        .filter(
          (item) =>
            item.status === 'finalized' && item.finalization?.status === 'active',
        )
        .map((item) => item.reviewItemId),
    );
    for (const reviewItemId of [...authorityRequiredIdsRef.current]) {
      if (!activeFinalizedIds.has(reviewItemId)) {
        updateAuthorityRequired(reviewItemId, false);
      }
    }
    for (const reviewItemId of activeFinalizedIds) {
      if (protectionById.get(reviewItemId) !== 'clear') {
        updateAuthorityRequired(reviewItemId, true);
      }
    }
  }, [items, protectionById, updateAuthorityRequired]);

  useEffect(() => {
    if (!uncertainKey) return;
    const ids = uncertainKey.split('|');
    const activeIds = new Set(ids);
    const reconciliationStates = reconciliationStatesRef.current;
    for (const id of [...reconciliationStates.keys()]) {
      if (!activeIds.has(id)) reconciliationStates.delete(id);
    }
    let disposed = false;
    let reconciling = false;
    const authorityAbortController = new AbortController();

    const markAttempt = (reviewItemId: string, authoritativeFinalized: boolean) => {
      const current =
        reconciliationStates.get(reviewItemId) ??
        createRevokeFinalizationReconciliationState();
      const step = nextRevokeFinalizationReconciliationStep(
        current,
        authoritativeFinalized,
      );
      reconciliationStates.set(reviewItemId, step.state);
      return step;
    };

    const reportReplayLimit = (reviewItemId: string) => {
      const state = reconciliationStates.get(reviewItemId);
      if (!state) return;
      const protectionState = getRevokeFinalizationProtectionState(
        projectRefId,
        reviewItemId,
      );
      const identityUnavailable =
        protectionState === 'storage-unavailable' ||
        (protectionState === 'clear' &&
          authorityRequiredIdsRef.current.has(reviewItemId));
      if (!state.exhausted && !(identityUnavailable && state.confirmationAttempts >= 4)) {
        return;
      }
      reportError(
        identityUnavailable
          ? `撤回结果仍不确定：会话存储不可用，无法恢复原请求身份；已完成 ${state.confirmationAttempts} 次权威查询，未发送新的撤回命令，系统将继续只读查询权威状态，期间保持锁定。`
          : `撤回结果仍不确定：已完成 ${state.confirmationAttempts} 次权威查询和 ${state.replayAttempts} 次同请求安全重试；已停止命令重放，但会持续只读查询权威状态，期间保持锁定。`,
      );
    };

    const reconcile = async () => {
      if (disposed || reconciling) return;
      reconciling = true;
      try {
        for (const reviewItemId of ids) {
          if (disposed) return;
          const protectionState = getRevokeFinalizationProtectionState(
            projectRefId,
            reviewItemId,
          );
          const expectedOperation = getPendingRevokeFinalizationOperation(
            projectRefId,
            reviewItemId,
          );
          if (protectionState !== 'clear') updateAuthorityRequired(reviewItemId, true);
          if (
            protectionState === 'clear' &&
            !authorityRequiredIdsRef.current.has(reviewItemId)
          ) {
            reconciliationStates.delete(reviewItemId);
            setProtectionVersion((current) => current + 1);
            continue;
          }
          let item: ReviewItemWithMetadata;
          try {
            item = await getReviewItemAuthority(
              { projectRefId, reviewItemId },
              { signal: authorityAbortController.signal },
            );
          } catch {
            if (disposed) return;
            markAttempt(reviewItemId, false);
            reportReplayLimit(reviewItemId);
            continue;
          }
          if (disposed) return;
          if (item.status !== 'finalized' || !item.activeFinalizationId) {
            const cleared = clearRevokeFinalizationOperation(
              projectRefId,
              reviewItemId,
              expectedOperation?.commandId ?? null,
            );
            if (!cleared) continue;
            syncReviewItemAuthority(item);
            reconciliationStates.delete(reviewItemId);
            updateAuthorityRequired(reviewItemId, false);
            setProtectionVersion((current) => current + 1);
            void refreshProjectSummary().catch(() => {
              reportError('撤回已生效，但项目列表同步失败；请刷新页面查看清理状态。');
            });
            continue;
          }

          const step = markAttempt(reviewItemId, protectionState === 'required');
          if (step.shouldReplay && protectionState === 'required') {
            try {
              const result = await replayRevokeFinalization({
                projectRefId,
                reviewItemId,
                confirmed: true,
              });
              if (disposed) return;
              reconciliationStates.delete(reviewItemId);
              reportError(null);
              reportMessage(revocationSuccessMessage(item, result));
              syncReviewItemAuthority({ ...item, ...result.reviewItem });
              setProtectionVersion((current) => current + 1);
              continue;
            } catch (caught) {
              if (disposed) return;
              if (!(caught instanceof RevokeFinalizationResultUncertainError)) {
                clearRevokeFinalizationOperation(
                  projectRefId,
                  reviewItemId,
                  expectedOperation?.commandId ?? null,
                );
                if (
                  getRevokeFinalizationProtectionState(
                    projectRefId,
                    reviewItemId,
                  ) !== 'clear'
                ) {
                  updateAuthorityRequired(reviewItemId, true);
                  continue;
                }
                reconciliationStates.delete(reviewItemId);
                updateAuthorityRequired(reviewItemId, false);
                syncReviewItemAuthority(item);
                reportError(
                  `撤回未执行：${actionError(caught)}。已解除结果不确定锁定，可重新确认后撤回。`,
                );
                setProtectionVersion((current) => current + 1);
                continue;
              }
            }
          }
          reportReplayLimit(reviewItemId);
        }
      } finally {
        reconciling = false;
      }
    };

    void reconcile();
    const interval = window.setInterval(
      () => void reconcile(),
      REVOKE_FINALIZATION_RECONCILIATION_INTERVAL_MS,
    );
    return () => {
      disposed = true;
      authorityAbortController.abort();
      window.clearInterval(interval);
    };
  }, [
    getReviewItemAuthority,
    projectRefId,
    refreshProjectSummary,
    reportError,
    reportMessage,
    replayRevokeFinalization,
    syncReviewItemAuthority,
    uncertainKey,
    updateAuthorityRequired,
  ]);

  const revoke = useCallback(
    async (item: ReviewProjectSummaryItem) => {
      reportError(null);
      try {
        const result = await replayRevokeFinalization({
          projectRefId,
          reviewItemId: item.reviewItemId,
          confirmed: true,
        });
        syncReviewItemAuthority({ ...item, ...result.reviewItem });
        reportMessage(revocationSuccessMessage(item, result));
        setProtectionVersion((current) => current + 1);
        return result;
      } catch (caught) {
        if (caught instanceof RevokeFinalizationResultUncertainError) {
          lockReviewItemRevocationUncertain(item);
          setProtectionVersion((current) => current + 1);
        }
        reportError(
          caught instanceof RevokeFinalizationResultUncertainError
            ? '撤回结果确认中：页面会持续查询权威状态，禁止新建撤回操作。'
            : actionError(caught),
        );
        throw caught;
      }
    },
    [
      lockReviewItemRevocationUncertain,
      projectRefId,
      reportError,
      reportMessage,
      replayRevokeFinalization,
      syncReviewItemAuthority,
    ],
  );

  return { revokeFinalization: revoke, revocationUncertainIds: uncertainIds };
}
