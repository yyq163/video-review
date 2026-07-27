import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import type {
  EntryMode,
  FinalizationRecord,
  ReviewIssue,
  ReviewVersion,
} from '../contracts/types';
import { DeleteReviewItemResultUncertainError } from '../adapters/http-review-uploads';
import { createSeedData } from '../core/seed';
import {
  ReviewRuntimeProvider,
  createReviewRuntime,
  type ReviewRuntime,
} from '../entry/runtime';
import type { ReviewItemWithMetadata } from '../ports';
import { ProjectDetailPage } from './ProjectDetailPage';
import { ProjectDetailItemList } from './project-detail-item-list';

interface DeleteGateFixture {
  entryMode: EntryMode;
  finalizations: FinalizationRecord[];
  isArchived: boolean;
  issues: ReviewIssue[];
  item: ReviewItemWithMetadata;
  versions: ReviewVersion[];
}

const activeRuntimes: ReviewRuntime[] = [];
const activeQueryClients: QueryClient[] = [];

function createDeleteGateFixture(): DeleteGateFixture {
  const seed = createSeedData();
  const item: ReviewItemWithMetadata = {
    ...seed.items[0],
    itemCode: 'DELETE-99',
    reviewItemId: 'item_delete_gate',
    currentVersionId: 'version_delete_gate_v1',
    activeFinalizationId: null,
    status: 'pending_review',
  };
  const version: ReviewVersion = {
    ...seed.versions[0],
    reviewItemId: item.reviewItemId,
    versionId: item.currentVersionId,
    versionNo: 1,
    label: 'V1',
    status: 'pending_review',
  };
  return {
    entryMode: 'edit',
    finalizations: [],
    isArchived: false,
    issues: [],
    item,
    versions: [version],
  };
}

function renderDeleteGate(fixture: DeleteGateFixture) {
  const runtime = createReviewRuntime();
  activeRuntimes.push(runtime);
  const onDeleteReviewItem = vi.fn();

  render(
    <ReviewRuntimeProvider runtime={runtime}>
      <MemoryRouter>
        <ProjectDetailItemList
          entryMode={fixture.entryMode}
          episodeGroups={[
            {
              episodeKey: fixture.item.episode,
              representative: fixture.item,
              items: [fixture.item],
            },
          ]}
          finalizations={fixture.finalizations}
          isArchived={fixture.isArchived}
          issuesByVersion={Object.fromEntries(
            fixture.versions.map((version) => [
              version.versionId,
              fixture.issues.filter((issue) => issue.versionId === version.versionId),
            ]),
          )}
          itemActionPending={false}
          onDeleteReviewItem={onDeleteReviewItem}
          onUpdateReviewItemMetadata={vi.fn(async () => undefined)}
          projectRefId={fixture.item.projectRefId}
          versionsByItem={{ [fixture.item.reviewItemId]: fixture.versions }}
        />
      </MemoryRouter>
    </ReviewRuntimeProvider>,
  );

  return onDeleteReviewItem;
}

async function renderDetailWithDeletableItem() {
  const runtime = createReviewRuntime();
  activeRuntimes.push(runtime);
  const editApi = runtime.getApi('edit');
  const created = await editApi.createReviewItemWithVersion(
    {
      projectRefId: 'prj_seed_final_cut',
      title: '审核前可删除分集',
      episode: '99',
      file: new File(['single-item-delete'], 'single-item-delete.mp4', {
        type: 'video/mp4',
      }),
    },
    editApi.entryPolicy.createContext('edit'),
  );
  const deleteReviewItem = vi.spyOn(editApi, 'deleteReviewItem');
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  activeQueryClients.push(queryClient);

  render(
    <QueryClientProvider client={queryClient}>
      <ReviewRuntimeProvider runtime={runtime}>
        <MemoryRouter initialEntries={['/edit/projects/prj_seed_final_cut']}>
          <Routes>
            <Route
              path="/edit/projects/:projectRefId"
              element={<ProjectDetailPage entryMode="edit" />}
            />
          </Routes>
        </MemoryRouter>
      </ReviewRuntimeProvider>
    </QueryClientProvider>,
  );

  return { created, deleteReviewItem };
}

