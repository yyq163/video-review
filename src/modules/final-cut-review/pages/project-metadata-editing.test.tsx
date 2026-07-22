import { afterEach, describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { ReviewRuntimeProvider, createReviewRuntime } from '../entry/runtime';
import { ReviewItemMetadataEditor } from '../components/MetadataEditors';
import { uploadSchema } from '../components/ProjectForms';
import { ProjectDetailPage } from './ProjectDetailPage';

function renderDetail(
  mode: 'edit' | 'review',
  onRuntime?: (runtime: ReturnType<typeof createReviewRuntime>) => void,
) {
  const runtime = createReviewRuntime();
  onRuntime?.(runtime);
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const route = `/${mode}/projects/prj_seed_final_cut`;
  render(
    <QueryClientProvider client={queryClient}>
      <ReviewRuntimeProvider runtime={runtime}>
        <MemoryRouter initialEntries={[route]}>
          <Routes>
            <Route path={`/${mode}/projects/:projectRefId`} element={<ProjectDetailPage entryMode={mode} />} />
            <Route path={`/${mode}/projects`} element={<div>项目列表</div>} />
          </Routes>
        </MemoryRouter>
      </ReviewRuntimeProvider>
    </QueryClientProvider>,
  );
  return () => {
    runtime.dispose();
    queryClient.clear();
  };
}

describe('project and review item metadata editing', () => {
  it('requires a create title while allowing a nonnumeric immutable item code', () => {
    const file = new File(['video'], 'item-code.mp4', { type: 'video/mp4' });
    expect(uploadSchema.safeParse({ title: '   ', episode: 'CUT-A', file }).success).toBe(false);
    expect(uploadSchema.safeParse({ title: 'Cut A', episode: 'CUT-A', file }).success).toBe(true);
    expect(uploadSchema.safeParse({ title: 'Cut zero', episode: '0', file }).success).toBe(false);
    expect(uploadSchema.safeParse({ title: 'Cut negative', episode: '-1', file }).success).toBe(false);
    expect(uploadSchema.safeParse({ title: 'Cut fraction', episode: '1.5', file }).success).toBe(false);
  });

  it('rejects a blank create title before invoking the create mutation', async () => {
    let runtime: ReturnType<typeof createReviewRuntime> | undefined;
    const dispose = renderDetail('edit', (createdRuntime) => {
      runtime = createdRuntime;
    });
    const createItem = vi.spyOn(runtime!.getApi('edit'), 'createReviewItemWithVersion');
    await userEvent.upload(
      await screen.findByTestId('create-item-file'),
      new File(['video'], 'blank-title.mp4', { type: 'video/mp4' }),
    );
    const title = await screen.findByLabelText('成片标题');
    await userEvent.clear(title);
    await userEvent.type(title, '   ');

    expect(screen.getByRole('button', { name: '上传 V1' })).toBeDisabled();
    expect(createItem).not.toHaveBeenCalled();
    dispose();
  });

  afterEach(() => {
    cleanup();
    window.localStorage.clear();
    window.sessionStorage.clear();
  });

  it('keeps compact metadata triggers in the edit title and item row without standalone panels', async () => {
    const disposeEdit = renderDetail('edit');
    const heading = await screen.findByRole('heading', { name: /真千金是男的/ });
    expect(within(heading.parentElement!).getByRole('button', { name: '编辑项目资料' })).toBeInTheDocument();
    const itemTrigger = screen.getByRole('button', { name: /编辑成片元数据.*第 28 集/ });
    expect(itemTrigger.closest('.fj-review-item-row')).not.toBeNull();
    expect(screen.queryByRole('heading', { name: '项目资料' })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: '成片元数据' })).not.toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    disposeEdit();
    cleanup();

    const disposeReview = renderDetail('review');
    expect(await screen.findByRole('heading', { name: /真千金是男的/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '编辑项目资料' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /编辑成片元数据/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    disposeReview();
  });

  it('updates project name and an empty description while keeping the project code readonly', async () => {
    const dispose = renderDetail('edit');
    await userEvent.click(await screen.findByRole('button', { name: '编辑项目资料' }));

    const dialog = screen.getByRole('dialog', { name: '编辑项目资料' });
    const code = within(dialog).getByLabelText('项目编号');
    expect(code).toHaveAttribute('readonly');
    expect(code).toHaveValue('FJ-DEMO-28');
    await userEvent.clear(within(dialog).getByLabelText('项目名称'));
    await userEvent.type(within(dialog).getByLabelText('项目名称'), '更新后的项目');
    await userEvent.clear(within(dialog).getByLabelText('项目说明'));
    await userEvent.click(within(dialog).getByRole('button', { name: '保存项目资料' }));

    expect(await screen.findByText('项目资料已更新。')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('heading', { name: /更新后的项目/ })).toBeInTheDocument());
    expect(screen.getByText('暂无项目说明')).toBeInTheDocument();
    expect(screen.queryByRole('dialog', { name: '编辑项目资料' })).not.toBeInTheDocument();
    dispose();
  });

  it('updates title and episode while keeping item_code readonly', async () => {
    const dispose = renderDetail('edit');
    await userEvent.click(await screen.findByRole('button', { name: /编辑成片元数据.*第 28 集/ }));
    const dialog = screen.getByRole('dialog', { name: /编辑成片元数据.*第 28 集/ });

    const code = within(dialog).getByLabelText(/成片编号/);
    expect(code).toHaveAttribute('readonly');
    expect(code).toHaveValue('28');
    await userEvent.clear(within(dialog).getByLabelText('成片标题'));
    await userEvent.type(within(dialog).getByLabelText('成片标题'), '更新后的成片');
    await userEvent.clear(within(dialog).getByLabelText('集数'));
    await userEvent.type(within(dialog).getByLabelText('集数'), '29');
    await userEvent.click(within(dialog).getByRole('button', { name: '保存成片元数据' }));

    expect(await screen.findByText('成片「更新后的成片」元数据已更新。')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId('review-item-table')).toHaveTextContent('第 29 集'));
    expect(screen.queryByRole('dialog', { name: /编辑成片元数据/ })).not.toBeInTheDocument();
    dispose();
  });

  it('updates only the title for a legal nonnumeric item code without inventing an episode number', async () => {
    const onSubmit = vi.fn(async () => undefined);
    const item = {
      reviewItemId: 'item_nonnumeric',
      projectRefId: 'project_nonnumeric',
      itemCode: 'CUT-A',
      title: 'Original cut',
      episode: 'CUT-A',
      currentVersionId: 'version_nonnumeric',
      activeFinalizationId: null,
      status: 'pending_review' as const,
      createdAt: '2026-07-14T00:00:00.000Z',
      updatedAt: '2026-07-14T00:00:00.000Z',
    };
    render(<ReviewItemMetadataEditor item={item} pending={false} onSubmit={onSubmit} />);

    await userEvent.click(screen.getByRole('button', { name: /编辑成片元数据 Original cut/ }));
    const dialog = screen.getByRole('dialog', { name: /编辑成片元数据.*Original cut/ });
    expect(within(dialog).getByLabelText(/^集数/)).toHaveAttribute('readonly');
    expect(within(dialog).getByLabelText(/^集数/)).toHaveValue('CUT-A');
    await userEvent.clear(within(dialog).getByLabelText('成片标题'));
    await userEvent.type(within(dialog).getByLabelText('成片标题'), 'Renamed cut');
    await userEvent.click(within(dialog).getByRole('button', { name: '保存成片元数据' }));

    expect(onSubmit).toHaveBeenCalledWith(item, { title: 'Renamed cut', episode: 'CUT-A' });
  });

  it('keeps the item dialog open and shows the API error when saving fails', async () => {
    const item = {
      reviewItemId: 'item_error',
      projectRefId: 'project_error',
      itemCode: '12',
      title: 'Error cut',
      episode: '12',
      currentVersionId: 'version_error',
      activeFinalizationId: null,
      status: 'pending_review' as const,
      createdAt: '2026-07-14T00:00:00.000Z',
      updatedAt: '2026-07-14T00:00:00.000Z',
    };
    const onSubmit = vi.fn(async () => {
      throw new Error('元数据保存失败');
    });
    render(<ReviewItemMetadataEditor item={item} pending={false} onSubmit={onSubmit} />);

    await userEvent.click(screen.getByRole('button', { name: /编辑成片元数据 Error cut/ }));
    const dialog = screen.getByRole('dialog', { name: /编辑成片元数据.*Error cut/ });
    await userEvent.click(within(dialog).getByRole('button', { name: '保存成片元数据' }));

    expect(await within(dialog).findByText('元数据保存失败')).toBeInTheDocument();
    expect(dialog).toBeInTheDocument();
    expect(onSubmit).toHaveBeenCalledOnce();
  });

  it('removes the compact edit command for finalized metadata', () => {
    render(
      <ReviewItemMetadataEditor
        item={{
          reviewItemId: 'item_finalized',
          projectRefId: 'project_finalized',
          itemCode: 'FINAL-008',
          title: 'Finalized item',
          episode: '8',
          currentVersionId: 'version_finalized',
          activeFinalizationId: 'finalization_1',
          status: 'finalized',
          createdAt: '2026-07-14T00:00:00.000Z',
          updatedAt: '2026-07-14T00:00:00.000Z',
        }}
        pending={false}
        onSubmit={vi.fn()}
      />,
    );
    expect(screen.queryByRole('button', { name: /编辑成片元数据/ })).not.toBeInTheDocument();
  });

  it('closes an open metadata form when the item becomes finalized', async () => {
    const item = {
      reviewItemId: 'item_transition',
      projectRefId: 'project_transition',
      itemCode: 'TRANSITION-008',
      title: 'Transition item',
      episode: '8',
      currentVersionId: 'version_transition',
      activeFinalizationId: null,
      status: 'pending_review' as const,
      createdAt: '2026-07-14T00:00:00.000Z',
      updatedAt: '2026-07-14T00:00:00.000Z',
    };
    const { rerender } = render(
      <ReviewItemMetadataEditor item={item} pending={false} onSubmit={vi.fn()} />,
    );
    await userEvent.click(screen.getByRole('button', { name: /编辑成片元数据 Transition item/ }));
    expect(screen.getByRole('button', { name: '保存成片元数据' })).toBeInTheDocument();

    rerender(
      <ReviewItemMetadataEditor
        item={{ ...item, status: 'finalized', activeFinalizationId: 'finalization_transition' }}
        pending={false}
        onSubmit={vi.fn()}
      />,
    );
    expect(screen.queryByRole('button', { name: '保存成片元数据' })).not.toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
});
