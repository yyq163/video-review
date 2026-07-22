import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import type { EntryMode } from '../contracts/types';
import {
  useProjectDetail,
  useReviewMutations,
  useWorkspace,
} from '../entry/use-review-queries';
import { ProjectDetailPage } from './ProjectDetailPage';
import { ReviewWorkspacePage } from './ReviewWorkspacePage';

vi.mock('../entry/use-review-queries', () => ({
  useProjectDetail: vi.fn(),
  useReviewMutations: vi.fn(),
  useWorkspace: vi.fn(),
}));

const projectDetailQuery = vi.mocked(useProjectDetail);
const reviewMutations = vi.mocked(useReviewMutations);
const workspaceQuery = vi.mocked(useWorkspace);

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function expectApprovedEntryNavigation(entryMode: EntryMode) {
  const navigation = screen.getByRole('navigation', { name: '入口切换' });
  const editEntry = within(navigation).getByRole('link', { name: '剪辑入口' });
  const reviewEntry = within(navigation).queryByRole('link', { name: '成片审阅' });

  expect(within(navigation).getAllByRole('link')).toHaveLength(entryMode === 'edit' ? 1 : 2);
  if (entryMode === 'edit') {
    expect(editEntry).toHaveAttribute('aria-current', 'page');
  } else {
    expect(editEntry).not.toHaveAttribute('aria-current');
  }
  if (entryMode === 'edit') {
    expect(reviewEntry).not.toBeInTheDocument();
  } else {
    expect(reviewEntry).toHaveAttribute('aria-current', 'page');
  }
}

function renderProjectDetail(entryMode: EntryMode) {
  render(
    <MemoryRouter initialEntries={[`/${entryMode}/projects/prj_state_test`]}>
      <Routes>
        <Route
          path={`/${entryMode}/projects/:projectRefId`}
          element={<ProjectDetailPage entryMode={entryMode} />}
        />
      </Routes>
    </MemoryRouter>,
  );
}

function renderWorkspace(entryMode: EntryMode) {
  render(
    <MemoryRouter initialEntries={[`/${entryMode}/projects/prj_state_test/items/item_state_test`]}>
      <Routes>
        <Route
          path={`/${entryMode}/projects/:projectRefId/items/:reviewItemId`}
          element={<ReviewWorkspacePage entryMode={entryMode} />}
        />
      </Routes>
    </MemoryRouter>,
  );
}

describe.each(['edit', 'review'] satisfies EntryMode[])('%s entry navigation in query boundary states', (entryMode) => {
  it('keeps the approved links while project detail is loading', () => {
    reviewMutations.mockReturnValue({} as ReturnType<typeof useReviewMutations>);
    projectDetailQuery.mockReturnValue({ isLoading: true } as ReturnType<typeof useProjectDetail>);

    renderProjectDetail(entryMode);

    expect(screen.getByRole('status')).toHaveTextContent('加载中');
    expectApprovedEntryNavigation(entryMode);
  });

  it('keeps the approved links when project detail fails', () => {
    reviewMutations.mockReturnValue({} as ReturnType<typeof useReviewMutations>);
    projectDetailQuery.mockReturnValue({
      isLoading: false,
      data: undefined,
      error: new Error('project detail unavailable'),
    } as ReturnType<typeof useProjectDetail>);

    renderProjectDetail(entryMode);

    expect(screen.getByText('project detail unavailable')).toBeInTheDocument();
    expectApprovedEntryNavigation(entryMode);
  });

  it('keeps the approved links while the workspace is loading', () => {
    workspaceQuery.mockReturnValue({ isLoading: true } as ReturnType<typeof useWorkspace>);

    renderWorkspace(entryMode);

    expect(screen.getByRole('status')).toHaveTextContent('载入审阅工作台');
    expectApprovedEntryNavigation(entryMode);
  });

  it('keeps the approved links when the workspace fails', () => {
    workspaceQuery.mockReturnValue({
      isLoading: false,
      data: undefined,
      error: new Error('workspace unavailable'),
    } as ReturnType<typeof useWorkspace>);

    renderWorkspace(entryMode);

    expect(screen.getByText('workspace unavailable')).toBeInTheDocument();
    expectApprovedEntryNavigation(entryMode);
  });
});
