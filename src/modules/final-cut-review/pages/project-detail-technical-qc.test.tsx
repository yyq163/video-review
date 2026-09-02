import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createReviewRuntime, ReviewRuntimeProvider, type ReviewRuntime } from '../entry/runtime';
import { ProjectDetailPage } from './ProjectDetailPage';

let runtime: ReviewRuntime | null = null;

afterEach(() => {
  runtime?.dispose();
  runtime = null;
});

describe('ProjectDetailPage technical QC progress', () => {
  it('places the project-scoped running marker immediately after the existing in-progress badge', async () => {
    runtime = createReviewRuntime({
      technicalQcProgressByProject: {
        prj_seed_final_cut: {
          activeTaskCount: 2,
          episodes: [
            { reviewItemId: 'item-2', episodeNo: 2, processedMs: 400, totalMs: 1_000 },
            { reviewItemId: 'item-1', episodeNo: 1, processedMs: 300, totalMs: 1_000 },
          ],
        },
      },
    });
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
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

    const heading = await screen.findByRole('heading', { level: 1 });
    const businessStatus = within(heading).getByText('进行中');
    const technicalStatus = await within(heading).findByRole('status', {
      name: '技术审查中',
    });
    expect(
      Boolean(
        businessStatus.compareDocumentPosition(technicalStatus)
        & Node.DOCUMENT_POSITION_FOLLOWING,
      ),
    ).toBe(true);

    await userEvent.hover(technicalStatus);
    await waitFor(() => {
      expect(screen.getByRole('tooltip')).toHaveTextContent('第 1 集');
      expect(screen.getByRole('tooltip')).toHaveTextContent('第 2 集');
    });
    expect(screen.getByRole('tooltip')).not.toHaveTextContent('项目总体进度');
  });

  it('places the edit-only rerun action immediately before return and submits once while pending', async () => {
    runtime = createReviewRuntime({
      technicalQcProgressByProject: {
        prj_seed_final_cut: {
          activeTaskCount: 0,
          episodes: [],
        },
      },
    });
    let resolveRerun: ((value: {
      projectRefId: string;
      status: 'queued';
      queuedItemCount: number;
    }) => void) | undefined;
    const rerun = vi.fn(() => new Promise((resolve) => {
      resolveRerun = resolve;
    }));
    Object.assign(runtime.getApi('edit'), {
      rerunProjectTechnicalQc: rerun,
    });
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
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

    const rerunButton = await screen.findByRole('button', {
      name: '重新进行技术审查',
    });
    const returnLink = screen.getByRole('link', { name: '返回项目' });
    expect(
      Boolean(
        rerunButton.compareDocumentPosition(returnLink)
        & Node.DOCUMENT_POSITION_FOLLOWING,
      ),
    ).toBe(true);

    await userEvent.click(rerunButton);
    await userEvent.click(rerunButton);
    expect(rerunButton).toBeDisabled();
    expect(rerun).toHaveBeenCalledOnce();
    expect(rerun).toHaveBeenCalledWith(
      { projectRefId: 'prj_seed_final_cut' },
      expect.objectContaining({ entryMode: 'edit' }),
    );

    resolveRerun?.({
      projectRefId: 'prj_seed_final_cut',
      status: 'queued',
      queuedItemCount: 12,
    });
    await waitFor(() => {
      expect(screen.getByText('已提交 12 集技术审查。')).toBeInTheDocument();
    });
  });

  it('hides the rerun action outside edit entry and disables it while tasks are active', async () => {
    runtime = createReviewRuntime({
      technicalQcProgressByProject: {
        prj_seed_final_cut: {
          activeTaskCount: 1,
          episodes: [],
        },
      },
    });
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const { unmount } = render(
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
    expect(
      await screen.findByRole('button', { name: '重新进行技术审查' }),
    ).toBeDisabled();
    unmount();

    const reviewQueryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={reviewQueryClient}>
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
    await screen.findByRole('heading', { level: 1 });
    expect(
      screen.queryByRole('button', { name: '重新进行技术审查' }),
    ).not.toBeInTheDocument();
  });

  it('shows the command failure and restores the rerun action', async () => {
    runtime = createReviewRuntime({
      technicalQcProgressByProject: {
        prj_seed_final_cut: {
          activeTaskCount: 0,
          episodes: [],
        },
      },
    });
    Object.assign(runtime.getApi('edit'), {
      rerunProjectTechnicalQc: vi.fn().mockRejectedValue(
        new Error('技术审查重新提交失败'),
      ),
    });
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
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

    const button = await screen.findByRole('button', {
      name: '重新进行技术审查',
    });
    await userEvent.click(button);

    expect(
      await screen.findByText('技术审查重新提交失败'),
    ).toBeInTheDocument();
    expect(button).toBeEnabled();
  });
});
