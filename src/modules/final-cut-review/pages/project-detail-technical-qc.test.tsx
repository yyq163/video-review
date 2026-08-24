import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, describe, expect, it } from 'vitest';
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
          episodes: [
            { episodeNo: 2, processedMs: 400, totalMs: 1_000 },
            { episodeNo: 1, processedMs: 300, totalMs: 1_000 },
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
});
