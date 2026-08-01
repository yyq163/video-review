import { act, fireEvent, render, screen, within } from '@testing-library/react';
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
  itemActionPending?: boolean;
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
  const renderView = (
    items: ReviewProjectSummaryItem[],
    itemActionPending = input.itemActionPending ?? false,
  ) => {
    const groups = items.map((item) => ({
      episodeKey: item.episode,
      representative: item,
      items: [item],
    }));
    return (
      <ReviewRuntimeProvider runtime={runtime}>
        <MemoryRouter>
          <ProjectDetailItemList
            entryMode={input.entryMode ?? 'edit'}
            episodeGroups={groups}
            isArchived={false}
            itemActionPending={itemActionPending}
            onBulkDeleteReviewItems={input.onBulkDeleteReviewItems}
            onDeleteReviewItem={onDeleteReviewItem}
            onRevokeFinalization={input.onRevokeFinalization}
            onUpdateReviewItemMetadata={vi.fn(async () => undefined)}
            projectRefId="prj_seed_final_cut"
          />
        </MemoryRouter>
      </ReviewRuntimeProvider>
    );
  };
  const result = render(renderView(input.items));
  return {
    onDeleteReviewItem,
    rerenderList: (
      items = input.items,
      itemActionPending = input.itemActionPending ?? false,
    ) => result.rerender(renderView(items, itemActionPending)),
  };
}

