import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import {
  beginRevokeFinalizationOperation,
  clearRevokeFinalizationOperation,
  getRevokeFinalizationProtectionState,
  RevokeFinalizationResultUncertainError,
} from '../adapters/http-review-finalization-operation';
import {
  ReviewRuntimeProvider,
  createReviewRuntime,
  type ReviewRuntime,
} from '../entry/runtime';
import { reviewKeys } from '../entry/use-review-queries';
import type { ReviewProjectSummary } from '../ports';
import { ProjectDetailPage } from './ProjectDetailPage';
import { ReviewWorkspacePage } from './ReviewWorkspacePage';

const runtimes: ReviewRuntime[] = [];
const queryClients: QueryClient[] = [];

afterEach(() => {
  for (const runtime of runtimes.splice(0)) runtime.dispose();
  for (const queryClient of queryClients.splice(0)) queryClient.clear();
  window.sessionStorage.clear();
  vi.restoreAllMocks();
});

describe('ProjectDetailPage uncertain finalization revocation reconciliation', () => {
  it('masks an uncached workspace load and every refetch while revocation authority is uncertain', async () => {
    const runtime = createReviewRuntime();
    runtimes.push(runtime);
    const review = runtime.getApi('review');
    const workspace = await review.getWorkspace({
      projectRefId: 'prj_seed_final_cut',
      reviewItemId: 'item_ep28',
    });
    await review.finalizeCurrentVersion(
      {
        projectRefId: workspace.project.projectRefId,
        reviewItemId: workspace.item.reviewItemId,
        versionId: workspace.item.currentVersionId,
        confirmed: true,
      },
      review.entryPolicy.createContext('review'),
    );
    beginRevokeFinalizationOperation('prj_seed_final_cut', 'item_ep28', 1);
    const getWorkspace = vi.spyOn(review, 'getWorkspace');
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    queryClients.push(queryClient);
    render(
      <QueryClientProvider client={queryClient}>
        <ReviewRuntimeProvider runtime={runtime}>
          <MemoryRouter
            initialEntries={[
              '/review/projects/prj_seed_final_cut/items/item_ep28',
            ]}
          >
            <Routes>
              <Route
                path="/review/projects/:projectRefId/items/:reviewItemId"
                element={<ReviewWorkspacePage entryMode="review" />}
              />
            </Routes>
          </MemoryRouter>
        </ReviewRuntimeProvider>
      </QueryClientProvider>,
    );

    expect(await screen.findByText('撤回结果确认中')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '下载单片定稿原片' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '打包项目定稿原片' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: '最终通过' })).not.toBeInTheDocument();

    await queryClient.invalidateQueries({
      queryKey: reviewKeys.workspace('prj_seed_final_cut', 'item_ep28'),
    });
    await waitFor(() => expect(getWorkspace.mock.calls.length).toBeGreaterThanOrEqual(2));
    expect(screen.getByText('撤回结果确认中')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '下载单片定稿原片' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '打包项目定稿原片' })).toBeDisabled();
  });

  it('isolates authority cache across revoke, refinalize, and a second uncertain revoke', async () => {
    const runtime = createReviewRuntime();
    runtimes.push(runtime);
    const review = runtime.getApi('review');
    const workspace = await review.getWorkspace({
      projectRefId: 'prj_seed_final_cut',
      reviewItemId: 'item_ep28',
    });
    await review.finalizeCurrentVersion(
      {
        projectRefId: workspace.project.projectRefId,
        reviewItemId: workspace.item.reviewItemId,
        versionId: workspace.item.currentVersionId,
        confirmed: true,
      },
      review.entryPolicy.createContext('review'),
    );
    const firstOperation = beginRevokeFinalizationOperation(
      'prj_seed_final_cut',
      'item_ep28',
      1,
    );
    await review.revokeFinalization(
      {
        projectRefId: 'prj_seed_final_cut',
        reviewItemId: 'item_ep28',
        confirmed: true,
      },
      review.entryPolicy.createContext('review'),
    );
    clearRevokeFinalizationOperation('prj_seed_final_cut', 'item_ep28');
    const staleRevokedItem = await review.getReviewItem({
      projectRefId: 'prj_seed_final_cut',
      reviewItemId: 'item_ep28',
    });
    await review.finalizeCurrentVersion(
      {
        projectRefId: 'prj_seed_final_cut',
        reviewItemId: 'item_ep28',
        versionId: workspace.item.currentVersionId,
        confirmed: true,
      },
      review.entryPolicy.createContext('review'),
    );
    const secondOperation = beginRevokeFinalizationOperation(
      'prj_seed_final_cut',
      'item_ep28',
      2,
    );
    expect(secondOperation.commandId).not.toBe(firstOperation.commandId);
    const getReviewItem = vi.spyOn(review, 'getReviewItem');
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    queryClients.push(queryClient);
    queryClient.setQueryData(
      reviewKeys.revocationAuthority(
        'prj_seed_final_cut',
        'item_ep28',
        firstOperation.commandId,
      ),
      staleRevokedItem,
    );
    render(
      <QueryClientProvider client={queryClient}>
        <ReviewRuntimeProvider runtime={runtime}>
          <MemoryRouter
            initialEntries={[
              '/review/projects/prj_seed_final_cut/items/item_ep28',
            ]}
          >
            <Routes>
              <Route
                path="/review/projects/:projectRefId/items/:reviewItemId"
                element={<ReviewWorkspacePage entryMode="review" />}
              />
            </Routes>
          </MemoryRouter>
        </ReviewRuntimeProvider>
      </QueryClientProvider>,
    );

    expect(await screen.findByText('撤回结果确认中')).toBeInTheDocument();
    await waitFor(() => expect(getReviewItem).toHaveBeenCalled());
    expect(
      getRevokeFinalizationProtectionState('prj_seed_final_cut', 'item_ep28'),
    ).toBe('required');
    expect(screen.getByRole('button', { name: '下载单片定稿原片' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '打包项目定稿原片' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: '最终通过' })).not.toBeInTheDocument();
  });

  it('does not let a delayed authority response for command A clear or sync over command B', async () => {
    const runtime = createReviewRuntime();
    runtimes.push(runtime);
    const review = runtime.getApi('review');
    const workspace = await review.getWorkspace({
      projectRefId: 'prj_seed_final_cut',
      reviewItemId: 'item_ep28',
    });
    await review.finalizeCurrentVersion(
      {
        projectRefId: workspace.project.projectRefId,
        reviewItemId: workspace.item.reviewItemId,
        versionId: workspace.item.currentVersionId,
        confirmed: true,
      },
      review.entryPolicy.createContext('review'),
    );
    const initialOperation = beginRevokeFinalizationOperation(
      'prj_seed_final_cut',
      'item_ep28',
      1,
    );
    await review.revokeFinalization(
      {
        projectRefId: 'prj_seed_final_cut',
        reviewItemId: 'item_ep28',
        confirmed: true,
      },
      review.entryPolicy.createContext('review'),
    );
    clearRevokeFinalizationOperation(
      'prj_seed_final_cut',
      'item_ep28',
      initialOperation.commandId,
    );
    const staleRevokedItem = await review.getReviewItem({
      projectRefId: 'prj_seed_final_cut',
      reviewItemId: 'item_ep28',
    });
    await review.finalizeCurrentVersion(
      {
        projectRefId: 'prj_seed_final_cut',
        reviewItemId: 'item_ep28',
        versionId: workspace.item.currentVersionId,
        confirmed: true,
      },
      review.entryPolicy.createContext('review'),
    );
    const finalizedWorkspace = await review.getWorkspace({
      projectRefId: 'prj_seed_final_cut',
      reviewItemId: 'item_ep28',
    });
    const operationA = beginRevokeFinalizationOperation(
      'prj_seed_final_cut',
      'item_ep28',
      2,
    );
    expect(operationA.commandId).not.toBe(initialOperation.commandId);
    let resolveAuthorityA: ((item: typeof staleRevokedItem) => void) | undefined;
    const delayedAuthorityA = new Promise<typeof staleRevokedItem>((resolve) => {
      resolveAuthorityA = resolve;
    });
    const getReviewItem = vi
      .spyOn(review, 'getReviewItem')
      .mockImplementationOnce(() => delayedAuthorityA);
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    queryClients.push(queryClient);
    queryClient.setQueryData(
      reviewKeys.workspace('prj_seed_final_cut', 'item_ep28'),
      finalizedWorkspace,
    );
    render(
      <QueryClientProvider client={queryClient}>
        <ReviewRuntimeProvider runtime={runtime}>
          <MemoryRouter
            initialEntries={[
              '/review/projects/prj_seed_final_cut/items/item_ep28',
            ]}
          >
            <Routes>
              <Route
                path="/review/projects/:projectRefId/items/:reviewItemId"
                element={<ReviewWorkspacePage entryMode="review" />}
              />
            </Routes>
          </MemoryRouter>
        </ReviewRuntimeProvider>
      </QueryClientProvider>,
    );

    expect(await screen.findByText('撤回结果确认中')).toBeInTheDocument();
    await waitFor(() => expect(getReviewItem).toHaveBeenCalledTimes(1));
    expect(
      clearRevokeFinalizationOperation(
        'prj_seed_final_cut',
        'item_ep28',
        operationA.commandId,
      ),
    ).toBe(true);
    const operationB = beginRevokeFinalizationOperation(
      'prj_seed_final_cut',
      'item_ep28',
      3,
    );
    expect(operationB.commandId).not.toBe(operationA.commandId);

    resolveAuthorityA?.(staleRevokedItem);
    await waitFor(() =>
      expect(
        queryClient.getQueryState(
          reviewKeys.revocationAuthority(
            'prj_seed_final_cut',
            'item_ep28',
            operationA.commandId,
          ),
        )?.status,
      ).toBe('success'),
    );

    expect(
      getRevokeFinalizationProtectionState('prj_seed_final_cut', 'item_ep28'),
    ).toBe('required');
    expect(
      queryClient.getQueryData(
        reviewKeys.workspace('prj_seed_final_cut', 'item_ep28'),
      ),
    ).toMatchObject({
      item: { status: 'finalized' },
      activeFinalization: { finalizationId: expect.any(String) },
    });
    expect(screen.getByText('撤回结果确认中')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '下载单片定稿原片' })).toBeDisabled();
  });

  it('does not commit a delayed workspace response from command A after command B starts', async () => {
    const runtime = createReviewRuntime();
    runtimes.push(runtime);
    const review = runtime.getApi('review');
    const workspace = await review.getWorkspace({
      projectRefId: 'prj_seed_final_cut',
      reviewItemId: 'item_ep28',
    });
    await review.finalizeCurrentVersion(
      {
        projectRefId: workspace.project.projectRefId,
        reviewItemId: workspace.item.reviewItemId,
        versionId: workspace.item.currentVersionId,
        confirmed: true,
      },
      review.entryPolicy.createContext('review'),
    );
    const initialOperation = beginRevokeFinalizationOperation(
      'prj_seed_final_cut',
      'item_ep28',
      1,
    );
    await review.revokeFinalization(
      {
        projectRefId: 'prj_seed_final_cut',
        reviewItemId: 'item_ep28',
        confirmed: true,
      },
      review.entryPolicy.createContext('review'),
    );
    clearRevokeFinalizationOperation(
      'prj_seed_final_cut',
      'item_ep28',
      initialOperation.commandId,
    );
    const staleRevokedWorkspace = await review.getWorkspace({
      projectRefId: 'prj_seed_final_cut',
      reviewItemId: 'item_ep28',
    });
    await review.finalizeCurrentVersion(
      {
        projectRefId: 'prj_seed_final_cut',
        reviewItemId: 'item_ep28',
        versionId: workspace.item.currentVersionId,
        confirmed: true,
      },
      review.entryPolicy.createContext('review'),
    );
    const finalizedWorkspace = await review.getWorkspace({
      projectRefId: 'prj_seed_final_cut',
      reviewItemId: 'item_ep28',
    });
    const operationA = beginRevokeFinalizationOperation(
      'prj_seed_final_cut',
      'item_ep28',
      2,
    );
    let resolveWorkspaceA: ((value: typeof staleRevokedWorkspace) => void) | undefined;
    const delayedWorkspaceA = new Promise<typeof staleRevokedWorkspace>((resolve) => {
      resolveWorkspaceA = resolve;
    });
    const getWorkspace = vi
      .spyOn(review, 'getWorkspace')
      .mockImplementationOnce(() => delayedWorkspaceA);
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    queryClients.push(queryClient);
    queryClient.setQueryData(
      reviewKeys.workspace('prj_seed_final_cut', 'item_ep28'),
      finalizedWorkspace,
    );
    render(
      <QueryClientProvider client={queryClient}>
        <ReviewRuntimeProvider runtime={runtime}>
          <MemoryRouter
            initialEntries={[
              '/review/projects/prj_seed_final_cut/items/item_ep28',
            ]}
          >
            <Routes>
              <Route
                path="/review/projects/:projectRefId/items/:reviewItemId"
                element={<ReviewWorkspacePage entryMode="review" />}
              />
            </Routes>
          </MemoryRouter>
        </ReviewRuntimeProvider>
      </QueryClientProvider>,
    );

    expect(await screen.findByText('撤回结果确认中')).toBeInTheDocument();
    void queryClient.invalidateQueries({
      queryKey: reviewKeys.workspace('prj_seed_final_cut', 'item_ep28'),
    });
    await waitFor(() => expect(getWorkspace).toHaveBeenCalledTimes(1));
    expect(
      clearRevokeFinalizationOperation(
        'prj_seed_final_cut',
        'item_ep28',
        operationA.commandId,
      ),
    ).toBe(true);
    const operationB = beginRevokeFinalizationOperation(
      'prj_seed_final_cut',
      'item_ep28',
      3,
    );
    expect(operationB.commandId).not.toBe(operationA.commandId);

    resolveWorkspaceA?.(staleRevokedWorkspace);
    await waitFor(() =>
      expect(
        queryClient.getQueryState(
          reviewKeys.workspace('prj_seed_final_cut', 'item_ep28'),
        )?.fetchStatus,
      ).toBe('idle'),
    );

    expect(
      getRevokeFinalizationProtectionState('prj_seed_final_cut', 'item_ep28'),
    ).toBe('required');
    expect(
      queryClient.getQueryData(
        reviewKeys.workspace('prj_seed_final_cut', 'item_ep28'),
      ),
    ).toMatchObject({
      item: { status: 'finalized' },
      activeFinalization: { finalizationId: expect.any(String) },
    });
    expect(screen.getByText('撤回结果确认中')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '下载单片定稿原片' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '打包项目定稿原片' })).toBeDisabled();
  });

  it('starts authoritative polling immediately when a clicked revoke loses its response', async () => {
    const runtime = createReviewRuntime();
    runtimes.push(runtime);
    const review = runtime.getApi('review');
    const workspace = await review.getWorkspace({
      projectRefId: 'prj_seed_final_cut',
      reviewItemId: 'item_ep28',
    });
    await review.finalizeCurrentVersion(
      {
        projectRefId: workspace.project.projectRefId,
        reviewItemId: workspace.item.reviewItemId,
        versionId: workspace.item.currentVersionId,
        confirmed: true,
      },
      review.entryPolicy.createContext('review'),
    );
    const finalizedWorkspace = await review.getWorkspace({
      projectRefId: 'prj_seed_final_cut',
      reviewItemId: 'item_ep28',
    });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.spyOn(review, 'revokeFinalization').mockImplementation(async () => {
      beginRevokeFinalizationOperation('prj_seed_final_cut', 'item_ep28', 1);
      throw new RevokeFinalizationResultUncertainError(
        new TypeError('response lost after submit'),
      );
    });
    let poll: (() => void) | undefined;
    vi.spyOn(window, 'setInterval').mockImplementation((handler, timeout) => {
      if (timeout === 3_000) poll = handler as () => void;
      return 41 as unknown as ReturnType<typeof window.setInterval>;
    });
    const getReviewItem = vi.spyOn(review, 'getReviewItem');
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    queryClients.push(queryClient);
    queryClient.setQueryData(
      reviewKeys.workspace('prj_seed_final_cut', 'item_ep28'),
      finalizedWorkspace,
    );
    queryClient.setQueryData(reviewKeys.projects, [
      { ...finalizedWorkspace.project, completionStatus: 'completed' as const },
    ]);
    render(
      <QueryClientProvider client={queryClient}>
        <ReviewRuntimeProvider runtime={runtime}>
          <MemoryRouter initialEntries={['/review/projects/prj_seed_final_cut']}>
            <Routes>
              <Route
                path="/review/projects/:projectRefId"
                element={<ProjectDetailPage entryMode="review" />}
              />
            </Routes>
          </MemoryRouter>
        </ReviewRuntimeProvider>
      </QueryClientProvider>,
    );

    await userEvent.click(
      await screen.findByRole('button', { name: '撤销第28集定稿' }),
    );

    expect(await screen.findByText('撤回结果确认中')).toBeInTheDocument();
    await waitFor(() => expect(poll).toBeTypeOf('function'));
    expect(
      getRevokeFinalizationProtectionState('prj_seed_final_cut', 'item_ep28'),
    ).toBe('required');
    expect(
      queryClient.getQueryData(
        reviewKeys.workspace('prj_seed_final_cut', 'item_ep28'),
      ),
    ).toMatchObject({
      item: { status: 'finalized', activeFinalizationId: null },
      activeFinalization: null,
    });
    const uncertainSummary = queryClient.getQueryData<ReviewProjectSummary>(
      reviewKeys.projectSummary('prj_seed_final_cut'),
    );
    expect(
      uncertainSummary?.items.find((item) => item.reviewItemId === 'item_ep28'),
    ).toMatchObject({
      reviewItemId: 'item_ep28',
      status: 'finalized',
      activeFinalizationId: null,
      finalization: { status: 'active' },
      revocationCleanupStatus: 'pending',
      bulkDelete: { eligible: false, locked: true },
    });
    expect(queryClient.getQueryData(reviewKeys.projects)).toMatchObject([
      { completionStatus: 'in_progress' },
    ]);
    const callsBeforePoll = getReviewItem.mock.calls.length;
    act(() => poll?.());
    await waitFor(() =>
      expect(getReviewItem.mock.calls.length).toBeGreaterThan(callsBeforePoll),
    );
  });

  it('bounds authoritative confirmation and exact-operation replays without unlocking an unknown result', async () => {
    const runtime = createReviewRuntime();
    runtimes.push(runtime);
    const review = runtime.getApi('review');
    const workspace = await review.getWorkspace({
      projectRefId: 'prj_seed_final_cut',
      reviewItemId: 'item_ep28',
    });
    await review.finalizeCurrentVersion(
      {
        projectRefId: workspace.project.projectRefId,
        reviewItemId: workspace.item.reviewItemId,
        versionId: workspace.item.currentVersionId,
        confirmed: true,
      },
      review.entryPolicy.createContext('review'),
    );
    beginRevokeFinalizationOperation(
      workspace.project.projectRefId,
      workspace.item.reviewItemId,
      1,
    );
    const revokeFinalization = vi
      .spyOn(review, 'revokeFinalization')
      .mockRejectedValue(
        new RevokeFinalizationResultUncertainError(
          new TypeError('same command response remains unknown'),
        ),
      );

    let poll: (() => void) | undefined;
    vi.spyOn(window, 'setInterval').mockImplementation((handler, timeout) => {
      if (timeout === 3_000) poll = handler as () => void;
      return 42 as unknown as ReturnType<typeof window.setInterval>;
    });
    const getReviewItem = vi.spyOn(review, 'getReviewItem');
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    queryClients.push(queryClient);
    render(
      <QueryClientProvider client={queryClient}>
        <ReviewRuntimeProvider runtime={runtime}>
          <MemoryRouter initialEntries={['/review/projects/prj_seed_final_cut']}>
            <Routes>
              <Route
                path="/review/projects/:projectRefId"
                element={<ProjectDetailPage entryMode="review" />}
              />
            </Routes>
          </MemoryRouter>
        </ReviewRuntimeProvider>
      </QueryClientProvider>,
    );

    expect(await screen.findByText('撤回结果确认中')).toBeInTheDocument();
    await waitFor(() => expect(poll).toBeTypeOf('function'));
    expect(
      getRevokeFinalizationProtectionState('prj_seed_final_cut', 'item_ep28'),
    ).toBe('required');
    await waitFor(() =>
      expect(getReviewItem.mock.calls.length).toBeGreaterThanOrEqual(1),
    );

    for (let attempt = 0; attempt < 3; attempt += 1) {
      const callsBeforePoll = getReviewItem.mock.calls.length;
      act(() => poll?.());
      await waitFor(() =>
        expect(getReviewItem.mock.calls.length).toBeGreaterThan(callsBeforePoll),
      );
    }

    expect(
      await screen.findByText(
        /已完成 4 次权威查询和 2 次同请求安全重试；已停止命令重放，但会持续只读查询/,
      ),
    ).toBeInTheDocument();
    expect(revokeFinalization).toHaveBeenCalledTimes(2);
    const callsAfterReplayLimit = getReviewItem.mock.calls.length;
    act(() => poll?.());
    await waitFor(() =>
      expect(getReviewItem.mock.calls.length).toBeGreaterThan(callsAfterReplayLimit),
    );
    expect(revokeFinalization).toHaveBeenCalledTimes(2);
    expect(
      getRevokeFinalizationProtectionState('prj_seed_final_cut', 'item_ep28'),
    ).toBe('required');
    expect(
      screen.getByRole('button', { name: '撤销第28集定稿' }),
    ).toBeDisabled();
  });

  it('fails closed and only queries authority when the stored operation becomes unavailable', async () => {
    const runtime = createReviewRuntime();
    runtimes.push(runtime);
    const review = runtime.getApi('review');
    const workspace = await review.getWorkspace({
      projectRefId: 'prj_seed_final_cut',
      reviewItemId: 'item_ep28',
    });
    await review.finalizeCurrentVersion(
      {
        projectRefId: workspace.project.projectRefId,
        reviewItemId: workspace.item.reviewItemId,
        versionId: workspace.item.currentVersionId,
        confirmed: true,
      },
      review.entryPolicy.createContext('review'),
    );
    beginRevokeFinalizationOperation(
      workspace.project.projectRefId,
      workspace.item.reviewItemId,
      1,
    );
    const originalGetItem = Storage.prototype.getItem;
    let storageUnavailable = true;
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(function (
      this: Storage,
      key: string,
    ) {
      if (storageUnavailable) {
        throw new DOMException('session storage unavailable', 'SecurityError');
      }
      return originalGetItem.call(this, key);
    });
    const revokeFinalization = vi.spyOn(review, 'revokeFinalization');
    let poll: (() => void) | undefined;
    vi.spyOn(window, 'setInterval').mockImplementation((handler, timeout) => {
      if (timeout === 3_000) poll = handler as () => void;
      return 44 as unknown as ReturnType<typeof window.setInterval>;
    });
    const getReviewItem = vi.spyOn(review, 'getReviewItem');
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    queryClients.push(queryClient);
    render(
      <QueryClientProvider client={queryClient}>
        <ReviewRuntimeProvider runtime={runtime}>
          <MemoryRouter initialEntries={['/review/projects/prj_seed_final_cut']}>
            <Routes>
              <Route
                path="/review/projects/:projectRefId"
                element={<ProjectDetailPage entryMode="review" />}
              />
            </Routes>
          </MemoryRouter>
        </ReviewRuntimeProvider>
      </QueryClientProvider>,
    );

    expect(await screen.findByText('撤回结果确认中')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: '撤销第28集定稿' }),
    ).toBeDisabled();
    await waitFor(() => expect(poll).toBeTypeOf('function'));
    for (let attempt = 0; attempt < 3; attempt += 1) {
      const callsBeforePoll = getReviewItem.mock.calls.length;
      act(() => poll?.());
      await waitFor(() =>
        expect(getReviewItem.mock.calls.length).toBeGreaterThan(callsBeforePoll),
      );
    }

    expect(
      await screen.findByText(
        /会话存储不可用，无法恢复原请求身份；已完成 4 次权威查询，未发送新的撤回命令/,
      ),
    ).toBeInTheDocument();
    expect(revokeFinalization).not.toHaveBeenCalled();
    expect(
      screen.getByRole('button', { name: '撤销第28集定稿' }),
    ).toBeDisabled();
    window.sessionStorage.clear();
    storageUnavailable = false;
    const callsBeforeRecoveredPoll = getReviewItem.mock.calls.length;
    act(() => poll?.());
    await waitFor(() =>
      expect(getReviewItem.mock.calls.length).toBeGreaterThan(
        callsBeforeRecoveredPoll,
      ),
    );
    expect(revokeFinalization).not.toHaveBeenCalled();
    expect(
      screen.getByRole('button', { name: '撤销第28集定稿' }),
    ).toBeDisabled();
  });

  it('fails closed on direct entry when the persisted operation payload is malformed', async () => {
    const runtime = createReviewRuntime();
    runtimes.push(runtime);
    const review = runtime.getApi('review');
    const workspace = await review.getWorkspace({
      projectRefId: 'prj_seed_final_cut',
      reviewItemId: 'item_ep28',
    });
    await review.finalizeCurrentVersion(
      {
        projectRefId: workspace.project.projectRefId,
        reviewItemId: workspace.item.reviewItemId,
        versionId: workspace.item.currentVersionId,
        confirmed: true,
      },
      review.entryPolicy.createContext('review'),
    );
    clearRevokeFinalizationOperation('prj_seed_final_cut', 'item_ep28');
    window.sessionStorage.setItem(
      'fj-final-cut-review:revoke-finalization-operation:prj_seed_final_cut:item_ep28',
      '{not-json',
    );
    const getReviewItem = vi.spyOn(review, 'getReviewItem');
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    queryClients.push(queryClient);
    render(
      <QueryClientProvider client={queryClient}>
        <ReviewRuntimeProvider runtime={runtime}>
          <MemoryRouter
            initialEntries={[
              '/review/projects/prj_seed_final_cut/items/item_ep28',
            ]}
          >
            <Routes>
              <Route
                path="/review/projects/:projectRefId/items/:reviewItemId"
                element={<ReviewWorkspacePage entryMode="review" />}
              />
            </Routes>
          </MemoryRouter>
        </ReviewRuntimeProvider>
      </QueryClientProvider>,
    );

    expect(await screen.findByText('撤回结果确认中')).toBeInTheDocument();
    expect(
      getRevokeFinalizationProtectionState('prj_seed_final_cut', 'item_ep28'),
    ).toBe('storage-unavailable');
    expect(screen.getByRole('button', { name: '下载单片定稿原片' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '打包项目定稿原片' })).toBeDisabled();
    await waitFor(() => expect(getReviewItem).toHaveBeenCalled());
  });

  it('fails closed on direct entry when session storage reads throw without in-memory state', async () => {
    const runtime = createReviewRuntime();
    runtimes.push(runtime);
    const review = runtime.getApi('review');
    const workspace = await review.getWorkspace({
      projectRefId: 'prj_seed_final_cut',
      reviewItemId: 'item_ep28',
    });
    await review.finalizeCurrentVersion(
      {
        projectRefId: workspace.project.projectRefId,
        reviewItemId: workspace.item.reviewItemId,
        versionId: workspace.item.currentVersionId,
        confirmed: true,
      },
      review.entryPolicy.createContext('review'),
    );
    clearRevokeFinalizationOperation('prj_seed_final_cut', 'item_ep28');
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('session storage read unavailable', 'SecurityError');
    });
    const getReviewItem = vi.spyOn(review, 'getReviewItem');
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    queryClients.push(queryClient);
    render(
      <QueryClientProvider client={queryClient}>
        <ReviewRuntimeProvider runtime={runtime}>
          <MemoryRouter
            initialEntries={[
              '/review/projects/prj_seed_final_cut/items/item_ep28',
            ]}
          >
            <Routes>
              <Route
                path="/review/projects/:projectRefId/items/:reviewItemId"
                element={<ReviewWorkspacePage entryMode="review" />}
              />
            </Routes>
          </MemoryRouter>
        </ReviewRuntimeProvider>
      </QueryClientProvider>,
    );

    expect(await screen.findByText('撤回结果确认中')).toBeInTheDocument();
    expect(
      getRevokeFinalizationProtectionState('prj_seed_final_cut', 'item_ep28'),
    ).toBe('storage-unavailable');
    expect(screen.getByRole('button', { name: '下载单片定稿原片' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '打包项目定稿原片' })).toBeDisabled();
    await waitFor(() => expect(getReviewItem).toHaveBeenCalled());
  });

  it('unlocks only after the exact retry is authoritatively rejected as not executed', async () => {
    const runtime = createReviewRuntime();
    runtimes.push(runtime);
    const review = runtime.getApi('review');
    const workspace = await review.getWorkspace({
      projectRefId: 'prj_seed_final_cut',
      reviewItemId: 'item_ep28',
    });
    await review.finalizeCurrentVersion(
      {
        projectRefId: workspace.project.projectRefId,
        reviewItemId: workspace.item.reviewItemId,
        versionId: workspace.item.currentVersionId,
        confirmed: true,
      },
      review.entryPolicy.createContext('review'),
    );
    beginRevokeFinalizationOperation(
      workspace.project.projectRefId,
      workspace.item.reviewItemId,
      1,
    );
    const revokeFinalization = vi
      .spyOn(review, 'revokeFinalization')
      .mockImplementation(async () => {
        clearRevokeFinalizationOperation('prj_seed_final_cut', 'item_ep28');
        throw new Error('权威服务确认原撤回请求未执行');
      });

    let poll: (() => void) | undefined;
    vi.spyOn(window, 'setInterval').mockImplementation((handler, timeout) => {
      if (timeout === 3_000) poll = handler as () => void;
      return 43 as unknown as ReturnType<typeof window.setInterval>;
    });
    const getReviewItem = vi.spyOn(review, 'getReviewItem');
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    queryClients.push(queryClient);
    render(
      <QueryClientProvider client={queryClient}>
        <ReviewRuntimeProvider runtime={runtime}>
          <MemoryRouter initialEntries={['/review/projects/prj_seed_final_cut']}>
            <Routes>
              <Route
                path="/review/projects/:projectRefId"
                element={<ProjectDetailPage entryMode="review" />}
              />
            </Routes>
          </MemoryRouter>
        </ReviewRuntimeProvider>
      </QueryClientProvider>,
    );

    expect(await screen.findByText('撤回结果确认中')).toBeInTheDocument();
    await waitFor(() => expect(poll).toBeTypeOf('function'));
    await waitFor(() =>
      expect(getReviewItem.mock.calls.length).toBeGreaterThanOrEqual(1),
    );
    const callsBeforePoll = getReviewItem.mock.calls.length;
    act(() => poll?.());
    await waitFor(() =>
      expect(getReviewItem.mock.calls.length).toBeGreaterThan(callsBeforePoll),
    );

    expect(
      await screen.findByText(/撤回未执行：权威服务确认原撤回请求未执行/),
    ).toBeInTheDocument();
    expect(revokeFinalization).toHaveBeenCalledTimes(1);
    await waitFor(() =>
      expect(
        getRevokeFinalizationProtectionState('prj_seed_final_cut', 'item_ep28'),
      ).toBe('clear'),
    );
    expect(
      screen.getByRole('button', { name: '撤销第28集定稿' }),
    ).toBeEnabled();
  });

  it('keeps the authoritative in-review state when the summary refresh fails after revocation', async () => {
    const runtime = createReviewRuntime();
    runtimes.push(runtime);
    const review = runtime.getApi('review');
    const workspace = await review.getWorkspace({
      projectRefId: 'prj_seed_final_cut',
      reviewItemId: 'item_ep28',
    });
    await review.finalizeCurrentVersion(
      {
        projectRefId: workspace.project.projectRefId,
        reviewItemId: workspace.item.reviewItemId,
        versionId: workspace.item.currentVersionId,
        confirmed: true,
      },
      review.entryPolicy.createContext('review'),
    );
    const finalizedWorkspace = await review.getWorkspace({
      projectRefId: 'prj_seed_final_cut',
      reviewItemId: 'item_ep28',
    });
    const staleSummary = await review.getProjectSummary('prj_seed_final_cut');
    beginRevokeFinalizationOperation('prj_seed_final_cut', 'item_ep28', 1);
    await review.revokeFinalization(
      {
        projectRefId: 'prj_seed_final_cut',
        reviewItemId: 'item_ep28',
        confirmed: true,
      },
      review.entryPolicy.createContext('review'),
    );
    const getProjectSummary = vi.spyOn(review, 'getProjectSummary')
      .mockResolvedValueOnce(staleSummary)
      .mockRejectedValue(new Error('summary unavailable'));
    const getReviewItem = vi.spyOn(review, 'getReviewItem');
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    queryClients.push(queryClient);
    queryClient.setQueryData(
      reviewKeys.workspace('prj_seed_final_cut', 'item_ep28'),
      finalizedWorkspace,
    );
    queryClient.setQueryData(reviewKeys.projects, [
      { ...staleSummary.project, completionStatus: 'completed' as const },
    ]);
    render(
      <QueryClientProvider client={queryClient}>
        <ReviewRuntimeProvider runtime={runtime}>
          <MemoryRouter initialEntries={['/review/projects/prj_seed_final_cut']}>
            <Routes>
              <Route
                path="/review/projects/:projectRefId"
                element={<ProjectDetailPage entryMode="review" />}
              />
            </Routes>
          </MemoryRouter>
        </ReviewRuntimeProvider>
      </QueryClientProvider>,
    );

    await waitFor(() => expect(getReviewItem).toHaveBeenCalled());
    await waitFor(() => expect(getProjectSummary.mock.calls.length).toBeGreaterThan(1));
    await waitFor(() =>
      expect(
        screen.queryByRole('button', { name: '撤销第28集定稿' }),
      ).not.toBeInTheDocument(),
    );
    expect(screen.getAllByText('审阅中').length).toBeGreaterThan(0);
    const cachedSummary = queryClient.getQueryData(
      reviewKeys.projectSummary('prj_seed_final_cut'),
    );
    expect(cachedSummary).toMatchObject({
      items: [
        expect.objectContaining({
          reviewItemId: 'item_ep28',
          status: 'in_review',
          revocationCleanupStatus: 'pending',
        }),
      ],
    });
    expect(
      queryClient.getQueryData(
        reviewKeys.workspace('prj_seed_final_cut', 'item_ep28'),
      ),
    ).toMatchObject({
      item: { status: 'in_review', activeFinalizationId: null },
      currentVersion: { status: 'in_review' },
      activeFinalization: null,
    });
    expect(queryClient.getQueryData(reviewKeys.projects)).toMatchObject([
      { completionStatus: 'in_progress' },
    ]);
    expect(
      getRevokeFinalizationProtectionState('prj_seed_final_cut', 'item_ep28'),
    ).toBe('clear');
  });

  it('aborts the authoritative item query when the page unmounts', async () => {
    const runtime = createReviewRuntime();
    runtimes.push(runtime);
    const review = runtime.getApi('review');
    const workspace = await review.getWorkspace({
      projectRefId: 'prj_seed_final_cut',
      reviewItemId: 'item_ep28',
    });
    await review.finalizeCurrentVersion(
      {
        projectRefId: workspace.project.projectRefId,
        reviewItemId: workspace.item.reviewItemId,
        versionId: workspace.item.currentVersionId,
        confirmed: true,
      },
      review.entryPolicy.createContext('review'),
    );
    beginRevokeFinalizationOperation('prj_seed_final_cut', 'item_ep28', 1);
    let authoritySignal: AbortSignal | undefined;
    vi.spyOn(review, 'getReviewItem').mockImplementation((_input, options) => {
      authoritySignal = options?.signal;
      return new Promise((_resolve, reject) => {
        authoritySignal?.addEventListener('abort', () => {
          reject(new DOMException('aborted', 'AbortError'));
        });
      });
    });
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    queryClients.push(queryClient);
    const rendered = render(
      <QueryClientProvider client={queryClient}>
        <ReviewRuntimeProvider runtime={runtime}>
          <MemoryRouter initialEntries={['/review/projects/prj_seed_final_cut']}>
            <Routes>
              <Route
                path="/review/projects/:projectRefId"
                element={<ProjectDetailPage entryMode="review" />}
              />
            </Routes>
          </MemoryRouter>
        </ReviewRuntimeProvider>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(authoritySignal).toBeDefined());
    rendered.unmount();
    expect(authoritySignal?.aborted).toBe(true);
  });
});
