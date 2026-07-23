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
import type { ReviewEpisodeGroup } from '../core/episode-dedupe';
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
  onDeleteReviewItem,
  onUpdateReviewItemMetadata,
  projectRefId,
  versionsByItem,
}: ProjectDetailItemListProps) {
  if (episodeGroups.length === 0) {
    return <EmptyState title="暂无成片" detail="剪辑入口可创建成片并上传 V1。" icon="upload" />;
  }

  return (
    <div className="fj-review-item-table" data-testid="review-item-table">
      {episodeGroups.map((group) => {
        const item = group.representative;
        const versions = versionsByItem[item.reviewItemId] ?? [];
        const currentVersion = versions.find((version) => version.versionId === item.currentVersionId);
        const currentOriginalFilename = currentVersion?.originalMedia.originalFilename || currentVersion?.fileName || '-';
        const currentIssues = currentVersion ? issuesByVersion[currentVersion.versionId] ?? [] : [];
        const openCount = currentIssues.filter((issue) => issue.status === 'unresolved').length;
        const deletableItems = group.items.filter((candidate) => {
          const candidateVersions = versionsByItem[candidate.reviewItemId] ?? [];
          const candidateIssues = candidateVersions.flatMap(
            (version) => issuesByVersion[version.versionId] ?? [],
          );
          const candidateHasFinalization = finalizations.some(
            (finalization) => finalization.reviewItemId === candidate.reviewItemId,
          );
          return (
            !isArchived &&
            entryMode === 'edit' &&
            candidate.status === 'pending_review' &&
            !candidate.activeFinalizationId &&
            candidateVersions.length === 1 &&
            candidateIssues.length === 0 &&
            !candidateHasFinalization
          );
        });
        return (
          <article key={group.episodeKey} className="fj-review-item-row">
            <div>
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
              {deletableItems.map((candidate) => (
                <span key={candidate.reviewItemId} className="fj-review-duplicate-item-action">
                  <CapabilityGate entryMode={entryMode} capability="review.item.delete">
                    <button
                      aria-label={`删除分集 ${candidate.title}`}
                      className="fj-review-secondary is-danger"
                      disabled={itemActionPending}
                      onClick={() => onDeleteReviewItem(candidate)}
                      type="button"
                    >
                      {candidate.reviewItemId === item.reviewItemId && group.items.length === 1
                        ? '删除'
                        : '删除重复项'}
                    </button>
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