afterEach(() => {
  vi.useRealTimers();
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
  it('requires a long press on non-control content and never selects locked/ineligible items', async () => {
    const eligible = summaryItem('eligible', { episode: '10' });
    const locked = summaryItem('locked', {
      episode: '11',
      bulkDelete: {
        eligible: false,
        locked: true,
        reason: 'version_history',
      },
    });
    const ineligible = summaryItem('ineligible', {
      episode: '12',
      status: 'in_review',
      bulkDelete: {
        eligible: false,
        locked: true,
        reason: 'workflow_locked',
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
    const lockedDelete = within(lockedRow).getByRole('button', { name: /删除分集/ });
    const ineligibleDelete = within(ineligibleRow).getByRole('button', { name: /删除分集/ });
    expect(lockedDelete).toBeDisabled();
    expect(lockedDelete.parentElement).toHaveAttribute(
      'title',
      '存在历史版本，不可删除',
    );
    expect(ineligibleDelete).toBeDisabled();
    expect(ineligibleDelete).toHaveClass('is-delete-disabled');
    expect(ineligibleDelete.parentElement).toHaveAttribute('title', '审阅中，不可删除');
    expect(ineligibleDelete.parentElement).toHaveAttribute('data-tooltip', '审阅中，不可删除');
    expect(ineligibleDelete.parentElement).toHaveAttribute('tabindex', '0');
    await userEvent.click(eligibleRow);
    expect(eligibleRow).not.toHaveClass('is-selected-for-delete');

    vi.useFakeTimers();
    fireEvent.pointerDown(eligibleRow, {
      button: 0,
      clientX: 20,
      clientY: 20,
      isPrimary: true,
      pointerId: 1,
      pointerType: 'mouse',
    });
    act(() => vi.advanceTimersByTime(499));
    expect(eligibleRow).not.toHaveClass('is-selected-for-delete');
    act(() => vi.advanceTimersByTime(1));
    expect(eligibleRow).toHaveClass('is-selected-for-delete');
    fireEvent.pointerUp(eligibleRow, { pointerId: 1, pointerType: 'mouse' });
    vi.useRealTimers();

    await userEvent.click(within(eligibleRow).getByRole('button', { name: '删除分集 第 28 集 · 最终成片' }));
    expect(onDeleteReviewItem).toHaveBeenCalledWith(eligible);
    expect(eligibleRow).toHaveClass('is-selected-for-delete');

    await userEvent.click(lockedRow);
    await userEvent.click(ineligibleRow);
    expect(lockedRow).not.toHaveClass('is-selected-for-delete');
    expect(ineligibleRow).not.toHaveClass('is-selected-for-delete');
    expect(screen.queryByRole('checkbox', { name: /选择第/ })).not.toBeInTheDocument();
  });

  it('cancels selection when a press is released or moves before the threshold', () => {
    vi.useFakeTimers();
    const eligible = summaryItem('eligible');
    renderList({
      items: [eligible],
      onBulkDeleteReviewItems: vi.fn(async () => ({
        succeededIds: [],
        failures: {},
        uncertainIds: [],
      })),
    });
    const row = screen.getByTestId('review-item-row-eligible');

    fireEvent.pointerDown(row, {
      button: 0,
      clientX: 10,
      clientY: 10,
      isPrimary: true,
      pointerId: 1,
      pointerType: 'touch',
    });
    fireEvent.pointerUp(row, { pointerId: 1, pointerType: 'touch' });
    act(() => vi.advanceTimersByTime(500));
    expect(row).not.toHaveClass('is-selected-for-delete');

    fireEvent.pointerDown(row, {
      button: 0,
      clientX: 10,
      clientY: 10,
      isPrimary: true,
      pointerId: 2,
      pointerType: 'touch',
    });
    fireEvent.pointerMove(row, {
      clientX: 30,
      clientY: 10,
      pointerId: 2,
      pointerType: 'touch',
    });
    act(() => vi.advanceTimersByTime(500));
    expect(row).not.toHaveClass('is-selected-for-delete');

    const deleteButton = within(row).getByRole('button', { name: /删除分集/ });
    fireEvent.pointerDown(deleteButton, {
      button: 0,
      clientX: 10,
      clientY: 10,
      isPrimary: true,
      pointerId: 3,
      pointerType: 'touch',
    });
    act(() => vi.advanceTimersByTime(500));
    expect(row).not.toHaveClass('is-selected-for-delete');
  });

  it('keeps accessible synthetic activation while ignoring physical clicks', async () => {
    const eligible = summaryItem('eligible');
    renderList({ items: [eligible], onBulkDeleteReviewItems: vi.fn() });
    const row = screen.getByTestId('review-item-row-eligible');

    await userEvent.click(row);
    expect(row).not.toHaveClass('is-selected-for-delete');
    fireEvent.click(row, { detail: 0 });
    expect(row).toHaveClass('is-selected-for-delete');
    fireEvent.click(row, { detail: 0 });
    expect(row).not.toHaveClass('is-selected-for-delete');
  });

  it('ignores non-primary pointers and non-primary buttons', () => {
    vi.useFakeTimers();
    const eligible = summaryItem('eligible');
    renderList({ items: [eligible], onBulkDeleteReviewItems: vi.fn() });
    const row = screen.getByTestId('review-item-row-eligible');

    fireEvent.pointerDown(row, {
      button: 0,
      clientX: 10,
      clientY: 10,
      isPrimary: false,
      pointerId: 2,
      pointerType: 'touch',
    });
    act(() => vi.advanceTimersByTime(500));
    expect(row).not.toHaveClass('is-selected-for-delete');

    fireEvent.pointerDown(row, {
      button: 2,
      clientX: 10,
      clientY: 10,
      isPrimary: true,
      pointerId: 3,
      pointerType: 'pen',
    });
    act(() => vi.advanceTimersByTime(500));
    expect(row).not.toHaveClass('is-selected-for-delete');
  });

  it('cancels on pointer leave and pointer cancel', () => {
    vi.useFakeTimers();
    const eligible = summaryItem('eligible');
    renderList({ items: [eligible], onBulkDeleteReviewItems: vi.fn() });
    const row = screen.getByTestId('review-item-row-eligible');

    for (const [pointerId, cancel] of [
      [4, () => fireEvent.pointerLeave(row, { pointerId: 4, pointerType: 'touch' })],
      [5, () => fireEvent.pointerCancel(row, { pointerId: 5, pointerType: 'touch' })],
    ] as const) {
      fireEvent.pointerDown(row, {
        button: 0,
        clientX: 10,
        clientY: 10,
        isPrimary: true,
        pointerId,
        pointerType: 'touch',
      });
      cancel();
      act(() => vi.advanceTimersByTime(500));
      expect(row).not.toHaveClass('is-selected-for-delete');
    }
  });

  it('rechecks current pending and eligibility state at the long-press threshold', () => {
    vi.useFakeTimers();
    const eligible = summaryItem('eligible');
    const ineligible = summaryItem('eligible', {
      bulkDelete: {
        eligible: false,
        locked: false,
        reason: 'REVIEW_STARTED',
      },
    });
    const { rerenderList } = renderList({
      items: [eligible],
      onBulkDeleteReviewItems: vi.fn(),
    });
    const row = screen.getByTestId('review-item-row-eligible');

    fireEvent.pointerDown(row, {
      button: 0,
      clientX: 10,
      clientY: 10,
      isPrimary: true,
      pointerId: 6,
      pointerType: 'touch',
    });
    act(() => vi.advanceTimersByTime(499));
    rerenderList([eligible], true);
    act(() => vi.advanceTimersByTime(1));
    expect(row).not.toHaveClass('is-selected-for-delete');

    rerenderList([eligible], false);
    fireEvent.pointerDown(row, {
      button: 0,
      clientX: 10,
      clientY: 10,
      isPrimary: true,
      pointerId: 7,
      pointerType: 'touch',
    });
    act(() => vi.advanceTimersByTime(499));
    rerenderList([ineligible], false);
    act(() => vi.advanceTimersByTime(1));
    expect(row).not.toHaveClass('is-selected-for-delete');
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
      '确认撤销第 13 集定稿？关联项目包将立即失效、无法继续下载，并进入物理删除的受控清理。',
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