afterEach(() => {
  cleanup();
  for (const queryClient of activeQueryClients.splice(0)) queryClient.clear();
  for (const runtime of activeRuntimes.splice(0)) runtime.dispose();
  window.localStorage.clear();
  window.sessionStorage.clear();
  vi.restoreAllMocks();
});

describe('ProjectDetailItemList finalized row state', () => {
  it('marks the entire finalized episode row without changing non-finalized rows', () => {
    const finalizedFixture = createDeleteGateFixture();
    finalizedFixture.item = {
      ...finalizedFixture.item,
      reviewItemId: 'item_finalized_row',
      currentVersionId: 'version_finalized_row_v1',
      status: 'finalized',
    };
    finalizedFixture.versions = [{
      ...finalizedFixture.versions[0],
      reviewItemId: finalizedFixture.item.reviewItemId,
      versionId: finalizedFixture.item.currentVersionId,
      status: 'finalized',
    }];
    renderDeleteGate(finalizedFixture);

    const finalizedBadge = screen.getByText('已定稿');
    expect(finalizedBadge.closest('article')).toHaveClass('is-finalized');

    cleanup();

    const pendingFixture = createDeleteGateFixture();
    renderDeleteGate(pendingFixture);
    const pendingBadge = screen.getByText('待审');
    expect(pendingBadge.closest('article')).not.toHaveClass('is-finalized');
  });
});

describe('ProjectDetailItemList row watermarks', () => {
  function createWatermarkFixture() {
    const fixture = createDeleteGateFixture();
    const currentVersion: ReviewVersion = {
      ...fixture.versions[0],
      versionId: 'version_watermark_v2',
      versionNo: 2,
      label: 'V2',
    };
    fixture.item = {
      ...fixture.item,
      reviewItemId: 'item_watermark_row',
      currentVersionId: currentVersion.versionId,
    };
    fixture.versions = [
      { ...fixture.versions[0], reviewItemId: fixture.item.reviewItemId },
      { ...currentVersion, reviewItemId: fixture.item.reviewItemId },
    ];
    fixture.issues = [
      {
        ...createSeedData().issues[0],
        issueId: 'issue_current_open',
        reviewItemId: fixture.item.reviewItemId,
        versionId: currentVersion.versionId,
        status: 'unresolved',
      },
      {
        ...createSeedData().issues[0],
        issueId: 'issue_current_resolved',
        reviewItemId: fixture.item.reviewItemId,
        versionId: currentVersion.versionId,
        status: 'resolved',
      },
      {
        ...createSeedData().issues[0],
        issueId: 'issue_history_open',
        reviewItemId: fixture.item.reviewItemId,
        versionId: 'version_delete_gate_v1',
        status: 'unresolved',
      },
    ];
    return fixture;
  }

  it('shows the current version label as the row watermark instead of a historical or fixed label', () => {
    renderDeleteGate(createWatermarkFixture());

    const watermark = screen.getByTestId('item-row-version-watermark-item_watermark_row');
    expect(watermark).toHaveTextContent('V2');
    expect(watermark).toHaveAttribute('aria-hidden', 'true');
  });

  it('shows only the current version unresolved count in both the watermark and the foreground text', () => {
    renderDeleteGate(createWatermarkFixture());

    const watermark = screen.getByTestId('item-row-count-watermark-item_watermark_row');
    expect(watermark).toHaveTextContent('1');
    expect(watermark).toHaveAttribute('aria-hidden', 'true');
    expect(screen.getByText('当前未修改 1')).toBeInTheDocument();
  });
});

