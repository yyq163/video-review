import { afterEach, describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import type { ReviewItemDTO, UploadSessionDTO } from '../contracts-generated/backend-contract';
import { ReviewRuntimeProvider, createReviewRuntime, type ReviewRuntime } from '../entry/runtime';
import { HttpReviewUploads, isAppendVersionConfirmationRequired } from '../adapters/http-review-uploads';
import type { HttpReviewTransport } from '../adapters/http-review-transport';
import { ReviewWorkspacePage } from './ReviewWorkspacePage';

const PROJECT_REF_ID = 'prj_seed_final_cut';
const REVIEW_ITEM_ID = 'item_ep28';
const CURRENT_VERSION_ID = 'ver_ep28_v2';

afterEach(() => {
  window.sessionStorage.clear();
  vi.restoreAllMocks();
});

describe('review workspace append-version confirmation', () => {
  it('blocks after response loss and remount until an explicit version-list refetch succeeds', async () => {
    const runtime = createReviewRuntime();
    const reviewApi = runtime.getApi('review');
    await reviewApi.requestChanges(
      { projectRefId: PROJECT_REF_ID, reviewItemId: REVIEW_ITEM_ID, versionId: CURRENT_VERSION_ID },
      reviewApi.entryPolicy.createContext('review'),
    );

    const first = renderEditWorkspace(runtime);
    expect(await screen.findByRole('button', { name: '确认追加 V3' })).toBeEnabled();
    first.unmount();
    first.queryClient.clear();

    const selectedFile = new File(['v3'], 'must-not-persist-v3.mp4', {
      type: 'video/mp4',
      lastModified: 3,
    });
    const item = reviewItem();
    const transport = {
      assertWriteContext: vi.fn(),
      projectForWrite: vi.fn().mockResolvedValue(undefined),
      requestJson: vi.fn().mockResolvedValue(item),
      command: vi.fn().mockRejectedValue(new TypeError('append response lost after commit')),
      baseUrl: 'https://review.example',
    } as unknown as HttpReviewTransport;
    const upload = vi.fn().mockResolvedValue({
      upload_id: 'upl_v3_lost',
      file_id: 'file_v3_lost',
      status: 'completed',
      original_filename: selectedFile.name,
      mime_type: selectedFile.type,
      declared_size: selectedFile.size,
      received_size: selectedFile.size,
    } satisfies UploadSessionDTO);
    const uploads = new HttpReviewUploads(transport, upload);

    await expect(uploads.appendVersion({
      projectRefId: PROJECT_REF_ID,
      reviewItemId: REVIEW_ITEM_ID,
      file: selectedFile,
      versionNote: '不得持久化的 V3 说明',
      changeSummary: '不得持久化的 V3 payload',
      supersedeReason: '',
    }, reviewApi.entryPolicy.createContext('edit'))).rejects.toThrow('append response lost after commit');
    expect(isAppendVersionConfirmationRequired(PROJECT_REF_ID, REVIEW_ITEM_ID)).toBe(true);

    const editApi = runtime.getApi('edit');
    const originalGetWorkspace = editApi.getWorkspace.bind(editApi);
    const getWorkspace = vi.spyOn(editApi, 'getWorkspace').mockImplementation((...args) => originalGetWorkspace(...args));
    const reloaded = renderEditWorkspace(runtime);

    expect(await screen.findByTestId('append-version-confirmation-required')).toHaveTextContent(
      '请先确认上一笔版本追加结果',
    );
    expect(screen.queryByTestId('append-version-panel')).not.toBeInTheDocument();
    const callsBeforeConfirmation = getWorkspace.mock.calls.length;

    await userEvent.click(screen.getByRole('button', { name: '我已核对版本列表，刷新后允许继续追加' }));
    await waitFor(() => expect(getWorkspace.mock.calls.length).toBeGreaterThan(callsBeforeConfirmation));
    await waitFor(() => expect(screen.queryByTestId('append-version-confirmation-required')).not.toBeInTheDocument());
    expect(screen.getByRole('button', { name: '确认追加 V3' })).toBeEnabled();
    expect(isAppendVersionConfirmationRequired(PROJECT_REF_ID, REVIEW_ITEM_ID)).toBe(false);

    reloaded.unmount();
    reloaded.queryClient.clear();
    runtime.dispose();
  });
});

function renderEditWorkspace(runtime: ReviewRuntime) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  const result = render(
    <QueryClientProvider client={queryClient}>
      <ReviewRuntimeProvider runtime={runtime}>
        <MemoryRouter initialEntries={[`/edit/projects/${PROJECT_REF_ID}/items/${REVIEW_ITEM_ID}`]}>
          <Routes>
            <Route
              path="/edit/projects/:projectRefId/items/:reviewItemId"
              element={<ReviewWorkspacePage entryMode="edit" />}
            />
          </Routes>
        </MemoryRouter>
      </ReviewRuntimeProvider>
    </QueryClientProvider>,
  );
  return { ...result, queryClient };
}

function reviewItem(): ReviewItemDTO {
  return {
    id: REVIEW_ITEM_ID,
    project_ref_id: PROJECT_REF_ID,
    item_code: '28',
    episode_no: 28,
    title: '第 28 集 · 最终成片',
    workflow_status: 'changes_requested',
    current_version_id: CURRENT_VERSION_ID,
    current_version_no: 2,
    ui_status: '待修改',
    active_finalization_id: null,
    unresolved_current_version_count: 3,
    resolved_current_version_count: 0,
    historical_version_count: 1,
    is_finalized: false,
    lock_version: 9,
    created_at: '2026-07-14T00:00:00.000Z',
    updated_at: '2026-07-14T00:00:00.000Z',
  };
}
