import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import type {
  EntryMode,
  FinalizationRecord,
  ReviewIssue,
  ReviewVersion,
} from '../contracts/types';
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
    {
      name: '从 review 入口访问',
      apply: (fixture) => {
        fixture.entryMode = 'review';
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
