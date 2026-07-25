import { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Link } from 'react-router-dom';
import { CapabilityGate, EmptyState, IconText, StatusBadge } from '../components/shared';
import { ReviewItemMetadataEditor, type ReviewItemMetadataValues } from '../components/MetadataEditors';
import type {
  EntryMode,
  FinalizationRecord,
  ReviewIssue,
  ReviewItem,
  ReviewVersion,
} from '../contracts/types';
import { formatEpisodeDisplayValue, type ReviewEpisodeGroup } from '../core/episode-dedupe';
import type { ReviewItemWithMetadata } from '../ports';

export type ProjectDetailMetadataEpisodeGroup = Omit<ReviewEpisodeGroup, 'items' | 'representative'> & {
  items: ReviewItemWithMetadata[];
  representative: ReviewItemWithMetadata;
};

interface ProjectDetailItemListProps {
  entryMode: EntryMode;
  episodeGroups: ProjectDetailMetadataEpisodeGroup[];
  finalizations: FinalizationRecord[];
  isArchived: boolean;
  issuesByVersion: Record<string, ReviewIssue[]>;
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
  onUpdateReviewItemMetadata: (item: ReviewItemWithMetadata, values: ReviewItemMetadataValues) => Promise<void>;
  projectRefId: string;
  versionsByItem: Record<string, ReviewVersion[]>;
}

