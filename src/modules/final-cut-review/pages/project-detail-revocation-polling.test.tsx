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
import { ProjectDetailPage } from './ProjectDetailPage';

const runtimes: ReviewRuntime[] = [];
const queryClients: QueryClient[] = [];

afterEach(() => {
  for (const runtime of runtimes.splice(0)) runtime.dispose();
  for (const queryClient of queryClients.splice(0)) queryClient.clear();
  window.sessionStorage.clear();
  vi.restoreAllMocks();
});

describe('ProjectDetailPage uncertain finalization revocation reconciliation', () => {
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
    const getProjectSummary = vi.spyOn(review, 'getProjectSummary');
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

    await userEvent.click(
      await screen.findByRole('button', { name: '撤销第28集定稿' }),
    );

    expect(await screen.findByText('撤回结果确认中')).toBeInTheDocument();
    await waitFor(() => expect(poll).toBeTypeOf('function'));
    expect(
      getRevokeFinalizationProtectionState('prj_seed_final_cut', 'item_ep28'),
    ).toBe('required');
    const callsBeforePoll = getProjectSummary.mock.calls.length;
    act(() => poll?.());
    await waitFor(() =>
      expect(getProjectSummary.mock.calls.length).toBeGreaterThan(callsBeforePoll),
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
    const getProjectSummary = vi.spyOn(review, 'getProjectSummary');
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
      expect(getProjectSummary.mock.calls.length).toBeGreaterThanOrEqual(2),
    );

    for (let attempt = 0; attempt < 3; attempt += 1) {
      const callsBeforePoll = getProjectSummary.mock.calls.length;
      act(() => poll?.());
      await waitFor(() =>
        expect(getProjectSummary.mock.calls.length).toBeGreaterThan(callsBeforePoll),
      );
    }

    expect(
      await screen.findByText(
        /已完成 4 次权威查询和 2 次同请求安全重试，自动确认已暂停/,
      ),
    ).toBeInTheDocument();
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
    const getProjectSummary = vi.spyOn(review, 'getProjectSummary');
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
      const callsBeforePoll = getProjectSummary.mock.calls.length;
      act(() => poll?.());
      await waitFor(() =>
        expect(getProjectSummary.mock.calls.length).toBeGreaterThan(callsBeforePoll),
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
    const callsBeforeRecoveredPoll = getProjectSummary.mock.calls.length;
    act(() => poll?.());
    await waitFor(() =>
      expect(getProjectSummary.mock.calls.length).toBeGreaterThan(
        callsBeforeRecoveredPoll,
      ),
    );
    expect(revokeFinalization).not.toHaveBeenCalled();
    expect(
      screen.getByRole('button', { name: '撤销第28集定稿' }),
    ).toBeDisabled();
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
    const getProjectSummary = vi.spyOn(review, 'getProjectSummary');
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
      expect(getProjectSummary.mock.calls.length).toBeGreaterThanOrEqual(2),
    );
    const callsBeforePoll = getProjectSummary.mock.calls.length;
    act(() => poll?.());
    await waitFor(() =>
      expect(getProjectSummary.mock.calls.length).toBeGreaterThan(callsBeforePoll),
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
});
