import { describe, expect, it } from 'vitest';
import type { ReviewItem, ReviewVersion } from '../contracts/types';
import { dedupeReviewItemsByEpisode, groupReviewItemsByEpisode } from './episode-dedupe';

function item(input: Partial<ReviewItem> & Pick<ReviewItem, 'reviewItemId' | 'episode'>): ReviewItem {
  return {
    projectRefId: 'prj_seed_final_cut',
    title: `第 ${input.episode} 集 · 最终成片`,
    currentVersionId: `ver_${input.reviewItemId}`,
    activeFinalizationId: null,
    status: 'in_review',
    createdAt: '2026-06-18T10:00:00.000Z',
    updatedAt: '2026-06-18T10:00:00.000Z',
    ...input,
  };
}

function versions(count: number): ReviewVersion[] {
  return Array.from({ length: count }, (_, index) => ({ versionId: `ver_${index}` }) as ReviewVersion);
}

describe('dedupeReviewItemsByEpisode', () => {
  it('keeps one representative per episode and prefers the item with more versions', () => {
    const result = dedupeReviewItemsByEpisode(
      [
        item({ reviewItemId: 'item_ep28_v1', episode: '28' }),
        item({ reviewItemId: 'item_ep28_v2', episode: '28', updatedAt: '2026-06-18T10:10:00.000Z' }),
        item({ reviewItemId: 'item_ep29', episode: '29' }),
      ],
      {
        versionsByItem: {
          item_ep28_v1: versions(2),
          item_ep28_v2: versions(3),
          item_ep29: versions(1),
        },
      },
    );

    expect(result.map((entry) => entry.reviewItemId)).toEqual(['item_ep28_v2', 'item_ep29']);
  });

  it('keeps the current item when the current workspace belongs to a duplicated episode', () => {
    const result = dedupeReviewItemsByEpisode(
      [
        item({ reviewItemId: 'item_ep28_v1', episode: '第 28 集' }),
        item({ reviewItemId: 'item_ep28_v2', episode: '28' }),
      ],
      {
        currentItemId: 'item_ep28_v1',
        versionsByItem: {
          item_ep28_v1: versions(1),
          item_ep28_v2: versions(3),
        },
      },
    );

    expect(result).toHaveLength(1);
    expect(result[0]?.reviewItemId).toBe('item_ep28_v1');
  });

  it('keeps every underlying item available when one episode is grouped', () => {
    const result = groupReviewItemsByEpisode(
      [
        item({ reviewItemId: 'item_ep28_reviewed', episode: '28', status: 'in_review' }),
        item({ reviewItemId: 'item_ep28_duplicate', episode: '第 28 集', status: 'pending_review' }),
      ],
      {
        versionsByItem: {
          item_ep28_reviewed: versions(2),
          item_ep28_duplicate: versions(1),
        },
      },
    );

    expect(result).toHaveLength(1);
    expect(result[0]?.representative.reviewItemId).toBe('item_ep28_reviewed');
    expect(result[0]?.items.map((entry) => entry.reviewItemId)).toEqual([
      'item_ep28_duplicate',
      'item_ep28_reviewed',
    ]);
  });
});