export function ProjectDetailItemList({
  entryMode,
  episodeGroups,
  finalizations,
  isArchived,
  issuesByVersion,
  itemActionPending,
  bulkActionHost,
  onBulkDeleteReviewItems,
  onDeleteReviewItem,
  onUpdateReviewItemMetadata,
  projectRefId,
  versionsByItem,
}: ProjectDetailItemListProps) {
  const [selectedDeleteIds, setSelectedDeleteIds] = useState<Set<string>>(() => new Set());
  const [batchDeleteFailures, setBatchDeleteFailures] = useState<Record<string, string>>({});
  const [batchDeleteUncertainIds, setBatchDeleteUncertainIds] = useState<Set<string>>(() => new Set());
  const [locallyDeletedIds, setLocallyDeletedIds] = useState<Set<string>>(() => new Set());
  const [batchDeletePending, setBatchDeletePending] = useState(false);
  const selectAllRef = useRef<HTMLInputElement>(null);
  const visibleEpisodeGroups = useMemo(
    () =>
      episodeGroups
        .map((group) => {
          const items = group.items.filter((item) => !locallyDeletedIds.has(item.reviewItemId));
          return {
            ...group,
            items,
            representative:
              items.find(
                (item) => item.reviewItemId === group.representative.reviewItemId,
              ) ??
              items[0] ??
              group.representative,
          };
        })
        .filter((group) => group.items.length > 0),
    [episodeGroups, locallyDeletedIds],
  );
  const deletableItems = useMemo(
    () =>
      visibleEpisodeGroups.flatMap((group) =>
        group.items.filter((candidate) => {
          const candidateVersions = versionsByItem[candidate.reviewItemId] ?? [];
          const candidateIssues = candidateVersions.flatMap(
            (version) => issuesByVersion[version.versionId] ?? [],
          );
          const candidateHasFinalization = finalizations.some(
            (finalization) => finalization.reviewItemId === candidate.reviewItemId,
          );
          return (
            !isArchived &&
            candidate.status === 'pending_review' &&
            !candidate.activeFinalizationId &&
            candidateVersions.length === 1 &&
            candidateIssues.length === 0 &&
            !candidateHasFinalization
          );
        }),
      ),
    [finalizations, isArchived, issuesByVersion, versionsByItem, visibleEpisodeGroups],
  );
  const retryableDeletableItems = deletableItems.filter(
    (item) => !batchDeleteUncertainIds.has(item.reviewItemId),
  );
  const deletableIds = useMemo(
    () => new Set(deletableItems.map((item) => item.reviewItemId)),
    [deletableItems],
  );
  const selectedItems = retryableDeletableItems.filter((item) => selectedDeleteIds.has(item.reviewItemId));
  const allSelected =
    retryableDeletableItems.length > 0 &&
    selectedItems.length === retryableDeletableItems.length;
  const partiallySelected = selectedItems.length > 0 && !allSelected;
  const bulkDeleteControls = onBulkDeleteReviewItems && deletableItems.length ? (
    <CapabilityGate entryMode={entryMode} capability="review.item.delete">
      <div
        aria-label="待审分集批量操作"
        className="fj-review-bulk-delete-controls"
        role="group"
      >
        <label className="fj-review-bulk-select-all">
          <input
            ref={selectAllRef}
            aria-label="全选可删除分集"
            checked={allSelected}
            disabled={batchDeletePending || itemActionPending}
            onChange={(event) => {
              const checked = event.currentTarget.checked;
              setSelectedDeleteIds(
                checked
                  ? new Set(retryableDeletableItems.map((item) => item.reviewItemId))
                  : new Set(),
              );
            }}
            type="checkbox"
          />
          <span>全选</span>
        </label>
        <span className="fj-review-sr-only" aria-live="polite">
          已选择 {selectedItems.length} 条
        </span>
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
                setBatchDeleteUncertainIds((current) => {
                  const next = new Set(current);
                  for (const id of result.succeededIds) next.delete(id);
                  for (const id of result.uncertainIds) next.add(id);
                  return next;
                });
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
    if (selectAllRef.current) selectAllRef.current.indeterminate = partiallySelected;
  }, [partiallySelected]);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      setSelectedDeleteIds((current) => {
        const next = new Set([...current].filter((id) => deletableIds.has(id)));
        return next.size === current.size ? current : next;
      });
      setBatchDeleteFailures((current) =>
        Object.fromEntries(Object.entries(current).filter(([id]) => deletableIds.has(id))),
      );
      setBatchDeleteUncertainIds((current) =>
        new Set([...current].filter((id) => deletableIds.has(id))),
      );
    });
    return () => window.cancelAnimationFrame(frame);
  }, [deletableIds]);

  if (visibleEpisodeGroups.length === 0) {
    return <EmptyState title="暂无成片" detail="剪辑入口可创建成片并上传 V1。" icon="upload" />;
  }

  return (
    <div className="fj-review-item-table" data-testid="review-item-table">
      {bulkDeleteControls
        ? bulkActionHost
          ? createPortal(bulkDeleteControls, bulkActionHost)
          : bulkDeleteControls
        : null}
      {visibleEpisodeGroups.map((group) => {
        const item = group.representative;
        const versions = versionsByItem[item.reviewItemId] ?? [];
        const currentVersion = versions.find((version) => version.versionId === item.currentVersionId);
        const currentOriginalFilename = currentVersion?.originalMedia.originalFilename || currentVersion?.fileName || '-';
        const currentIssues = currentVersion ? issuesByVersion[currentVersion.versionId] ?? [] : [];
        const openCount = currentIssues.filter((issue) => issue.status === 'unresolved').length;
        const groupDeletableItems = group.items.filter((candidate) =>
          deletableIds.has(candidate.reviewItemId),
        );
        const hasDuplicateDeleteTargets = groupDeletableItems.length > 1;
        const isFinalized = item.status === 'finalized';
        return (
          <article
            key={group.episodeKey}
            className={`fj-review-item-row ${isFinalized ? 'is-finalized' : ''}`.trim()}
          >
            <div className={`fj-review-item-select-column ${hasDuplicateDeleteTargets ? 'has-duplicates' : ''}`}>
              {groupDeletableItems.map((candidate) => (
                <CapabilityGate
                  entryMode={entryMode}
                  capability="review.item.delete"
                  key={candidate.reviewItemId}
                >
                  {onBulkDeleteReviewItems ? (
                    <label className="fj-review-item-delete-select">
                      <input
                        aria-describedby={
                          hasDuplicateDeleteTargets
                            ? `delete-target-${candidate.reviewItemId}`
                            : undefined
                        }
                        aria-label={`选择第${formatEpisodeDisplayValue(candidate.episode)}集`}
                        checked={selectedDeleteIds.has(candidate.reviewItemId)}
                        disabled={
                          batchDeletePending ||
                          itemActionPending ||
                          batchDeleteUncertainIds.has(candidate.reviewItemId)
                        }
                        onChange={(event) => {
                          const checked = event.currentTarget.checked;
                          setSelectedDeleteIds((current) => {
                            const next = new Set(current);
                            if (checked) next.add(candidate.reviewItemId);
                            else next.delete(candidate.reviewItemId);
                            return next;
                          });
                          setBatchDeleteFailures((current) => {
                            const next = { ...current };
                            delete next[candidate.reviewItemId];
                            return next;
                          });
                        }}
                        type="checkbox"
                      />
                      {hasDuplicateDeleteTargets ? (
                        <span
                          className="fj-review-delete-target-identity"
                          id={`delete-target-${candidate.reviewItemId}`}
                        >
                          <strong>{candidate.title}</strong>
                          <small>
                            {(versionsByItem[candidate.reviewItemId] ?? [])
                              .find((version) => version.versionId === candidate.currentVersionId)
                              ?.originalMedia.originalFilename ??
                              candidate.itemCode}
                          </small>
                        </span>
                      ) : null}
                    </label>
                  ) : null}
                </CapabilityGate>
              ))}
            </div>
            <div className="fj-review-item-summary">
              <strong>第 {item.episode} 集</strong>
              <span>原文件：{currentOriginalFilename} · {versions.length}个版本 · 当前 {currentVersion?.label ?? '-'}</span>
            </div>
            <StatusBadge status={item.status} />
            <span>当前未修改 {openCount}</span>
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
                  {group.items.map((candidate) => (
                    <ReviewItemMetadataEditor
                      item={candidate}
                      key={candidate.reviewItemId}
                      pending={itemActionPending}
                      onSubmit={onUpdateReviewItemMetadata}
                    />
                  ))}
                </CapabilityGate>
              ) : null}
              {groupDeletableItems.map((candidate) => (
                <span key={candidate.reviewItemId} className="fj-review-duplicate-item-action">
                  <CapabilityGate entryMode={entryMode} capability="review.item.delete">
                    <button
                      aria-label={`删除分集 ${candidate.title}`}
                      className="fj-review-secondary is-danger"
                      disabled={
                        itemActionPending ||
                        batchDeleteUncertainIds.has(candidate.reviewItemId)
                      }
                      onClick={() => onDeleteReviewItem(candidate)}
                      type="button"
                    >
                      {candidate.reviewItemId === item.reviewItemId && group.items.length === 1
                        ? '删除'
                        : '删除重复项'}
                    </button>
                    {batchDeleteFailures[candidate.reviewItemId] ? (
                      <span
                        className="fj-review-form-error"
                        data-testid={`batch-delete-error-${candidate.reviewItemId}`}
                        role="alert"
                      >
                        {batchDeleteFailures[candidate.reviewItemId]}
                      </span>
                    ) : null}
                    {batchDeleteUncertainIds.has(candidate.reviewItemId) ? (
                      <span
                        className="fj-review-form-error"
                        data-testid={`batch-delete-uncertain-${candidate.reviewItemId}`}
                        role="alert"
                      >
                        删除结果不确定，已锁定；请刷新页面核对后再操作。
                      </span>
                    ) : null}
                  </CapabilityGate>
                </span>
              ))}
            </div>
          </article>
        );
      })}
    </div>
  );
}
