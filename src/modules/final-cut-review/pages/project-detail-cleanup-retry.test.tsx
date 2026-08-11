import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import {
  ReviewRuntimeProvider,
  createReviewRuntime,
  type ReviewRuntime,
} from '../entry/runtime';
import type { ReviewProjectSummary } from '../ports';
import { ProjectDetailPage } from './ProjectDetailPage';

const runtimes: ReviewRuntime[] = [];
const queryClients: QueryClient[] = [];

afterEach(() => {
  cleanup();
  for (const queryClient of queryClients.splice(0)) queryClient.clear();
  for (const runtime of runtimes.splice(0)) runtime.dispose();
  vi.restoreAllMocks();
});

async function cleanupFailedSummary(runtime: ReviewRuntime): Promise<ReviewProjectSummary> {
  const review = runtime.getApi('review');
  const summary = await review.getProjectSummary('prj_seed_final_cut');
  return {
    ...summary,
    items: summary.items.map((item) =>
      item.reviewItemId === 'item_ep28'
        ? {
            ...item,
            status: 'in_review' as const,
            activeFinalizationId: null,
            finalization: {
              id: 'fin-cleanup-retry',
              status: 'revoked' as const,
              revokedAt: '2026-08-11T00:00:00.000Z',
            },
            revocationCleanupStatus: 'failed' as const,
            bulkDelete: {
              eligible: false,
              locked: true,
              reason: 'CLEANUP_FAILED',
            },
          }
        : item,
    ),
  };
}

function renderProject(runtime: ReviewRuntime) {
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
}

describe('ProjectDetailPage finalization cleanup retry', () => {
  it.each(['pending', 'complete'] as const)(
    'passes the summary lock and waits for an authoritative %s refresh',
    async (nextStatus) => {
      const runtime = createReviewRuntime();
      runtimes.push(runtime);
      const review = runtime.getApi('review');
      const failedSummary = await cleanupFailedSummary(runtime);
      let cleanupStatus: 'failed' | 'pending' | 'complete' = 'failed';
      const getProjectSummary = vi
        .spyOn(review, 'getProjectSummary')
        .mockImplementation(async () => ({
          ...failedSummary,
          items: failedSummary.items.map((item) =>
            item.reviewItemId === 'item_ep28'
              ? { ...item, revocationCleanupStatus: cleanupStatus }
              : item,
          ),
        }));
      const retry = vi.spyOn(review, 'retryFinalizationCleanup').mockImplementation(async () => {
        cleanupStatus = nextStatus;
        return {
          projectRefId: 'prj_seed_final_cut',
          reviewItemId: 'item_ep28',
          cleanupStatus: nextStatus,
          requeuedPackageIds: ['package-cleanup-retry'],
        };
      });
      renderProject(runtime);

      const button = await screen.findByRole('button', {
        name: '重试清理第28集关联包',
      });
      await userEvent.click(button);

      await waitFor(() => expect(retry).toHaveBeenCalledWith({
        projectRefId: 'prj_seed_final_cut',
        reviewItemId: 'item_ep28',
        lockVersion: 1,
      }, expect.any(Object)));
      if (nextStatus === 'pending') {
        expect(await screen.findByText('定稿已撤回，关联包清理中')).toBeVisible();
      } else {
        await waitFor(() => {
          expect(screen.queryByText('定稿已撤回，关联包清理失败。')).not.toBeInTheDocument();
        });
        expect(screen.queryByText('定稿已撤回，关联包清理中')).not.toBeInTheDocument();
      }
      expect(screen.queryByRole('button', { name: /重试清理第28集关联包/ })).not.toBeInTheDocument();
      expect(getProjectSummary.mock.calls.length).toBeGreaterThanOrEqual(2);
    },
  );

  it('keeps the authoritative failure warning and safe retry error after rejection', async () => {
    const runtime = createReviewRuntime();
    runtimes.push(runtime);
    const review = runtime.getApi('review');
    const failedSummary = await cleanupFailedSummary(runtime);
    vi.spyOn(review, 'getProjectSummary').mockResolvedValue(failedSummary);
    vi.spyOn(review, 'retryFinalizationCleanup').mockRejectedValue(
      new Error('raw transport failure'),
    );
    renderProject(runtime);

    await userEvent.click(await screen.findByRole('button', {
      name: '重试清理第28集关联包',
    }));

    expect(await screen.findByText('清理重试失败，请稍后再试；当前失败状态已保留。')).toBeVisible();
    expect(screen.getByText('定稿已撤回，关联包清理失败。')).toBeVisible();
    expect(screen.getByRole('button', { name: '重试清理第28集关联包' })).toBeEnabled();
    expect(screen.queryByText('raw transport failure')).not.toBeInTheDocument();
  });
});
