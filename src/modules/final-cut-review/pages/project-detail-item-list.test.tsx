import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import type { FinalizationRevocation, ReviewItem } from '../contracts/types';
import { createSeedData } from '../core/seed';
import {
  ReviewRuntimeProvider,
  createReviewRuntime,
  type ReviewRuntime,
} from '../entry/runtime';
import type { ReviewProjectSummaryItem } from '../ports';
import { RevokeFinalizationResultUncertainError } from '../adapters/http-review-finalization-operation';
import { ProjectDetailItemList } from './project-detail-item-list';

const activeRuntimes: ReviewRuntime[] = [];

function summaryItem(
  id: string,
  overrides: Partial<ReviewProjectSummaryItem> = {},
): ReviewProjectSummaryItem {
  const seed = createSeedData();
  const item = seed.items[0];
  const version = seed.versions.find(
    (candidate) => candidate.versionId === item.currentVersionId,
  ) ?? seed.versions[0];
  return {
    ...item,
    itemCode: `ITEM-${id}`,
    reviewItemId: id,
    currentVersionId: `${id}-current`,
    activeFinalizationId: null,
    status: 'pending_review',
    lockVersion: 1,
    currentVersion: {
      id: `${id}-current`,
      versionNo: version.versionNo,
      versionLabel: version.label,
      durationMs: version.durationMs,
      fileSize: version.size,
      playbackStatus: 'ready',
      playbackUrl: `/stream/${id}`,
      thumbnailStatus: 'ready',
      thumbnailUrl: `/thumbnail/${id}`,
    },
    unresolvedCurrentVersionCount: 0,
    finalization: null,
    revocationCleanupStatus: 'none',
    bulkDelete: {
      eligible: true,
      locked: false,
      reason: null,
    },
    ...overrides,
  };
}

function renderList(input: {
  items: ReviewProjectSummaryItem[];
  entryMode?: 'edit' | 'review';
  onBulkDeleteReviewItems?: (
    items: ReviewItem[],
  ) => Promise<{
    succeededIds: string[];
    failures: Record<string, string>;
    uncertainIds: string[];
  }>;
  onRevokeFinalization?: (
    item: ReviewProjectSummaryItem,
  ) => Promise<FinalizationRevocation>;
}) {
  const runtime = createReviewRuntime();
  activeRuntimes.push(runtime);
  const onDeleteReviewItem = vi.fn();
  const groups = input.items.map((item) => ({
    episodeKey: item.episode,
    representative: item,
    items: [item],
  }));
  render(
    <ReviewRuntimeProvider runtime={runtime}>
      <MemoryRouter>
        <ProjectDetailItemList
          entryMode={input.entryMode ?? 'edit'}
          episodeGroups={groups}
          isArchived={false}
          itemActionPending={false}
          onBulkDeleteReviewItems={input.onBulkDeleteReviewItems}
          onDeleteReviewItem={onDeleteReviewItem}
          onRevokeFinalization={input.onRevokeFinalization}
          onUpdateReviewItemMetadata={vi.fn(async () => undefined)}
          projectRefId="prj_seed_final_cut"
        />
      </MemoryRouter>
    </ReviewRuntimeProvider>,
  );
  return { onDeleteReviewItem };
}

afterEach(() => {
  for (const runtime of activeRuntimes.splice(0)) runtime.dispose();
  vi.restoreAllMocks();
});