describe('ProjectDetailItemList single-item delete gate', () => {
  const hiddenCases: Array<{
    apply: (fixture: DeleteGateFixture) => void;
    name: string;
  }> = [
    {
      name: '审核已开始',
      apply: (fixture) => {
        fixture.item = { ...fixture.item, status: 'in_review' };
      },
    },
    {
      name: '存在多个版本',
      apply: (fixture) => {
        fixture.versions = [
          ...fixture.versions,
          {
            ...fixture.versions[0],
            versionId: 'version_delete_gate_v2',
            versionNo: 2,
            label: 'V2',
          },
        ];
      },
    },
    {
      name: '已有审核意见',
      apply: (fixture) => {
        const seedIssue = createSeedData().issues[0];
        fixture.issues = [
          {
            ...seedIssue,
            projectRefId: fixture.item.projectRefId,
            reviewItemId: fixture.item.reviewItemId,
            versionId: fixture.versions[0].versionId,
          },
        ];
      },
    },
    {
      name: '已有定稿记录',
      apply: (fixture) => {
        const version = fixture.versions[0];
        fixture.finalizations = [
          {
            finalizationId: 'finalization_delete_gate',
            projectRefId: fixture.item.projectRefId,
            reviewItemId: fixture.item.reviewItemId,
            versionId: version.versionId,
            originalFileId: version.originalFileId,
            sha256: version.sha256,
            fileName: version.fileName,
            originalMedia: version.originalMedia,
            frozenAt: '2026-07-14T00:00:00.000Z',
          },
        ];
      },
    },
    {
      name: '存在活跃定稿',
      apply: (fixture) => {
        fixture.item = {
          ...fixture.item,
          activeFinalizationId: 'finalization_active_delete_gate',
        };
      },
    },
    {
      name: '项目已归档',
      apply: (fixture) => {
        fixture.isArchived = true;
      },
    },
  ];

  it.each(hiddenCases)('$name时隐藏删除入口', ({ apply }) => {
    const fixture = createDeleteGateFixture();
    apply(fixture);
    renderDeleteGate(fixture);

    expect(
      screen.queryByRole('button', { name: `删除分集 ${fixture.item.title}` }),
    ).not.toBeInTheDocument();
  });

  it('审核开始前仅有一个无意见、无定稿版本时允许删除', () => {
    const fixture = createDeleteGateFixture();
    renderDeleteGate(fixture);

    expect(screen.getByText(`第 ${fixture.item.episode} 集`, { selector: 'strong' })).toBeInTheDocument();
    expect(screen.queryByText(fixture.item.title, { selector: 'strong' })).not.toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: `删除分集 ${fixture.item.title}` }),
    ).toBeEnabled();
  });

  it('取消确认时不调用 delete，确认后只调用一次', async () => {
    const confirm = vi
      .spyOn(window, 'confirm')
      .mockReturnValueOnce(false)
      .mockReturnValueOnce(true);
    const { created, deleteReviewItem } = await renderDetailWithDeletableItem();
    const deleteButton = await screen.findByRole('button', {
      name: `删除分集 ${created.item.title}`,
    });

    await userEvent.click(deleteButton);
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(deleteReviewItem).not.toHaveBeenCalled();

    await userEvent.click(deleteButton);
    await waitFor(() => expect(deleteReviewItem).toHaveBeenCalledOnce());
    expect(confirm).toHaveBeenCalledTimes(2);
    expect(deleteReviewItem).toHaveBeenCalledWith(
      {
        projectRefId: created.item.projectRefId,
        reviewItemId: created.item.reviewItemId,
        confirmed: true,
      },
      expect.objectContaining({ entryMode: 'edit' }),
    );
    expect(await screen.findByText('分集已删除，列表已刷新。')).toBeInTheDocument();
  });
});

