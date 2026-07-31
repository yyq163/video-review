import { useEffect, useMemo, useState } from 'react';
import { createPortal } from 'react-dom';
import { Link } from 'react-router-dom';
import { CapabilityGate, EmptyState, IconText, StatusBadge } from '../components/shared';
import { ReviewItemMetadataEditor, type ReviewItemMetadataValues } from '../components/MetadataEditors';
import type { EntryMode, FinalizationRevocation, ReviewItem } from '../contracts/types';
import { formatEpisodeDisplayValue, type ReviewEpisodeGroup } from '../core/episode-dedupe';
import type { ReviewProjectSummaryItem } from '../ports';
import { RevokeFinalizationResultUncertainError } from '../adapters/http-review-finalization-operation';

export type ProjectDetailMetadataEpisodeGroup = Omit<ReviewEpisodeGroup, 'items' | 'representative'> & {
  items: ReviewProjectSummaryItem[];
  representative: ReviewProjectSummaryItem;
};

type RevokeUiState = 'pending' | 'uncertain';

interface ProjectDetailItemListProps {
  entryMode: EntryMode;
  episodeGroups: ProjectDetailMetadataEpisodeGroup[];
  isArchived: boolean;
  itemActionPending: boolean;
  bulkActionHost?: HTMLElement | null;
  onBulkDeleteReviewItems?: (
    items: ReviewItem[],
  ) => Promise<{
    succeededIds: string[];
    failures: Record<string, string>;
    uncertainIds: string[];
  }>;
  onDeleteReviewItem: (item: ReviewItem) => void;
  onRevokeFinalization?: (item: ReviewProjectSummaryItem) => Promise<FinalizationRevocation>;
  revocationUncertainIds?: ReadonlySet<string>;
  onUpdateReviewItemMetadata: (
    item: ReviewProjectSummaryItem,
    values: ReviewItemMetadataValues,
  ) => Promise<void>;
  projectRefId: string;
}

function isControlTarget(target: EventTarget | null): boolean {
  return target instanceof Element && Boolean(
    target.closest('a, button, input, textarea, select, label, summary'),
  );
}

function thumbnailState(item: ReviewProjectSummaryItem) {
  if (
    item.currentVersion.thumbnailStatus === 'ready' &&
    item.currentVersion.thumbnailUrl
  ) {
    return (
      <img
        alt={`第${formatEpisodeDisplayValue(item.episode)}集 ${item.currentVersion.versionLabel} 首帧`}
        className="fj-review-item-thumbnail"
        data-testid={`item-row-thumbnail-${item.reviewItemId}`}
        loading="lazy"
        src={item.currentVersion.thumbnailUrl}
      />
    );
  }
  return (
    <span
      className={`fj-review-item-thumbnail-placeholder is-${item.currentVersion.thumbnailStatus}`}
      data-testid={`item-row-thumbnail-${item.reviewItemId}`}
    >
      {item.currentVersion.thumbnailStatus === 'failed' ? '首帧生成失败' : '首帧生成中'}
    </span>
  );
}

