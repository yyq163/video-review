import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import type { EntryMode, FinalizationRevocation, ReviewItem } from '../contracts/types';
import { entryLinksFor } from '../entry/entry-links';
import { useProjectSummary, useReviewMutations } from '../entry/use-review-queries';
import { AppShell, CapabilityGate, ErrorView, LoadingBlock, StatusBadge, actionError } from '../components/shared';
import { CreateItemUploadPanel } from '../components/UploadPanel';
import { ProjectMetadataEditor, type ReviewItemMetadataValues } from '../components/MetadataEditors';
import { uploadSchema, type ProjectFormValues } from '../components/ProjectForms';
import type { ReviewItemWithMetadata } from '../ports';
import type { ReviewProjectSummaryItem } from '../ports';
import { groupReviewItemsByEpisode } from '../core/episode-dedupe';
import {
  DeleteReviewItemResultUncertainError,
  V1UploadResultUncertainError,
  clearV1ListConfirmationRequired,
  getV1ListProtectionState,
  markV1ListConfirmationRequired,
} from '../adapters/http-review-uploads';
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
import { ProjectDetailItemList, type ProjectDetailMetadataEpisodeGroup } from './project-detail-item-list';

function revocationSuccessMessage(
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

export function ProjectDetailPage(props: { entryMode: EntryMode }) {
  const navigate = useNavigate();
  const { projectRefId = '' } = useParams();
  const detail = useProjectSummary(props.entryMode, projectRefId);
  const mutations = useReviewMutations(props.entryMode);
  const [projectActionError, setProjectActionError] = useState<string | null>(null);
  const [projectActionMessage, setProjectActionMessage] = useState<string | null>(null);
  const [bulkActionsHost, setBulkActionsHost] = useState<HTMLSpanElement | null>(null);
  const v1ListRefreshSequenceRef = useRef(0);
  const activeV1UploadCountRef = useRef(0);
  const [activeV1UploadCount, setActiveV1UploadCount] = useState(0);
  const [v1ListProtectionState, setV1ListProtectionState] = useState(() => props.entryMode === 'edit' ? getV1ListProtectionState(projectRefId) : 'clear');
  const v1UncertainRef = useRef(v1ListProtectionState !== 'clear');
  const v1ListConfirmationRequired = v1ListProtectionState !== 'clear';
  const [v1ListConfirmationPending, setV1ListConfirmationPending] = useState(false);
  const [revocationProtectionVersion, setRevocationProtectionVersion] = useState(0);
  const revocationReconciliationStatesRef = useRef(
    new Map<string, RevokeFinalizationReconciliationState>(),
  );
  const [revocationAuthorityRequiredIds, setRevocationAuthorityRequiredIds] =
    useState(() => new Set<string>());
  const revocationAuthorityRequiredIdsRef = useRef(
    revocationAuthorityRequiredIds,
  );
  const updateRevocationAuthorityRequired = useCallback(
    (reviewItemId: string, required: boolean) => {
      const current = revocationAuthorityRequiredIdsRef.current;
      if (current.has(reviewItemId) === required) return;
      const next = new Set(current);
      if (required) next.add(reviewItemId);
      else next.delete(reviewItemId);
      revocationAuthorityRequiredIdsRef.current = next;
      setRevocationAuthorityRequiredIds(next);
    },
    [],
  );
  const revocationProtectionById = useMemo(
    () => {
      void revocationProtectionVersion;
      return new Map(
        (detail.data?.items ?? []).map((item) => [
          item.reviewItemId,
          getRevokeFinalizationProtectionState(
            projectRefId,
            item.reviewItemId,
          ),
        ]),
      );
    },
    [detail.data?.items, projectRefId, revocationProtectionVersion],
  );
  const revocationUncertainIds = useMemo(() => {
    const items = detail.data?.items ?? [];
    return new Set(
      items
        .filter(
          (item) =>
            item.status === 'finalized' &&
            item.finalization?.status === 'active' &&
            (revocationProtectionById.get(item.reviewItemId) !== 'clear' ||
              revocationAuthorityRequiredIds.has(item.reviewItemId)),
        )
        .map((item) => item.reviewItemId),
    );
  }, [
    detail.data?.items,
    revocationAuthorityRequiredIds,
    revocationProtectionById,
  ]);
  const revocationUncertainKey = [...revocationUncertainIds].sort().join('|');
  const refetchProjectSummary = detail.refetch;
  const getReviewItemAuthority = mutations.getReviewItemAuthority;
  const syncReviewItemAuthority = mutations.syncReviewItemAuthority;
  const lockReviewItemRevocationUncertain =
    mutations.lockReviewItemRevocationUncertain;
  const replayRevokeFinalization = mutations.revokeFinalization.mutateAsync;
  useEffect(() => {
    const items = detail.data?.items ?? [];
    const activeFinalizedIds = new Set(
      items
        .filter(
          (item) =>
            item.status === 'finalized' &&
            item.finalization?.status === 'active',
        )
        .map((item) => item.reviewItemId),
    );
    for (const reviewItemId of [
      ...revocationAuthorityRequiredIdsRef.current,
    ]) {
      if (!activeFinalizedIds.has(reviewItemId)) {
        updateRevocationAuthorityRequired(reviewItemId, false);
      }
    }
    for (const reviewItemId of activeFinalizedIds) {
      if (revocationProtectionById.get(reviewItemId) !== 'clear') {
        updateRevocationAuthorityRequired(reviewItemId, true);
      }
    }
  }, [
    detail.data?.items,
    revocationProtectionById,
    updateRevocationAuthorityRequired,
  ]);
  useEffect(() => {
    if (!revocationUncertainKey) return;
    const uncertainIds = revocationUncertainKey.split('|');
    const uncertainIdSet = new Set(uncertainIds);
    const reconciliationStates = revocationReconciliationStatesRef.current;
    for (const id of [...reconciliationStates.keys()]) {
      if (!uncertainIdSet.has(id)) reconciliationStates.delete(id);
    }
    let disposed = false;
    let reconciling = false;
    const authorityAbortController = new AbortController();

    const markAttempt = (
      reviewItemId: string,
      authoritativeFinalized: boolean,
    ) => {
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
          revocationAuthorityRequiredIdsRef.current.has(reviewItemId));
      if (!state.exhausted && !(identityUnavailable && state.confirmationAttempts >= 4)) {
        return;
      }
      setProjectActionError(
        identityUnavailable
          ? `撤回结果仍不确定：会话存储不可用，无法恢复原请求身份；已完成 ${state.confirmationAttempts} 次权威查询，未发送新的撤回命令，系统将继续只读查询权威状态，期间保持锁定。`
          : `撤回结果仍不确定：已完成 ${state.confirmationAttempts} 次权威查询和 ${state.replayAttempts} 次同请求安全重试；已停止命令重放，但会持续只读查询权威状态，期间保持锁定。`,
      );
    };

    const reconcile = async () => {
      if (disposed || reconciling) return;
      reconciling = true;
      try {
        for (const reviewItemId of uncertainIds) {
          if (disposed) return;
          const protectionState = getRevokeFinalizationProtectionState(
            projectRefId,
            reviewItemId,
          );
          const expectedOperation = getPendingRevokeFinalizationOperation(
            projectRefId,
            reviewItemId,
          );
          if (protectionState !== 'clear') {
            updateRevocationAuthorityRequired(reviewItemId, true);
          }
          if (
            protectionState === 'clear' &&
            !revocationAuthorityRequiredIdsRef.current.has(reviewItemId)
          ) {
            reconciliationStates.delete(reviewItemId);
            setRevocationProtectionVersion((current) => current + 1);
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
          if (
            item.status !== 'finalized' ||
            !item.activeFinalizationId
          ) {
            const cleared = clearRevokeFinalizationOperation(
              projectRefId,
              reviewItemId,
              expectedOperation?.commandId ?? null,
            );
            if (!cleared) continue;
            syncReviewItemAuthority(item);
            reconciliationStates.delete(reviewItemId);
            updateRevocationAuthorityRequired(reviewItemId, false);
            setRevocationProtectionVersion((current) => current + 1);
            void refetchProjectSummary({ throwOnError: true }).catch(() => {
              setProjectActionError(
                '撤回已生效，但项目列表同步失败；请刷新页面查看清理状态。',
              );
            });
            continue;
          }

          const step = markAttempt(
            reviewItemId,
            protectionState === 'required',
          );
          if (step.shouldReplay && protectionState === 'required') {
            try {
              const result = await replayRevokeFinalization({
                projectRefId,
                reviewItemId,
                confirmed: true,
              });
              if (disposed) return;
              reconciliationStates.delete(reviewItemId);
              setProjectActionError(null);
              setProjectActionMessage(revocationSuccessMessage(item, result));
              syncReviewItemAuthority({ ...item, ...result.reviewItem });
              setRevocationProtectionVersion((current) => current + 1);
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
                  updateRevocationAuthorityRequired(reviewItemId, true);
                  continue;
                }
                reconciliationStates.delete(reviewItemId);
                updateRevocationAuthorityRequired(reviewItemId, false);
                setProjectActionError(
                  `撤回未执行：${actionError(caught)}。已解除结果不确定锁定，可重新确认后撤回。`,
                );
                setRevocationProtectionVersion((current) => current + 1);
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
    refetchProjectSummary,
    replayRevokeFinalization,
    revocationUncertainKey,
    syncReviewItemAuthority,
    updateRevocationAuthorityRequired,
  ]);
  const settleV1UploadAttempt = (mayClearProtection: boolean) => {
    activeV1UploadCountRef.current = Math.max(0, activeV1UploadCountRef.current - 1);
    setActiveV1UploadCount(activeV1UploadCountRef.current);
    if (
      mayClearProtection &&
      activeV1UploadCountRef.current === 0 &&
      !v1UncertainRef.current
    ) {
      clearV1ListConfirmationRequired(projectRefId);
    }
    const nextProtectionState = getV1ListProtectionState(projectRefId);
    setV1ListProtectionState(nextProtectionState);
    return nextProtectionState;
  };
  if (detail.isLoading) {
    return (
      <AppShell entryMode={props.entryMode} homeHref={`/${props.entryMode}/projects`} entryLinks={entryLinksFor(props.entryMode)}>
        <LoadingBlock />
      </AppShell>
    );
  }

  if (!detail.data) {
    return (
      <AppShell entryMode={props.entryMode} homeHref={`/${props.entryMode}/projects`} entryLinks={entryLinksFor(props.entryMode)}>
        <ErrorView error={detail.error ?? new Error('项目不存在')} />
      </AppShell>
    );
  }

  const { project, items } = detail.data;
  const episodeGroups = groupReviewItemsByEpisode(items) as ProjectDetailMetadataEpisodeGroup[];
  const isArchived = project.status === 'archived';
  const projectActionPending =
    mutations.updateProject.isPending ||
    mutations.archiveProject.isPending ||
    mutations.restoreProject.isPending ||
    mutations.deleteProject.isPending;
  const itemActionPending =
    mutations.updateReviewItem.isPending ||
    mutations.deleteReviewItem.isPending ||
    mutations.bulkDeleteReviewItems.isPending ||
    mutations.revokeFinalization.isPending;

  const updateProjectMetadata = async (values: ProjectFormValues) => {
    setProjectActionError(null);
    setProjectActionMessage(null);
    await mutations.updateProject.mutateAsync({ projectRefId, ...values });
    setProjectActionMessage('项目资料已更新。');
  };

  const updateReviewItemMetadata = async (item: ReviewItemWithMetadata, values: ReviewItemMetadataValues) => {
    setProjectActionError(null);
    setProjectActionMessage(null);
    await mutations.updateReviewItem.mutateAsync({ projectRefId, reviewItemId: item.reviewItemId, ...values });
    setProjectActionMessage(`成片「${values.title}」元数据已更新。`);
  };

  const archiveProject = async () => {
    setProjectActionError(null);
    setProjectActionMessage(null);
    try {
      await mutations.archiveProject.mutateAsync({ projectRefId });
      setProjectActionMessage('项目已归档，现有成片、版本、意见、定稿和文件保持可读。');
    } catch (caught) {
      setProjectActionError(actionError(caught));
    }
  };

  const restoreProject = async () => {
    setProjectActionError(null);
    setProjectActionMessage(null);
    try {
      await mutations.restoreProject.mutateAsync({ projectRefId });
      setProjectActionMessage('项目已恢复，可继续剪辑管理。');
    } catch (caught) {
      setProjectActionError(actionError(caught));
    }
  };

  const deleteProject = async () => {
    setProjectActionError(null);
    setProjectActionMessage(null);
    const confirmed = window.confirm(`确认删除项目「${project.name}」？删除后项目会从审阅列表移除，历史记录和媒体文件仍保留。`);
    if (!confirmed) return;
    try {
      await mutations.deleteProject.mutateAsync({ projectRefId, confirmed: true });
      navigate(`/${props.entryMode}/projects`);
    } catch (caught) {
      setProjectActionError(actionError(caught));
    }
  };

  const deleteReviewItem = async (item: ReviewItem) => {
    setProjectActionError(null);
    setProjectActionMessage(null);
    const confirmed = window.confirm(
      `确认删除分集「${item.title}」？该操作仅用于审核开始前去重，会永久删除该分集、唯一未审核版本和可安全释放的原始媒体文件；待清理分片将由维护任务回收，且无法撤销。`,
    );
    if (!confirmed) return;
    try {
      await mutations.deleteReviewItem.mutateAsync({ projectRefId, reviewItemId: item.reviewItemId, confirmed: true });
      setProjectActionMessage('分集已删除，列表已刷新。');
      await detail.refetch();
    } catch (caught) {
      setProjectActionError(actionError(caught));
    }
  };

  const bulkDeleteReviewItems = async (selectedItems: ReviewItem[]) => {
    setProjectActionError(null);
    setProjectActionMessage(null);
    const succeededIds: string[] = [];
    const failures: Record<string, string> = {};
    let uncertainIds: string[] = [];
    const settled = await mutations.bulkDeleteReviewItems.mutateAsync(
      selectedItems.map((item) => ({
          projectRefId,
          reviewItemId: item.reviewItemId,
          confirmed: true,
      })),
    );
    settled.results.forEach((result, index) => {
      const selectedId = settled.inputs[index].reviewItemId;
      if (result.status === 'fulfilled') {
        succeededIds.push(selectedId);
      } else {
        const caught = result.reason;
        if (caught instanceof DeleteReviewItemResultUncertainError) {
          uncertainIds.push(selectedId);
        } else {
          failures[selectedId] = actionError(caught);
        }
      }
    });
    let refreshFailed = false;
    if (uncertainIds.length) {
      try {
        const refreshed = await detail.refetch({ throwOnError: true });
        const remainingIds = new Set(refreshed.data?.items.map((item) => item.reviewItemId) ?? []);
        const stillUncertainIds: string[] = [];
        for (const id of uncertainIds) {
          if (remainingIds.has(id)) {
            stillUncertainIds.push(id);
          } else {
            succeededIds.push(id);
          }
        }
        uncertainIds = stillUncertainIds;
      } catch {
        refreshFailed = true;
      }
    }
    const failedCount = Object.keys(failures).length;
    setProjectActionMessage(
      `批量删除完成：成功 ${succeededIds.length} 条，失败 ${failedCount} 条，不确定 ${uncertainIds.length} 条。${
        refreshFailed
          ? '列表刷新失败；不确定项已锁定，请刷新页面核对，切勿重复删除。'
          : uncertainIds.length
            ? '即时核对仍可见，原请求可能尚在提交；不确定项保持锁定，切勿用新操作重删。'
            : ''
      }`,
    );
    return { succeededIds, failures, uncertainIds };
  };

  const revokeFinalization = async (item: ReviewProjectSummaryItem) => {
    setProjectActionError(null);
    setProjectActionMessage(null);
    try {
      const result = await mutations.revokeFinalization.mutateAsync({
        projectRefId,
        reviewItemId: item.reviewItemId,
        confirmed: true,
      });
      syncReviewItemAuthority({ ...item, ...result.reviewItem });
      setProjectActionMessage(revocationSuccessMessage(item, result));
      setRevocationProtectionVersion((current) => current + 1);
      return result;
    } catch (caught) {
      if (caught instanceof RevokeFinalizationResultUncertainError) {
        lockReviewItemRevocationUncertain(item);
        setRevocationProtectionVersion((current) => current + 1);
      }
      setProjectActionError(
        caught instanceof RevokeFinalizationResultUncertainError
          ? '撤回结果确认中：页面会持续查询权威状态，禁止新建撤回操作。'
          : actionError(caught),
      );
      throw caught;
    }
  };

  const confirmV1List = async () => {
    setProjectActionError(null);
    if (activeV1UploadCountRef.current > 0) {
      setProjectActionError('仍有 V1 上传正在结算，批次结束前不能解除不确定结果保护。');
      return;
    }
    setV1ListConfirmationPending(true);
    try {
      await detail.refetch({ throwOnError: true });
      clearV1ListConfirmationRequired(projectRefId);
      v1UncertainRef.current = false;
      const nextProtectionState = getV1ListProtectionState(projectRefId);
      setV1ListProtectionState(nextProtectionState);
      if (nextProtectionState === 'storage-unavailable') {
        throw new Error('浏览器会话存储不可用，无法安全解除 V1 创建保护。');
      }
      setProjectActionMessage('已确认当前成片列表，可以继续创建新的 V1。');
    } catch (caught) {
      setProjectActionError(`列表刷新失败，尚未解除 V1 创建保护：${actionError(caught)}`);
    } finally {
      setV1ListConfirmationPending(false);
    }
  };

  return (
    <AppShell
      entryMode={props.entryMode}
      homeHref={`/${props.entryMode}/projects`}
      entryLinks={entryLinksFor(props.entryMode)}
      right={
        <>
          {isArchived ? (
            <CapabilityGate entryMode={props.entryMode} capability="review.project.restore">
              <button className="fj-review-secondary" disabled={projectActionPending} onClick={restoreProject} type="button">
                恢复项目
              </button>
            </CapabilityGate>
          ) : (
            <CapabilityGate entryMode={props.entryMode} capability="review.project.archive">
              <button className="fj-review-secondary" disabled={projectActionPending} onClick={archiveProject} type="button">
                归档项目
              </button>
            </CapabilityGate>
          )}
          {!isArchived ? (
            <CapabilityGate entryMode={props.entryMode} capability="review.project.delete">
              <button className="fj-review-secondary is-danger" disabled={projectActionPending} onClick={deleteProject} type="button">
                删除项目
              </button>
            </CapabilityGate>
          ) : null}
          <Link className="fj-review-secondary" to={`/${props.entryMode}/projects`}>
            返回项目
          </Link>
        </>
      }
    >
      <section className="fj-review-page fj-review-project-detail">
        <div className="fj-review-page-heading">
          <div>
            <span>{project.code}</span>
            <div className="fj-review-icon-text">
              <h1>
                {project.name}
                <StatusBadge status={project.status} />
              </h1>
              {!isArchived ? (
                <CapabilityGate entryMode={props.entryMode} capability="review.project.update">
                  <ProjectMetadataEditor project={project} pending={mutations.updateProject.isPending} onSubmit={updateProjectMetadata} />
                </CapabilityGate>
              ) : null}
            </div>
          </div>
          <p>{project.description || '暂无项目说明'}</p>
        </div>
        {projectActionMessage && <div className="fj-review-notice">{projectActionMessage}</div>}
        {projectActionError && <div className="fj-review-error">{projectActionError}</div>}
        {v1ListConfirmationRequired ? (
          <section className="fj-review-readonly-notice" data-testid="v1-list-confirmation-required" role="alert">
            {v1ListProtectionState === 'storage-unavailable' ? (
              <>
                <strong>浏览器会话存储不可用</strong>
                <span>无法可靠保存 V1 不确定结果保护。请恢复浏览器站点存储后重新载入页面。</span>
              </>
            ) : (
              <>
                <strong>请先确认上一笔 V1 的列表结果</strong>
                <span>上一笔 V1 命令的响应不确定。请核对下方成片列表，避免重复创建。</span>
                {activeV1UploadCount > 0 ? (
                  <span>仍有 {activeV1UploadCount} 条 V1 正在结算，全部结束后才能确认。</span>
                ) : null}
                <button
                  className="fj-review-secondary"
                  disabled={v1ListConfirmationPending || activeV1UploadCount > 0}
                  onClick={() => void confirmV1List()}
                  type="button"
                >
                  {v1ListConfirmationPending ? '正在刷新列表...' : '我已核对列表，允许新建 V1'}
                </button>
              </>
            )}
          </section>
        ) : null}
        {isArchived ? (
          <section className="fj-review-readonly-notice" data-testid="archived-readonly-notice">
            <strong>项目已归档</strong>
            <span>归档状态只允许查看既有资料；恢复项目后才能创建成片、上传版本或执行写操作。</span>
          </section>
        ) : (
          <CapabilityGate entryMode={props.entryMode} capability="review.item.create">
            <CreateItemUploadPanel
              pending={mutations.createReviewItemWithVersion.isPending}
              blockedForListConfirmation={v1ListConfirmationRequired}
              setBulkActionsHost={setBulkActionsHost}
              existingEpisodes={items.map((item) => item.episode)}
              onSubmit={async (input, onProgress) => {
                const parsedInput = uploadSchema.safeParse(input);
                if (!parsedInput.success) {
                  return {
                    outcome: 'failed' as const,
                    message: parsedInput.error.issues[0]?.message ?? '成片信息校验失败。',
                  };
                }
                const validatedInput = parsedInput.data;
                setProjectActionError(null);
                setProjectActionMessage(null);
                onProgress?.({ stage: 'validating', percent: 0, totalBytes: validatedInput.file.size });
                activeV1UploadCountRef.current += 1;
                setActiveV1UploadCount(activeV1UploadCountRef.current);
                try {
                  const created = await mutations.createReviewItemWithVersion.mutateAsync({
                    projectRefId,
                    ...validatedInput,
                    onProgress,
                  });
                  onProgress?.({
                    stage: 'completed',
                    percent: 100,
                    bytesSent: validatedInput.file.size,
                    totalBytes: validatedInput.file.size,
                  });
                  const nextProtectionState = settleV1UploadAttempt(true);
                  const stopBatch = nextProtectionState === 'storage-unavailable';
                  if (stopBatch) {
                    setProjectActionError('浏览器会话存储不可用，后续 V1 上传已停止；已成功的文件不会重传。');
                  }
                  const refreshSequence = ++v1ListRefreshSequenceRef.current;
                  void detail.refetch({ throwOnError: true }).then((refreshed) => {
                    if (refreshSequence !== v1ListRefreshSequenceRef.current) return;
                    const confirmedInList = refreshed.data?.items.some(
                      (item) => item.reviewItemId === created.item.reviewItemId,
                    );
                    if (!confirmedInList) {
                      setProjectActionMessage('文件已上传成功，待审列表暂时刷新失败，请刷新页面查看。');
                    }
                  }).catch(() => {
                    if (refreshSequence !== v1ListRefreshSequenceRef.current) return;
                    setProjectActionMessage('文件已上传成功，待审列表暂时刷新失败，请刷新页面查看。');
                  });
                  return { outcome: 'success' as const, stopBatch };
                } catch (caught) {
                  if (caught instanceof V1UploadResultUncertainError) {
                    markV1ListConfirmationRequired(projectRefId);
                    v1UncertainRef.current = true;
                    const nextProtectionState = settleV1UploadAttempt(false);
                    return {
                      outcome: 'uncertain' as const,
                      message: '结果不确定/原因未确认，请先核对待审列表；确认未成功前不会自动重传。',
                      stopBatch: nextProtectionState === 'storage-unavailable',
                    };
                  }
                  settleV1UploadAttempt(true);
                  return { outcome: 'failed' as const, message: actionError(caught) };
                }
              }}
            />
          </CapabilityGate>
        )}
        <ProjectDetailItemList
          entryMode={props.entryMode}
          episodeGroups={episodeGroups}
          isArchived={isArchived}
          itemActionPending={itemActionPending}
          bulkActionHost={bulkActionsHost}
          onBulkDeleteReviewItems={bulkDeleteReviewItems}
          onDeleteReviewItem={(item) => void deleteReviewItem(item)}
          onRevokeFinalization={revokeFinalization}
          onUpdateReviewItemMetadata={updateReviewItemMetadata}
          projectRefId={projectRefId}
          revocationUncertainIds={revocationUncertainIds}
        />
      </section>
    </AppShell>
  );
}