describe('ProjectDetailItemList batch delete', () => {
  it('exposes the same selectable bulk-delete controls from the review entry without deleting on cancel', async () => {
    const fixture = createDeleteGateFixture();
    fixture.entryMode = 'review';
    const runtime = createReviewRuntime();
    activeRuntimes.push(runtime);
    const onBulkDeleteReviewItems = vi.fn(async () => ({
      succeededIds: [],
      failures: {},
      uncertainIds: [],
    }));
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);

    render(
      <ReviewRuntimeProvider runtime={runtime}>
        <MemoryRouter>
          <ProjectDetailItemList
            entryMode={fixture.entryMode}
            episodeGroups={[
              {
                episodeKey: fixture.item.episode,
                representative: fixture.item,
                items: [fixture.item],
              },
            ]}
            finalizations={fixture.finalizations}
            isArchived={fixture.isArchived}
            issuesByVersion={{ [fixture.versions[0].versionId]: [] }}
            itemActionPending={false}
            onBulkDeleteReviewItems={onBulkDeleteReviewItems}
            onDeleteReviewItem={vi.fn()}
            onUpdateReviewItemMetadata={vi.fn(async () => undefined)}
            projectRefId={fixture.item.projectRefId}
            versionsByItem={{ [fixture.item.reviewItemId]: fixture.versions }}
          />
        </MemoryRouter>
      </ReviewRuntimeProvider>,
    );

    const selectAll = screen.getByRole('checkbox', { name: '全选可删除分集' });
    const rowSelector = screen.getByRole('checkbox', {
      name: `选择第${fixture.item.episode}集`,
    });
    const deleteButton = screen.getByRole('button', { name: '批量删除（0）' });

    expect(selectAll).not.toBeChecked();
    expect(deleteButton).toBeDisabled();

    await userEvent.click(rowSelector);
    expect(selectAll).toBeChecked();
    expect(screen.getByRole('button', { name: '批量删除（1）' })).toBeEnabled();

    await userEvent.click(screen.getByRole('button', { name: '批量删除（1）' }));
    expect(confirm).toHaveBeenCalledWith(
      '确认批量删除已选择的 1 条待审分集？该操作逐条执行且无法撤销。',
    );
    expect(onBulkDeleteReviewItems).not.toHaveBeenCalled();
    expect(rowSelector).toBeChecked();
  });

  it('keeps an immediately visible uncertain delete locked instead of calling it safe to retry', async () => {
    const runtime = createReviewRuntime();
    activeRuntimes.push(runtime);
    const editApi = runtime.getApi('edit');
    const created = await editApi.createReviewItemWithVersion(
      {
        projectRefId: 'prj_seed_final_cut',
        title: '即时仍可见的不确定删除',
        episode: '03',
        file: new File(['uncertain-delete'], 'uncertain-delete.mp4', { type: 'video/mp4' }),
      },
      editApi.entryPolicy.createContext('edit'),
    );
    vi.spyOn(editApi, 'deleteReviewItem').mockRejectedValue(
      new DeleteReviewItemResultUncertainError(new TypeError('lost response')),
    );
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    activeQueryClients.push(queryClient);

    render(
      <QueryClientProvider client={queryClient}>
        <ReviewRuntimeProvider runtime={runtime}>
          <MemoryRouter initialEntries={['/edit/projects/prj_seed_final_cut']}>
            <Routes>
              <Route
                path="/edit/projects/:projectRefId"
                element={<ProjectDetailPage entryMode="edit" />}
              />
            </Routes>
          </MemoryRouter>
        </ReviewRuntimeProvider>
      </QueryClientProvider>,
    );

    const selector = await screen.findByRole('checkbox', { name: '选择第03集' });
    await userEvent.click(selector);
    await userEvent.click(screen.getByRole('button', { name: '批量删除（1）' }));

    await waitFor(() => expect(selector).toBeDisabled());
    expect(screen.getByText(/删除结果不确定，已锁定/)).toBeInTheDocument();
    expect(screen.queryByText(/可安全重试/)).not.toBeInTheDocument();
    expect(screen.getByText(/不确定 1 条/)).toBeInTheDocument();
    expect(created.item.reviewItemId).toBeTruthy();
  });

  it('preserves the selected representative for a duplicate episode until that item is deleted', () => {
    const seed = createSeedData();
    const first = {
      ...createDeleteGateFixture().item,
      reviewItemId: 'duplicate-first',
      currentVersionId: 'duplicate-first-v1',
      episode: '90',
      title: '先返回但不是代表项',
    };
    const representative = {
      ...first,
      reviewItemId: 'duplicate-representative',
      currentVersionId: 'duplicate-representative-v1',
      title: '按版本和更新时间选出的代表项',
    };
    const versionsByItem = {
      [first.reviewItemId]: [{
        ...seed.versions[0],
        reviewItemId: first.reviewItemId,
        versionId: first.currentVersionId,
      }],
      [representative.reviewItemId]: [{
        ...seed.versions[0],
        reviewItemId: representative.reviewItemId,
        versionId: representative.currentVersionId,
      }],
    };
    const runtime = createReviewRuntime();
    activeRuntimes.push(runtime);

    render(
      <ReviewRuntimeProvider runtime={runtime}>
        <MemoryRouter>
          <ProjectDetailItemList
            entryMode="edit"
            episodeGroups={[{
              episodeKey: first.episode,
              representative,
              items: [first, representative],
            }]}
            finalizations={[]}
            isArchived={false}
            issuesByVersion={{
              [first.currentVersionId]: [],
              [representative.currentVersionId]: [],
            }}
            itemActionPending={false}
            onBulkDeleteReviewItems={vi.fn()}
            onDeleteReviewItem={vi.fn()}
            onUpdateReviewItemMetadata={vi.fn(async () => undefined)}
            projectRefId={first.projectRefId}
            versionsByItem={versionsByItem}
          />
        </MemoryRouter>
      </ReviewRuntimeProvider>,
    );

    expect(screen.getByRole('link', { name: '查看与追加' })).toHaveAttribute(
      'href',
      expect.stringContaining(representative.reviewItemId),
    );
    const duplicateSelectors = screen.getAllByRole('checkbox', { name: '选择第90集' });
    expect(duplicateSelectors).toHaveLength(2);
    expect(duplicateSelectors[0]).toHaveAttribute(
      'aria-describedby',
      `delete-target-${first.reviewItemId}`,
    );
    expect(duplicateSelectors[1]).toHaveAttribute(
      'aria-describedby',
      `delete-target-${representative.reviewItemId}`,
    );
    expect(document.getElementById(`delete-target-${first.reviewItemId}`)).toHaveTextContent(
      first.title,
    );
    expect(document.getElementById(`delete-target-${representative.reviewItemId}`)).toHaveTextContent(
      representative.title,
    );
  });

  it('confirms the exact count, continues after a failure, and retains only failed selections', async () => {
    const seed = createSeedData();
    const items = Array.from({ length: 3 }, (_, index) => ({
      ...createDeleteGateFixture().item,
      reviewItemId: `batch-delete-${index + 1}`,
      currentVersionId: `batch-delete-version-${index + 1}`,
      episode: String(90 + index),
      title: `批量删除 ${index + 1}`,
    }));
    const versions = Object.fromEntries(items.map((item, index) => [
      item.reviewItemId,
      [{
        ...seed.versions[0],
        reviewItemId: item.reviewItemId,
        versionId: item.currentVersionId,
        originalFileId: `original-file-${index + 1}`,
        originalMedia: {
          ...seed.versions[0].originalMedia,
          originalFileId: `original-file-${index + 1}`,
          originalFilename: '相同文件名.mp4',
        },
      }],
    ]));
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const onBulkDeleteReviewItems = vi.fn(async () => ({
      succeededIds: [items[0].reviewItemId],
      failures: { [items[1].reviewItemId]: '精确资源删除失败' },
      uncertainIds: [items[2].reviewItemId],
    }));
    const runtime = createReviewRuntime();
    activeRuntimes.push(runtime);

    render(
      <ReviewRuntimeProvider runtime={runtime}>
        <MemoryRouter>
          <ProjectDetailItemList
            entryMode="edit"
            episodeGroups={items.map((item) => ({
              episodeKey: item.episode,
              representative: item,
              items: [item],
            }))}
            finalizations={[]}
            isArchived={false}
            issuesByVersion={Object.fromEntries(
              Object.values(versions).flat().map((version) => [version.versionId, []]),
            )}
            itemActionPending={false}
            onBulkDeleteReviewItems={onBulkDeleteReviewItems}
            onDeleteReviewItem={vi.fn()}
            onUpdateReviewItemMetadata={vi.fn(async () => undefined)}
            projectRefId={items[0].projectRefId}
            versionsByItem={versions}
          />
        </MemoryRouter>
      </ReviewRuntimeProvider>,
    );

    await userEvent.click(screen.getByRole('checkbox', { name: '全选可删除分集' }));
    await userEvent.click(screen.getByRole('button', { name: '批量删除（3）' }));

    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('3'));
    expect(onBulkDeleteReviewItems).toHaveBeenCalledWith(items);
    expect(await screen.findByText('精确资源删除失败')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '批量删除（1）' })).toBeEnabled();
    expect(screen.getByRole('checkbox', { name: `选择第${items[1].episode}集` })).toBeChecked();
    expect(screen.queryByRole('checkbox', { name: `选择第${items[0].episode}集` })).not.toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: `选择第${items[2].episode}集` })).toBeDisabled();
    expect(screen.getByRole('button', { name: `删除分集 ${items[2].title}` })).toBeDisabled();
    expect(screen.getByTestId(`batch-delete-uncertain-${items[2].reviewItemId}`)).toHaveTextContent(
      '删除结果不确定',
    );
    expect(versions[items[0].reviewItemId][0].originalMedia.originalFilename).toBe(
      versions[items[1].reviewItemId][0].originalMedia.originalFilename,
    );
  });

  it('keeps direct selection and select-all deselection stable without losing sibling state', async () => {
    const seed = createSeedData();
    const items = Array.from({ length: 3 }, (_, index) => ({
      ...createDeleteGateFixture().item,
      reviewItemId: `selection-stability-${index + 1}`,
      currentVersionId: `selection-stability-version-${index + 1}`,
      episode: String(70 + index),
      title: `选择稳定性 ${index + 1}`,
    }));
    const versionsByItem = Object.fromEntries(items.map((item) => [
      item.reviewItemId,
      [{
        ...seed.versions[0],
        reviewItemId: item.reviewItemId,
        versionId: item.currentVersionId,
      }],
    ]));
    const runtime = createReviewRuntime();
    activeRuntimes.push(runtime);

    render(
      <ReviewRuntimeProvider runtime={runtime}>
        <MemoryRouter>
          <ProjectDetailItemList
            entryMode="edit"
            episodeGroups={items.map((item) => ({
              episodeKey: item.episode,
              representative: item,
              items: [item],
            }))}
            finalizations={[]}
            isArchived={false}
            issuesByVersion={Object.fromEntries(
              Object.values(versionsByItem).flat().map((version) => [version.versionId, []]),
            )}
            itemActionPending={false}
            onBulkDeleteReviewItems={vi.fn()}
            onDeleteReviewItem={vi.fn()}
            onUpdateReviewItemMetadata={vi.fn(async () => undefined)}
            projectRefId={items[0].projectRefId}
            versionsByItem={versionsByItem}
          />
        </MemoryRouter>
      </ReviewRuntimeProvider>,
    );

    const first = screen.getByRole('checkbox', { name: `选择第${items[0].episode}集` });
    const second = screen.getByRole('checkbox', { name: `选择第${items[1].episode}集` });
    const selectAll = screen.getByRole('checkbox', { name: '全选可删除分集' });
    await userEvent.click(first);
    expect(first).toBeChecked();
    expect(second).not.toBeChecked();
    expect(selectAll).not.toBeChecked();
    expect(selectAll).toHaveProperty('indeterminate', true);

    await userEvent.click(selectAll);
    expect(first).toBeChecked();
    expect(second).toBeChecked();
    expect(selectAll).toBeChecked();
    expect(selectAll).toHaveProperty('indeterminate', false);

    await userEvent.click(first);
    expect(first).not.toBeChecked();
    expect(second).toBeChecked();
    expect(selectAll).not.toBeChecked();
    expect(selectAll).toHaveProperty('indeterminate', true);

    await userEvent.click(first);
    expect(first).toBeChecked();
    expect(second).toBeChecked();
    expect(selectAll).toBeChecked();
    expect(selectAll).toHaveProperty('indeterminate', false);
  });

  it('preserves an older uncertain lock when a later unrelated batch settles', async () => {
    const seed = createSeedData();
    const items = Array.from({ length: 2 }, (_, index) => ({
      ...createDeleteGateFixture().item,
      reviewItemId: `uncertain-lock-${index + 1}`,
      currentVersionId: `uncertain-lock-version-${index + 1}`,
      episode: String(80 + index),
      title: `不确定锁 ${index + 1}`,
    }));
    const versionsByItem = Object.fromEntries(items.map((item) => [
      item.reviewItemId,
      [{
        ...seed.versions[0],
        reviewItemId: item.reviewItemId,
        versionId: item.currentVersionId,
      }],
    ]));
    const onBulkDeleteReviewItems = vi.fn()
      .mockResolvedValueOnce({
        succeededIds: [],
        failures: {},
        uncertainIds: [items[0].reviewItemId],
      })
      .mockResolvedValueOnce({
        succeededIds: [],
        failures: { [items[1].reviewItemId]: '第二批失败' },
        uncertainIds: [],
      });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const runtime = createReviewRuntime();
    activeRuntimes.push(runtime);

    render(
      <ReviewRuntimeProvider runtime={runtime}>
        <MemoryRouter>
          <ProjectDetailItemList
            entryMode="edit"
            episodeGroups={items.map((item) => ({
              episodeKey: item.episode,
              representative: item,
              items: [item],
            }))}
            finalizations={[]}
            isArchived={false}
            issuesByVersion={Object.fromEntries(
              Object.values(versionsByItem).flat().map((version) => [version.versionId, []]),
            )}
            itemActionPending={false}
            onBulkDeleteReviewItems={onBulkDeleteReviewItems}
            onDeleteReviewItem={vi.fn()}
            onUpdateReviewItemMetadata={vi.fn(async () => undefined)}
            projectRefId={items[0].projectRefId}
            versionsByItem={versionsByItem}
          />
        </MemoryRouter>
      </ReviewRuntimeProvider>,
    );

    const first = screen.getByRole('checkbox', { name: `选择第${items[0].episode}集` });
    const second = screen.getByRole('checkbox', { name: `选择第${items[1].episode}集` });
    await userEvent.click(first);
    await userEvent.click(screen.getByRole('button', { name: '批量删除（1）' }));
    await waitFor(() => expect(first).toBeDisabled());

    await userEvent.click(second);
    await userEvent.click(screen.getByRole('button', { name: '批量删除（1）' }));
    expect(await screen.findByText('第二批失败')).toBeInTheDocument();
    expect(first).toBeDisabled();
    expect(screen.getByTestId(`batch-delete-uncertain-${items[0].reviewItemId}`)).toBeInTheDocument();
  });

  it('places the compact bulk delete control before file selection and each row checkbox before its title', async () => {
    const { created } = await renderDetailWithDeletableItem();
    const checkbox = await screen.findByRole('checkbox', {
      name: `选择第${created.item.episode}集`,
    });
    await userEvent.click(checkbox);

    const bulkDelete = screen.getByRole('button', { name: '批量删除（1）' });
    const chooseFiles = screen.getByRole('button', { name: '选择文件' });
    expect(bulkDelete.closest('.fj-review-upload-actions')).not.toBeNull();
    expect(
      bulkDelete.compareDocumentPosition(chooseFiles) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(bulkDelete).toHaveClass('fj-review-bulk-delete-button');
    expect(bulkDelete).toHaveTextContent('批量删除（1）');
    expect(bulkDelete).toBeVisible();
    const css = readFileSync(
      resolve(process.cwd(), 'src/modules/final-cut-review/styles/fj-review.css'),
      'utf8',
    );
    expect(css).toMatch(
      /\.fj-review-root\s+button\.fj-review-bulk-delete-button\s*\{[^}]*background:\s*var\(--fj-review-red\);[^}]*color:\s*var\(--fj-review-bg\);[^}]*min-height:\s*30px;/s,
    );
    expect(css).toMatch(
      /\.fj-review-root\s+button\.fj-review-bulk-delete-button:hover:not\(:disabled\),\s*\.fj-review-root\s+button\.fj-review-bulk-delete-button:focus-visible:not\(:disabled\)\s*\{[^}]*background:\s*var\(--fj-review-red\);[^}]*color:\s*var\(--fj-review-bg\);/s,
    );
    expect(css).toMatch(
      /\.fj-review-root\s+button\.fj-review-bulk-delete-button:disabled\s*\{[^}]*background:\s*var\(--fj-review-surface-2\);[^}]*color:\s*var\(--fj-review-muted\);/s,
    );
    expect(document.querySelector('.fj-review-bulk-delete-toolbar')).not.toBeInTheDocument();

    const row = checkbox.closest('article');
    expect(row).not.toBeNull();
    const title = within(row as HTMLElement).getByText(`第 ${created.item.episode} 集`);
    expect(
      checkbox.compareDocumentPosition(title) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(within(row as HTMLElement).queryByText('选择')).not.toBeInTheDocument();
  });

  it('does not expose batch controls when no bulk-delete handler is provided', () => {
    const fixture = createDeleteGateFixture();
    fixture.entryMode = 'review';
    renderDeleteGate(fixture);
    expect(screen.queryByRole('checkbox', { name: '全选可删除分集' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /批量删除/ })).not.toBeInTheDocument();
  });
});