export function ProjectDetailItemList({
  entryMode,
  episodeGroups,
  isArchived,
  itemActionPending,
  bulkActionHost,
  onBulkDeleteReviewItems,
  onDeleteReviewItem,
  onRevokeFinalization,
  revocationUncertainIds,
  onUpdateReviewItemMetadata,
  projectRefId,
}: ProjectDetailItemListProps) {
  const [selectedDeleteIds, setSelectedDeleteIds] = useState<Set<string>>(() => new Set());
  const [batchDeleteFailures, setBatchDeleteFailures] = useState<Record<string, string>>({});
  const [batchDeleteUncertainIds, setBatchDeleteUncertainIds] = useState<Set<string>>(() => new Set());
  const [locallyDeletedIds, setLocallyDeletedIds] = useState<Set<string>>(() => new Set());
  const [batchDeletePending, setBatchDeletePending] = useState(false);
  const [revokeUiStates, setRevokeUiStates] = useState<Record<string, RevokeUiState>>({});
  const visibleItems = useMemo(
    () =>
      episodeGroups
        .flatMap((group) => group.items)
        .filter((item) => !locallyDeletedIds.has(item.reviewItemId)),
    [episodeGroups, locallyDeletedIds],
  );
  const serverEligibleItems = useMemo(
    () =>
      visibleItems.filter(
        (item) =>
          !isArchived &&
          item.bulkDelete.eligible &&
          !item.bulkDelete.locked &&
          !batchDeleteUncertainIds.has(item.reviewItemId),
      ),
    [batchDeleteUncertainIds, isArchived, visibleItems],
  );
  const serverEligibleIds = useMemo(
    () => new Set(serverEligibleItems.map((item) => item.reviewItemId)),
    [serverEligibleItems],
  );
  const selectedItems = serverEligibleItems.filter((item) =>
    selectedDeleteIds.has(item.reviewItemId),
  );
  const allSelected =
    serverEligibleItems.length > 0 &&
    selectedItems.length === serverEligibleItems.length;

  const toggleItem = (item: ReviewProjectSummaryItem) => {
    if (
      batchDeletePending ||
      itemActionPending ||
      !serverEligibleIds.has(item.reviewItemId)
    ) {
      return;
    }
    setSelectedDeleteIds((current) => {
      const next = new Set(current);
      if (next.has(item.reviewItemId)) next.delete(item.reviewItemId);
      else next.add(item.reviewItemId);
      return next;
    });
    setBatchDeleteFailures((current) => {
      if (!(item.reviewItemId in current)) return current;
      const next = { ...current };
      delete next[item.reviewItemId];
      return next;
    });
  };

  const bulkDeleteControls = onBulkDeleteReviewItems && visibleItems.some(
    (item) => item.bulkDelete.eligible || item.bulkDelete.locked,
  ) ? (
    <CapabilityGate entryMode={entryMode} capability="review.item.delete">
      <div
        aria-label="待审分集批量操作"
        className={`fj-review-bulk-delete-controls ${allSelected ? 'is-all-selected' : ''}`.trim()}
        role="group"
      >
        <button
          aria-pressed={allSelected}
          className="fj-review-bulk-select-all"
          disabled={batchDeletePending || itemActionPending || serverEligibleItems.length === 0}
          onClick={() => {
            setSelectedDeleteIds(
              allSelected
                ? new Set()
                : new Set(serverEligibleItems.map((item) => item.reviewItemId)),
            );
          }}
          type="button"
        >
          {allSelected ? '取消全选' : '全选'}
        </button>
        <span aria-live="polite">已选择 {selectedItems.length} 条</span>
        <button
          className="fj-review-secondary is-danger fj-review-bulk-delete-button"
          disabled={!selectedItems.length || batchDeletePending || itemActionPending}
          onClick={() => {
            const batch = [...selectedItems];
            if (!window.confirm(`确认批量删除已选择的 ${batch.length} 条待审分集？该操作逐条执行且无法撤销。`)) {
              return;
            }
            setBatchDeletePending(true);
            void onBulkDeleteReviewItems(batch)
              .then((result) => {
                setSelectedDeleteIds((current) => {
                  const next = new Set(current);
                  for (const id of result.succeededIds) next.delete(id);
                  for (const id of result.uncertainIds) next.delete(id);
                  for (const id of Object.keys(result.failures)) next.add(id);
                  return next;
                });
                setLocallyDeletedIds((current) => new Set([
                  ...current,
                  ...result.succeededIds,
                ]));
                setBatchDeleteFailures((current) => {
                  const next = { ...current };
                  for (const item of batch) delete next[item.reviewItemId];
                  return { ...next, ...result.failures };
                });
                setBatchDeleteUncertainIds((current) => new Set([
                  ...current,
                  ...result.uncertainIds,
                ]));
              })
              .catch((error: unknown) => {
                const message = error instanceof Error
                  ? error.message
                  : '批量删除结果不确定，请核对列表后再操作。';
                setBatchDeleteFailures((current) => ({
                  ...current,
                  ...Object.fromEntries(batch.map((item) => [item.reviewItemId, message])),
                }));
                setBatchDeleteUncertainIds((current) => new Set([
                  ...current,
                  ...batch.map((item) => item.reviewItemId),
                ]));
              })
              .finally(() => setBatchDeletePending(false));
          }}
          type="button"
        >
          {batchDeletePending ? '批量删除中...' : `批量删除（${selectedItems.length}）`}
        </button>
      </div>
    </CapabilityGate>
  ) : null;

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      setSelectedDeleteIds((current) => {
        const next = new Set([...current].filter((id) => serverEligibleIds.has(id)));
        return next.size === current.size ? current : next;
      });
      setBatchDeleteFailures((current) =>
        Object.fromEntries(
          Object.entries(current).filter(([id]) =>
            visibleItems.some((item) => item.reviewItemId === id),
          ),
        ),
      );
      setRevokeUiStates((current) =>
        Object.fromEntries(
          Object.entries(current).filter(([id]) => {
            const item = visibleItems.find((candidate) => candidate.reviewItemId === id);
            if (
              current[id] === 'uncertain' &&
              revocationUncertainIds &&
              !revocationUncertainIds.has(id)
            ) {
              return false;
            }
            return item?.status === 'finalized' && item.finalization?.status === 'active';
          }),
        ),
      );
    });
    return () => window.cancelAnimationFrame(frame);
  }, [revocationUncertainIds, serverEligibleIds, visibleItems]);

  if (visibleItems.length === 0) {
    return <EmptyState title="暂无成片" detail="剪辑入口可创建成片并上传 V1。" icon="upload" />;
  }

  return (
    <div className="fj-review-item-table" data-testid="review-item-table">
      {bulkDeleteControls
        ? bulkActionHost
          ? createPortal(bulkDeleteControls, bulkActionHost)
          : bulkDeleteControls
        : null}
      {visibleItems.map((item) => {
        const selected = selectedDeleteIds.has(item.reviewItemId);
        const selectable = serverEligibleIds.has(item.reviewItemId);
        const revokeState = revokeUiStates[item.reviewItemId];
        const revokePending = revokeState === 'pending';
        const revokeUncertain =
          revokeState === 'uncertain' ||
          Boolean(revocationUncertainIds?.has(item.reviewItemId));
        const cleanupPending = item.revocationCleanupStatus === 'pending';
        const cleanupFailed = item.revocationCleanupStatus === 'failed';
        const isFinalized = item.status === 'finalized' && item.finalization?.status === 'active';
        const classes = [
          'fj-review-item-row',
          selected ? 'is-selected-for-delete' : '',
          !selected && item.status === 'changes_requested' ? 'is-changes-requested' : '',
          !selected && isFinalized ? 'is-finalized' : '',
          selectable ? 'is-selectable' : '',
          item.bulkDelete.locked || revokePending || revokeUncertain || cleanupPending ? 'is-locked' : '',
        ].filter(Boolean).join(' ');
        return (
          <article
            aria-pressed={selectable ? selected : undefined}
            className={classes}
            data-testid={`review-item-row-${item.reviewItemId}`}
            key={item.reviewItemId}
            onClick={(event) => {
              if (!isControlTarget(event.target)) toggleItem(item);
            }}
            onKeyDown={(event) => {
              if (!selectable || isControlTarget(event.target)) return;
              if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                toggleItem(item);
              }
            }}
            role={selectable ? 'button' : undefined}
            tabIndex={selectable ? 0 : undefined}
          >
            <div className="fj-review-item-thumbnail-slot">
              {thumbnailState(item)}
            </div>
            <div className="fj-review-item-summary">
              <div className="fj-review-item-summary-text">
                <strong>第 {formatEpisodeDisplayValue(item.episode)} 集</strong>
                <span>
                  {item.title} · {item.currentVersion.versionLabel} · {Math.round(item.currentVersion.durationMs / 1000)} 秒 · {Math.round(item.currentVersion.fileSize / 1024 / 1024)} MiB
                </span>
              </div>
              <span
                aria-hidden="true"
                className="fj-review-item-version-watermark"
                data-testid={`item-row-version-watermark-${item.reviewItemId}`}
              >
                {item.currentVersion.versionLabel}
              </span>
            </div>
            {isFinalized && entryMode === 'review' && !isArchived && onRevokeFinalization ? (
              <CapabilityGate entryMode={entryMode} capability="review.finalization.revoke">
                <button
                  aria-label={`撤销第${formatEpisodeDisplayValue(item.episode)}集定稿`}
                  className="fj-review-finalized-revoke"
                  disabled={itemActionPending || revokePending || revokeUncertain}
                  onClick={() => {
                    if (!window.confirm(`确认撤销第 ${item.episode} 集定稿？关联项目包将立即失效并进入受控清理。`)) {
                      return;
                    }
                    setRevokeUiStates((current) => ({
                      ...current,
                      [item.reviewItemId]: 'pending',
                    }));
                    void onRevokeFinalization(item).catch((error: unknown) => {
                      setRevokeUiStates((current) => {
                        const next = { ...current };
                        if (error instanceof RevokeFinalizationResultUncertainError) {
                          next[item.reviewItemId] = 'uncertain';
                        } else {
                          delete next[item.reviewItemId];
                        }
                        return next;
                      });
                    });
                  }}
                  type="button"
                >
                  <span className="fj-review-finalized-label">已定稿</span>
                  <span className="fj-review-revoke-label">
                    {revokePending ? '撤销中' : revokeUncertain ? '确认中' : '撤销'}
                  </span>
                </button>
              </CapabilityGate>
            ) : (
              <StatusBadge status={item.status} />
            )}
            <span className="fj-review-item-open-count">
              <span
                aria-hidden="true"
                className="fj-review-item-count-watermark"
                data-testid={`item-row-count-watermark-${item.reviewItemId}`}
              >
                {item.unresolvedCurrentVersionCount}
              </span>
              <span className="fj-review-item-open-count-text">
                当前未修改 {item.unresolvedCurrentVersionCount}
              </span>
            </span>
            <div className="fj-review-item-actions">
              <Link
                className="fj-review-primary"
                to={`/${entryMode}/projects/${projectRefId}/items/${item.reviewItemId}`}
              >
                <IconText icon="upload">
                  {isArchived ? '查看' : entryMode === 'edit' ? '查看与追加' : '审阅'}
                </IconText>
              </Link>
              {!isArchived ? (
                <CapabilityGate entryMode={entryMode} capability="review.item.update">
                  <ReviewItemMetadataEditor
                    item={item}
                    pending={itemActionPending}
                    onSubmit={onUpdateReviewItemMetadata}
                  />
                </CapabilityGate>
              ) : null}
              {item.bulkDelete.eligible ? (
                <CapabilityGate entryMode={entryMode} capability="review.item.delete">
                  <button
                    aria-label={`删除分集 ${item.title}`}
                    className="fj-review-secondary is-danger"
                    disabled={itemActionPending || item.bulkDelete.locked || batchDeleteUncertainIds.has(item.reviewItemId)}
                    onClick={() => onDeleteReviewItem(item)}
                    type="button"
                  >
                    删除
                  </button>
                </CapabilityGate>
              ) : null}
              {batchDeleteFailures[item.reviewItemId] ? (
                <span
                  className="fj-review-form-error"
                  data-testid={`batch-delete-error-${item.reviewItemId}`}
                  role="alert"
                >
                  {batchDeleteFailures[item.reviewItemId]}
                </span>
              ) : null}
              {batchDeleteUncertainIds.has(item.reviewItemId) ? (
                <span
                  className="fj-review-form-error"
                  data-testid={`batch-delete-uncertain-${item.reviewItemId}`}
                  role="alert"
                >
                  删除结果不确定，已锁定；请刷新页面核对后再操作。
                </span>
              ) : null}
              {revokeUncertain ? (
                <span className="fj-review-revoke-state" role="status">
                  撤回结果确认中
                </span>
              ) : cleanupPending ? (
                <span className="fj-review-revoke-state" role="status">
                  定稿已撤回，关联包清理中
                </span>
              ) : cleanupFailed ? (
                <span className="fj-review-form-error" role="alert">
                  定稿已撤回，关联包清理失败，等待受控重试
                </span>
              ) : null}
            </div>
          </article>
        );
      })}
    </div>
  );
}
