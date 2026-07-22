import type { ReviewItem, ReviewVersion } from '../contracts/types';

function episodeKey(item: ReviewItem): string {
  return item.episode.trim().replace(/^第\s*/, '').replace(/\s*集$/, '').replace(/\s+/g, ' ') || item.episode;
}

function versionCount(item: ReviewItem, versionsByItem?: Record<string, ReviewVersion[]>): number {
  return versionsByItem?.[item.reviewItemId]?.length ?? 0;
}

function updatedAtMs(item: ReviewItem): number {
  const parsed = Date.parse(item.updatedAt);
  return Number.isFinite(parsed) ? parsed : 0;
}

function shouldReplaceEpisodeItem(
  current: ReviewItem,
  candidate: ReviewItem,
  options: {
    currentItemId?: string;
    versionsByItem?: Record<string, ReviewVersion[]>;
  },
): boolean {
  if (candidate.reviewItemId === options.currentItemId) return true;
  if (current.reviewItemId === options.currentItemId) return false;

  const candidateVersionCount = versionCount(candidate, options.versionsByItem);
  const currentVersionCount = versionCount(current, options.versionsByItem);
  if (candidateVersionCount !== currentVersionCount) return candidateVersionCount > currentVersionCount;

  const candidateUpdatedAt = updatedAtMs(candidate);
  const currentUpdatedAt = updatedAtMs(current);
  if (candidateUpdatedAt !== currentUpdatedAt) return candidateUpdatedAt > currentUpdatedAt;

  return candidate.reviewItemId.localeCompare(current.reviewItemId) < 0;
}

export interface ReviewEpisodeGroup {
  episodeKey: string;
  representative: ReviewItem;
  items: ReviewItem[];
}

export function groupReviewItemsByEpisode(
  items: ReviewItem[],
  options: {
    currentItemId?: string;
    versionsByItem?: Record<string, ReviewVersion[]>;
  } = {},
): ReviewEpisodeGroup[] {
  const byEpisode = new Map<string, ReviewItem[]>();
  for (const item of items) {
    const key = episodeKey(item);
    byEpisode.set(key, [...(byEpisode.get(key) ?? []), item]);
  }

  return [...byEpisode.entries()]
    .map(([key, groupedItems]) => {
      const representative = groupedItems.reduce((current, candidate) =>
        shouldReplaceEpisodeItem(current, candidate, options) ? candidate : current,
      );
      return {
        episodeKey: key,
        representative,
        items: [...groupedItems].sort((left, right) => left.reviewItemId.localeCompare(right.reviewItemId)),
      };
    })
    .sort((left, right) => left.representative.episode.localeCompare(right.representative.episode, 'zh-CN', { numeric: true }));
}

export function dedupeReviewItemsByEpisode(
  items: ReviewItem[],
  options: {
    currentItemId?: string;
    versionsByItem?: Record<string, ReviewVersion[]>;
  } = {},
): ReviewItem[] {
  return groupReviewItemsByEpisode(items, options).map((group) => group.representative);
}