describe('ProjectDetailItemList summary rendering', () => {
  it('renders the currentVersionId real thumbnail in the former selector slot and no row checkbox', () => {
    const item = summaryItem('item-current', {
      episode: '7',
      currentVersion: {
        ...summaryItem('base').currentVersion,
        id: 'item-current-v4',
        versionNo: 4,
        versionLabel: 'V4',
        thumbnailStatus: 'ready',
        thumbnailUrl: '/api/v1/final-cut-review/projects/p/items/i/versions/v/thumbnail',
      },
      unresolvedCurrentVersionCount: 3,
    });
    renderList({ items: [item], onBulkDeleteReviewItems: vi.fn() });

    const thumbnail = screen.getByTestId('item-row-thumbnail-item-current');
    expect(thumbnail).toHaveAttribute(
      'src',
      '/api/v1/final-cut-review/projects/p/items/i/versions/v/thumbnail',
    );
    expect(thumbnail).toHaveAttribute('alt', '第07集 V4 首帧');
    expect(screen.getByTestId('item-row-version-watermark-item-current')).toHaveTextContent('V4');
    expect(screen.getByTestId('item-row-count-watermark-item-current')).toHaveTextContent('3');
    expect(screen.queryByRole('checkbox', { name: /选择第/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
  });

  it('shows explicit pending and failed thumbnail states without inventing a media URL', () => {
    const pending = summaryItem('pending', {
      episode: '8',
      currentVersion: {
        ...summaryItem('base').currentVersion,
        thumbnailStatus: 'pending',
        thumbnailUrl: null,
      },
    });
    const failed = summaryItem('failed', {
      episode: '9',
      currentVersion: {
        ...summaryItem('base').currentVersion,
        thumbnailStatus: 'failed',
        thumbnailUrl: null,
      },
    });
    renderList({ items: [pending, failed] });

    expect(screen.getByText('首帧生成中')).toBeInTheDocument();
    expect(screen.getByText('首帧生成失败')).toBeInTheDocument();
    expect(screen.queryAllByRole('img')).toHaveLength(0);
  });
});

describe('ProjectDetailItemList server-authoritative selection', () => {
  it('uses non-control row clicks, removes row checkboxes, and never selects locked/ineligible items', async () => {
    const eligible = summaryItem('eligible', { episode: '10' });
    const locked = summaryItem('locked', {
      episode: '11',
      bulkDelete: {
        eligible: true,
        locked: true,
        reason: 'DELETE_PENDING',
      },
    });
    const ineligible = summaryItem('ineligible', {
      episode: '12',
      bulkDelete: {
        eligible: false,
        locked: false,
        reason: 'REVIEW_STARTED',
      },
    });
    const { onDeleteReviewItem } = renderList({
      items: [eligible, locked, ineligible],
      onBulkDeleteReviewItems: vi.fn(async () => ({
        succeededIds: [],
        failures: {},
        uncertainIds: [],
      })),
    });

    const eligibleRow = screen.getByTestId('review-item-row-eligible');
    const lockedRow = screen.getByTestId('review-item-row-locked');
    const ineligibleRow = screen.getByTestId('review-item-row-ineligible');
    await userEvent.click(eligibleRow);
    expect(eligibleRow).toHaveClass('is-selected-for-delete');

    await userEvent.click(within(eligibleRow).getByRole('button', { name: '删除分集 第 28 集 · 最终成片' }));
    expect(onDeleteReviewItem).toHaveBeenCalledWith(eligible);
    expect(eligibleRow).toHaveClass('is-selected-for-delete');

    await userEvent.click(lockedRow);
    await userEvent.click(ineligibleRow);
    expect(lockedRow).not.toHaveClass('is-selected-for-delete');
    expect(ineligibleRow).not.toHaveClass('is-selected-for-delete');
    expect(screen.queryByRole('checkbox', { name: /选择第/ })).not.toBeInTheDocument();
  });

  it('select-all selects only server eligible unlocked rows and keeps red selection highest priority', async () => {
    const eligible = summaryItem('eligible', {
      status: 'changes_requested',
      unresolvedCurrentVersionCount: 1,
    });
    const locked = summaryItem('locked', {
      status: 'finalized',
      activeFinalizationId: 'fin-locked',
      finalization: { id: 'fin-locked', status: 'active', revokedAt: null },
      bulkDelete: { eligible: true, locked: true, reason: 'LOCKED' },
    });
    const onBulkDeleteReviewItems = vi.fn(async () => ({
      succeededIds: [],
      failures: {},
      uncertainIds: [],
    }));
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderList({ items: [eligible, locked], onBulkDeleteReviewItems });

    const selectAll = screen.getByRole('button', { name: '全选' });
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
    await userEvent.click(selectAll);
    expect(screen.getByRole('button', { name: '取消全选' })).toHaveAttribute('aria-pressed', 'true');
    const eligibleRow = screen.getByTestId('review-item-row-eligible');
    expect(eligibleRow).toHaveClass('is-selected-for-delete');
    expect(eligibleRow).not.toHaveClass('is-changes-requested');
    expect(screen.getByTestId('review-item-row-locked')).not.toHaveClass('is-selected-for-delete');

    await userEvent.click(screen.getByRole('button', { name: '批量删除（1）' }));
    expect(onBulkDeleteReviewItems).toHaveBeenCalledWith([eligible]);
  });
});

describe('ProjectDetailItemList status and finalization revocation', () => {
  it('applies yellow changes-requested and green finalized rows when they are not selected', () => {
    const changes = summaryItem('changes', {
      status: 'changes_requested',
      unresolvedCurrentVersionCount: 2,
      bulkDelete: { eligible: false, locked: false, reason: 'REVIEW_STARTED' },
    });
    const finalized = summaryItem('finalized', {
      status: 'finalized',
      activeFinalizationId: 'fin-finalized',
      finalization: { id: 'fin-finalized', status: 'active', revokedAt: null },
      bulkDelete: { eligible: false, locked: false, reason: 'FINALIZED' },
    });
    renderList({ items: [changes, finalized] });

    expect(screen.getByTestId('review-item-row-changes')).toHaveClass('is-changes-requested');
    expect(screen.getByTestId('review-item-row-finalized')).toHaveClass('is-finalized');
  });

  it('requires confirmation, locks the finalized card while revoking, and reports uncertain results', async () => {
    const finalized = summaryItem('finalized', {
      episode: '13',
      status: 'finalized',
      activeFinalizationId: 'fin-finalized',
      finalization: { id: 'fin-finalized', status: 'active', revokedAt: null },
      bulkDelete: { eligible: false, locked: false, reason: 'FINALIZED' },
    });
    const onRevokeFinalization = vi.fn(async () => {
      throw new RevokeFinalizationResultUncertainError(
        new TypeError('network response lost'),
      );
    });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderList({
      items: [finalized],
      entryMode: 'review',
      onRevokeFinalization,
    });

    const revoke = screen.getByRole('button', { name: '撤销第13集定稿' });
    await userEvent.click(revoke);
    expect(window.confirm).toHaveBeenCalledWith(
      '确认撤销第 13 集定稿？关联项目包将立即失效并进入受控清理。',
    );
    expect(onRevokeFinalization).toHaveBeenCalledWith(finalized);
    expect(await screen.findByText('撤回结果确认中')).toBeInTheDocument();
    expect(revoke).toBeDisabled();
  });

  it('shows authoritative cleanup pending and failed states without restoring finalized status', () => {
    const pending = summaryItem('cleanup-pending', {
      status: 'in_review',
      finalization: { id: 'fin-pending', status: 'revoked', revokedAt: '2026-07-31T00:00:00Z' },
      revocationCleanupStatus: 'pending',
      bulkDelete: { eligible: false, locked: true, reason: 'CLEANUP_PENDING' },
    });
    const failed = summaryItem('cleanup-failed', {
      status: 'in_review',
      finalization: { id: 'fin-failed', status: 'revoked', revokedAt: '2026-07-31T00:00:00Z' },
      revocationCleanupStatus: 'failed',
      bulkDelete: { eligible: false, locked: true, reason: 'CLEANUP_FAILED' },
    });
    renderList({ items: [pending, failed], entryMode: 'review' });

    expect(screen.getByText('定稿已撤回，关联包清理中')).toBeInTheDocument();
    expect(screen.getByText('定稿已撤回，关联包清理失败，等待受控重试')).toBeInTheDocument();
    expect(screen.getAllByText('审阅中')).toHaveLength(2);
  });
});
