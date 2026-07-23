import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createRef, useState } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { ReviewRuntimeProvider, createReviewRuntime, type ReviewRuntime } from '../entry/runtime';
import { FinalCutReviewClient } from '../contracts-generated/backend-contract';
import type { UploadProgress } from '../contracts/types';
import { createSeedData } from '../core/seed';
import { playbackTargetFromIssue } from '../core/playback';
import { IssuePanel } from './IssuePanel';
import { ReviewPlayer, type ReviewPlayerHandle } from './ReviewPlayer';
import {
  AppendVersionPanel,
  CreateItemUploadPanel,
  episodeFromUploadFileName,
  titleFromUploadFileName,
} from './UploadPanel';
import { DecisionBar } from './DecisionBar';
import { ProjectListPage } from '../pages/ProjectListPage';
import { ProjectDetailPage } from '../pages/ProjectDetailPage';
import { ReviewWorkspacePage } from '../pages/ReviewWorkspacePage';
import { HttpReviewApiAdapter } from '../adapters/http-review-api-adapter';
import {
  V1UploadResultUncertainError,
  isV1ListConfirmationRequired,
  markV1ListConfirmationRequired,
} from '../adapters/http-review-uploads';
import { NoAccountEntryPolicyAdapter } from '../adapters/no-account-permission-adapter';

function renderWithRuntime(ui: React.ReactElement) {
  const runtime = createReviewRuntime();
  const result = render(<ReviewRuntimeProvider runtime={runtime}>{ui}</ReviewRuntimeProvider>);
  return { ...result, runtime };
}

function cleanupRuntime(runtime: ReviewRuntime, queryClient?: QueryClient) {
  runtime.dispose();
  queryClient?.clear();
}

function expectBefore(first: HTMLElement, second: HTMLElement) {
  expect(Boolean(first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING)).toBe(true);
}

function renderProjectDetail(route: string) {
  const runtime = createReviewRuntime();
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  const result = render(
    <QueryClientProvider client={queryClient}>
      <ReviewRuntimeProvider runtime={runtime}>
        <MemoryRouter initialEntries={[route]}>
          <Routes>
            <Route path="/edit/projects/:projectRefId" element={<ProjectDetailPage entryMode="edit" />} />
            <Route path="/review/projects/:projectRefId" element={<ProjectDetailPage entryMode="review" />} />
            <Route path="/edit/projects/:projectRefId/items/:reviewItemId" element={<div data-testid="item-workspace">单集详情</div>} />
            <Route path="/review/projects/:projectRefId/items/:reviewItemId" element={<div data-testid="item-workspace">单集详情</div>} />
            <Route path="/edit/projects" element={<div>项目列表</div>} />
            <Route path="/review/projects" element={<div>项目列表</div>} />
          </Routes>
        </MemoryRouter>
      </ReviewRuntimeProvider>
    </QueryClientProvider>,
  );
  return { ...result, runtime, queryClient };
}

async function renderProjectList(setup?: (runtime: ReviewRuntime) => Promise<void>) {
  const runtime = createReviewRuntime();
  if (setup) await setup(runtime);
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  const result = render(
    <QueryClientProvider client={queryClient}>
      <ReviewRuntimeProvider runtime={runtime}>
        <MemoryRouter initialEntries={['/edit/projects']}>
          <Routes>
            <Route path="/edit/projects" element={<ProjectListPage entryMode="edit" />} />
            <Route path="/edit/projects/:projectRefId" element={<div>项目详情</div>} />
          </Routes>
        </MemoryRouter>
      </ReviewRuntimeProvider>
    </QueryClientProvider>,
  );
  return { ...result, runtime, queryClient };
}

async function renderArchivedReviewWorkspace() {
  const runtime = createReviewRuntime();
  const review = runtime.getApi('review');
  await review.archiveProject({ projectRefId: 'prj_seed_final_cut' }, review.entryPolicy.createContext('review'));
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  const result = render(
    <QueryClientProvider client={queryClient}>
      <ReviewRuntimeProvider runtime={runtime}>
        <MemoryRouter initialEntries={['/review/projects/prj_seed_final_cut/items/item_ep28']}>
          <Routes>
            <Route path="/review/projects/:projectRefId/items/:reviewItemId" element={<ReviewWorkspacePage entryMode="review" />} />
          </Routes>
        </MemoryRouter>
      </ReviewRuntimeProvider>
    </QueryClientProvider>,
  );
  return { ...result, runtime, queryClient };
}

function renderReviewWorkspace(route = '/review/projects/prj_seed_final_cut/items/item_ep28') {
  const runtime = createReviewRuntime();
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  const result = render(
    <QueryClientProvider client={queryClient}>
      <ReviewRuntimeProvider runtime={runtime}>
        <MemoryRouter initialEntries={[route]}>
          <Routes>
            <Route path="/review/projects/:projectRefId/items/:reviewItemId" element={<ReviewWorkspacePage entryMode="review" />} />
          </Routes>
        </MemoryRouter>
      </ReviewRuntimeProvider>
    </QueryClientProvider>,
  );
  return { ...result, runtime, queryClient };
}

function renderEditReviewWorkspace(route = '/edit/projects/prj_seed_final_cut/items/item_ep28') {
  const runtime = createReviewRuntime();
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  const result = render(
    <QueryClientProvider client={queryClient}>
      <ReviewRuntimeProvider runtime={runtime}>
        <MemoryRouter initialEntries={[route]}>
          <Routes>
            <Route path="/edit/projects/:projectRefId/items/:reviewItemId" element={<ReviewWorkspacePage entryMode="edit" />} />
          </Routes>
        </MemoryRouter>
      </ReviewRuntimeProvider>
    </QueryClientProvider>,
  );
  return { ...result, runtime, queryClient };
}

describe('DecisionBar component', () => {
  it('presents a failed package as a retry action', async () => {
    const seed = createSeedData();
    const onPackage = vi.fn();
    const runtimeResult = renderWithRuntime(
      <DecisionBar
        entryMode="review"
        version={seed.versions[1]}
        issues={[]}
        finalization={null}
        isCurrentVersion
        packageState="failed"
        onFinalize={vi.fn()}
        onDownload={vi.fn()}
        onPackage={onPackage}
      />,
    );

    await userEvent.click(screen.getByRole('button', { name: '重新准备项目包' }));
    expect(onPackage).toHaveBeenCalledOnce();
    cleanupRuntime(runtimeResult.runtime);
  });
});

describe('IssuePanel component', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('selects issue from card or timecode and marks selected state', async () => {
    const seed = createSeedData();
    const onSelectIssue = vi.fn();
    const runtimeResult = renderWithRuntime(
      <IssuePanel
        entryMode="review"
        version={seed.versions[1]}
        issues={seed.issues.filter((issue) => issue.versionId === 'ver_ep28_v2')}
        historicalIssues={seed.issues.filter((issue) => issue.versionId === 'ver_ep28_v1')}
        selectedIssueId="issue_v2_001"
        isCurrentVersion
        onCreateIssue={vi.fn()}
        onSelectIssue={onSelectIssue}
        onEditIssue={vi.fn()}
        onReplyIssue={vi.fn()}
        onResolve={vi.fn()}
        onReopen={vi.fn()}
        onDeleteIssue={vi.fn()}
      />,
    );

    const issueCard = screen.getByTestId('issue-issue_v2_001');
    expect(issueCard).toHaveClass('is-selected');
    await userEvent.click(within(issueCard).getByRole('button', { name: /#002/ }));
    await userEvent.click(within(issueCard).getByRole('button', { name: '00:00:00:04' }));
    expect(onSelectIssue).toHaveBeenLastCalledWith(expect.objectContaining({ issueId: 'issue_v2_001', frameNumber: 4 }));
    cleanupRuntime(runtimeResult.runtime);
  });

  it('summarizes annotation chips without exposing raw shape internals', () => {
    const seed = createSeedData();
    const issue = {
      ...seed.issues.find((candidate) => candidate.issueId === 'issue_v2_001')!,
      currentAnnotationSet: {
        ...seed.issues.find((candidate) => candidate.issueId === 'issue_v2_001')!.currentAnnotationSet!,
        shapes: [
          { shapeId: 'shape_text_1', tool: 'text' as const, color: '#58e1d4', lineWidth: 2, text: '文字批注', points: [{ x: 0.2, y: 0.2 }] },
          { shapeId: 'shape_text_2', tool: 'text' as const, color: '#58e1d4', lineWidth: 2, text: '文字批注', points: [{ x: 0.3, y: 0.2 }] },
          { shapeId: 'shape_pen_1', tool: 'pen' as const, color: '#58e1d4', lineWidth: 2, points: [{ x: 0.4, y: 0.4 }] },
          { shapeId: 'shape_pen_2', tool: 'pen' as const, color: '#58e1d4', lineWidth: 2, points: [{ x: 0.5, y: 0.4 }] },
          { shapeId: 'shape_arrow_1', tool: 'arrow' as const, color: '#58e1d4', lineWidth: 2, points: [{ x: 0.1, y: 0.1 }, { x: 0.2, y: 0.2 }] },
        ],
      },
    };
    const runtimeResult = renderWithRuntime(
      <IssuePanel
        entryMode="review"
        version={seed.versions[1]}
        issues={[issue]}
        historicalIssues={[]}
        selectedIssueId="issue_v2_001"
        isCurrentVersion
        onCreateIssue={vi.fn()}
        onSelectIssue={vi.fn()}
        onEditIssue={vi.fn()}
        onReplyIssue={vi.fn()}
        onResolve={vi.fn()}
        onReopen={vi.fn()}
        onDeleteIssue={vi.fn()}
      />,
    );

    const shapeSummary = screen.getByTestId('issue-shapes-issue_v2_001');
    expect(within(shapeSummary).getByText('文字 2')).toBeInTheDocument();
    expect(within(shapeSummary).getByText('画笔 2')).toBeInTheDocument();
    expect(within(shapeSummary).getByText('箭头')).toBeInTheDocument();
    expect(within(shapeSummary).queryByText('文字批注')).not.toBeInTheDocument();
    expect(within(shapeSummary).queryByText('pen')).not.toBeInTheDocument();
    cleanupRuntime(runtimeResult.runtime);
  });

  it('shows lightweight playback pending feedback outside the issue counters', () => {
    const seed = createSeedData();
    const runtimeResult = renderWithRuntime(
      <IssuePanel
        entryMode="review"
        version={seed.versions[1]}
        issues={seed.issues.filter((issue) => issue.versionId === 'ver_ep28_v2')}
        historicalIssues={seed.issues.filter((issue) => issue.versionId === 'ver_ep28_v1')}
        selectedIssueId="issue_v2_001"
        isCurrentVersion
        playbackPending
        onCreateIssue={vi.fn()}
        onSelectIssue={vi.fn()}
        onEditIssue={vi.fn()}
        onReplyIssue={vi.fn()}
        onResolve={vi.fn()}
        onReopen={vi.fn()}
        onDeleteIssue={vi.fn()}
      />,
    );

    const panel = screen.getByTestId('issue-panel');
    expect(panel.querySelector('.fj-review-issue-summary')?.textContent).not.toContain('回放定位中');
    expect(screen.getByRole('status')).toHaveTextContent('正在定位意见画面');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();

    cleanupRuntime(runtimeResult.runtime);
  });

  it('keeps playback errors separate from issue counters', () => {
    const seed = createSeedData();
    const runtimeResult = renderWithRuntime(
      <IssuePanel
        entryMode="review"
        version={seed.versions[1]}
        issues={seed.issues.filter((issue) => issue.versionId === 'ver_ep28_v2')}
        historicalIssues={seed.issues.filter((issue) => issue.versionId === 'ver_ep28_v1')}
        selectedIssueId="issue_v2_001"
        isCurrentVersion
        playbackError="定位失败，请手动跳转"
        onCreateIssue={vi.fn()}
        onSelectIssue={vi.fn()}
        onEditIssue={vi.fn()}
        onReplyIssue={vi.fn()}
        onResolve={vi.fn()}
        onReopen={vi.fn()}
        onDeleteIssue={vi.fn()}
      />,
    );

    const panel = screen.getByTestId('issue-panel');
    expect(panel.querySelector('.fj-review-issue-summary')?.textContent).not.toContain('定位失败');
    expect(screen.getByRole('alert')).toHaveTextContent('定位失败，请手动跳转');
    cleanupRuntime(runtimeResult.runtime);
  });

  it('keeps the issue scroll content in top-first reading order', () => {
    const seed = createSeedData();
    const runtimeResult = renderWithRuntime(
      <IssuePanel
        entryMode="review"
        version={seed.versions[1]}
        issues={seed.issues.filter((issue) => issue.versionId === 'ver_ep28_v2')}
        historicalIssues={seed.issues.filter((issue) => issue.versionId === 'ver_ep28_v1')}
        selectedIssueId="issue_v2_001"
        isCurrentVersion
        onCreateIssue={vi.fn()}
        onSelectIssue={vi.fn()}
        onEditIssue={vi.fn()}
        onReplyIssue={vi.fn()}
        onResolve={vi.fn()}
        onReopen={vi.fn()}
        onDeleteIssue={vi.fn()}
      />,
    );

    const scroll = screen.getByTestId('issue-panel-scroll');
    expect(scroll.children[0]).toHaveClass('fj-review-issue-summary');
    expect(scroll.children[1]).toHaveClass('fj-review-issue-list');
    expect(scroll.children[2]).toHaveClass('fj-review-historical-issues');
    cleanupRuntime(runtimeResult.runtime);
  });

  it('keeps issue panel and toolbar layout contracts compact', () => {
    const css = readFileSync(resolve(process.cwd(), 'src/modules/final-cut-review/styles/fj-review.css'), 'utf8');
    expect(css).toMatch(/\.fj-review-issue-scroll\s*\{[^}]*display:\s*flex;[^}]*flex-direction:\s*column;/s);
    expect(css).toMatch(/\.fj-review-issue-card\s*\{[^}]*align-content:\s*start;/s);
    expect(css).toMatch(/\.fj-review-workspace-head\s*\{[^}]*grid-template-columns:\s*max-content minmax\(0,\s*1fr\) max-content;/s);
    expect(css).toMatch(/\.fj-review-toolbar-dock\s*\{[^}]*justify-self:\s*stretch;[^}]*width:\s*100%;/s);
    expect(css).not.toMatch(/\.fj-review-toolbar-dock\s*\{[^}]*padding-inline-end:/s);
    expect(css).toMatch(/\.fj-review-floating-toolbar\s*\{[^}]*overflow:\s*visible;[^}]*padding:\s*6px 22px 6px 12px;/s);
    expect(css).not.toMatch(/\.fj-review-floating-toolbar\s*\{[^}]*overflow-x:\s*auto;/s);
    expect(css).not.toContain('fj-review-floating-toolbar::-webkit-scrollbar');
    expect(css).not.toContain('scroll-padding-inline');
    expect(css).not.toMatch(/^\s*(html|body|#root)\b/m);
    expect(css).toContain('--fj-review-topbar: #191919;');
    expect(css).toContain('--fj-review-surface: #171a1c;');
    expect(css).toContain('--fj-review-surface-2: #1d2124;');
    expect(css).toContain('--fj-review-input: #0b0d0e;');
    expect(css).toContain('--fj-review-line: #292f31;');
    expect(css).toContain('--fj-review-line-soft: #1f2426;');
    expect(css).toContain('--fj-review-text: #f1f5f4;');
    expect(css).toContain('--fj-review-muted: #8c9695;');
    expect(css).toContain('--fj-review-dim: #586160;');
    expect(css).toContain('--fj-review-workstation-max-width: 1440px;');
    expect(css).toContain('--fj-review-version-rail-width: 150px;');
    expect(css).toContain('--fj-review-issue-panel-width: 340px;');
    expect(css).toMatch(/\.fj-review-workspace-frame\s*\{[^}]*max-width:\s*var\(--fj-review-workstation-max-width\);/s);
    expect(css).toMatch(/@media \(max-width: 1279px\)[\s\S]*\.fj-review-issue-drawer-region\.is-open\s*\{/);
    expect(css).toMatch(/@media \(max-width: 920px\)[\s\S]*--fj-review-topbar-height:\s*84px;/);
    expect(css).not.toMatch(/@media \(max-width: 920px\)[\s\S]*\.fj-review-topbar\s*\{[^}]*height:\s*auto;/);
    expect(css).not.toContain('"panel panel"');
  });

  it('hides create controls in edit entry and labels historical issues read-only', () => {
    const seed = createSeedData();
    const runtimeResult = renderWithRuntime(
      <IssuePanel
        entryMode="edit"
        version={seed.versions[0]}
        issues={seed.issues.filter((issue) => issue.versionId === 'ver_ep28_v1')}
        historicalIssues={seed.issues.filter((issue) => issue.versionId === 'ver_ep28_v2')}
        isCurrentVersion={false}
        onCreateIssue={vi.fn()}
        onSelectIssue={vi.fn()}
        onEditIssue={vi.fn()}
        onReplyIssue={vi.fn()}
        onResolve={vi.fn()}
        onReopen={vi.fn()}
        onDeleteIssue={vi.fn()}
      />,
    );

    expect(screen.queryByTestId('issue-form')).not.toBeInTheDocument();
    expect(screen.getAllByText('历史版本只读').length).toBeGreaterThan(0);
    cleanupRuntime(runtimeResult.runtime);
  });

  it('formats historical issue timecodes with their own frozen version frame rate', () => {
    const seed = createSeedData();
    const v1 = { ...seed.versions[0], fpsNum: 10, fpsDen: 1, originalMedia: { ...seed.versions[0].originalMedia, fpsNum: 10, fpsDen: 1 } };
    const v2 = { ...seed.versions[1], fpsNum: 25, fpsDen: 1, originalMedia: { ...seed.versions[1].originalMedia, fpsNum: 25, fpsDen: 1 } };
    const runtimeResult = renderWithRuntime(
      <IssuePanel
        entryMode="review"
        version={v2}
        versions={[v1, v2]}
        issues={seed.issues.filter((issue) => issue.versionId === 'ver_ep28_v2')}
        historicalIssues={seed.issues.filter((issue) => issue.versionId === 'ver_ep28_v1')}
        isCurrentVersion
        onCreateIssue={vi.fn()}
        onSelectIssue={vi.fn()}
        onEditIssue={vi.fn()}
        onReplyIssue={vi.fn()}
        onResolve={vi.fn()}
        onReopen={vi.fn()}
        onDeleteIssue={vi.fn()}
      />,
    );

    const historicalCard = screen.getByTestId('issue-issue_v1_001');
    expect(within(historicalCard).getByRole('button', { name: '00:00:00:00' })).toBeInTheDocument();
    expect(within(historicalCard).queryByRole('button', { name: '00:00:00:02' })).not.toBeInTheDocument();
    cleanupRuntime(runtimeResult.runtime);
  });

  it('submits edit as a new revision and reply from current review entry', async () => {
    const seed = createSeedData();
    const onEditIssue = vi.fn();
    const onReplyIssue = vi.fn();
    const runtimeResult = renderWithRuntime(
      <IssuePanel
        entryMode="review"
        version={seed.versions[1]}
        issues={seed.issues.filter((issue) => issue.versionId === 'ver_ep28_v2')}
        historicalIssues={[]}
        selectedIssueId="issue_v2_001"
        isCurrentVersion
        onCreateIssue={vi.fn()}
        onSelectIssue={vi.fn()}
        onEditIssue={onEditIssue}
        onReplyIssue={onReplyIssue}
        onResolve={vi.fn()}
        onReopen={vi.fn()}
        onDeleteIssue={vi.fn()}
      />,
    );

    const issueCard = screen.getByTestId('issue-issue_v2_001');
    await userEvent.click(within(issueCard).getByRole('button', { name: '编辑意见' }));
    await userEvent.clear(within(issueCard).getByLabelText('编辑意见正文'));
    await userEvent.type(within(issueCard).getByLabelText('编辑意见正文'), '更新后的画面意见');
    await userEvent.click(within(issueCard).getByRole('button', { name: '保存为新修订' }));
    expect(onEditIssue).toHaveBeenCalledWith(expect.objectContaining({ issueId: 'issue_v2_001' }), '更新后的画面意见');

    await userEvent.click(within(issueCard).getByRole('button', { name: '回复' }));
    await userEvent.type(within(issueCard).getByLabelText('回复意见正文'), '收到，按当前版本处理。');
    await userEvent.click(within(issueCard).getByRole('button', { name: '提交回复' }));
    expect(onReplyIssue).toHaveBeenCalledWith(expect.objectContaining({ issueId: 'issue_v2_001' }), '收到，按当前版本处理。');
    cleanupRuntime(runtimeResult.runtime);
  });

  it('waits for create success before clearing the issue draft', async () => {
    const seed = createSeedData();
    let resolveCreate!: () => void;
    const onCreateIssue = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          resolveCreate = resolve;
        }),
    );
    const runtimeResult = renderWithRuntime(
      <IssuePanel
        entryMode="review"
        version={seed.versions[1]}
        issues={seed.issues.filter((issue) => issue.versionId === 'ver_ep28_v2')}
        historicalIssues={[]}
        isCurrentVersion
        onCreateIssue={onCreateIssue}
        onSelectIssue={vi.fn()}
        onEditIssue={vi.fn()}
        onReplyIssue={vi.fn()}
        onResolve={vi.fn()}
        onReopen={vi.fn()}
        onDeleteIssue={vi.fn()}
      />,
    );
    const textarea = screen.getByLabelText('当前版本意见正文');
    await userEvent.clear(textarea);
    await userEvent.type(textarea, '等待服务端确认后再清空');
    await userEvent.click(screen.getByRole('button', { name: '提交意见' }));

    expect(textarea).toHaveValue('等待服务端确认后再清空');
    expect(textarea).toBeDisabled();
    expect(screen.getByRole('button', { name: '提交中...' })).toBeDisabled();

    await act(async () => resolveCreate());
    await waitFor(() => expect(textarea).toHaveValue(''));
    cleanupRuntime(runtimeResult.runtime);
  });

  it('retains create, edit, and reply drafts when their requests fail', async () => {
    const seed = createSeedData();
    const onCreateIssue = vi.fn().mockRejectedValue(new Error('创建失败'));
    const onEditIssue = vi.fn().mockRejectedValue(new Error('编辑失败'));
    const onReplyIssue = vi.fn().mockRejectedValue(new Error('回复失败'));
    const runtimeResult = renderWithRuntime(
      <IssuePanel
        entryMode="review"
        version={seed.versions[1]}
        issues={seed.issues.filter((issue) => issue.versionId === 'ver_ep28_v2')}
        historicalIssues={[]}
        selectedIssueId="issue_v2_001"
        isCurrentVersion
        onCreateIssue={onCreateIssue}
        onSelectIssue={vi.fn()}
        onEditIssue={onEditIssue}
        onReplyIssue={onReplyIssue}
        onResolve={vi.fn()}
        onReopen={vi.fn()}
        onDeleteIssue={vi.fn()}
      />,
    );

    const createDraft = screen.getByLabelText('当前版本意见正文');
    await userEvent.clear(createDraft);
    await userEvent.type(createDraft, '创建失败后保留');
    await userEvent.click(screen.getByRole('button', { name: '提交意见' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '提交意见' })).not.toBeDisabled());
    expect(createDraft).toHaveValue('创建失败后保留');

    const issueCard = screen.getByTestId('issue-issue_v2_001');
    await userEvent.click(within(issueCard).getByRole('button', { name: '编辑意见' }));
    const editDraft = within(issueCard).getByLabelText('编辑意见正文');
    await userEvent.clear(editDraft);
    await userEvent.type(editDraft, '编辑失败后保留');
    await userEvent.click(within(issueCard).getByRole('button', { name: '保存为新修订' }));
    await waitFor(() => expect(within(issueCard).getByRole('button', { name: '保存为新修订' })).not.toBeDisabled());
    expect(within(issueCard).getByTestId('edit-issue-issue_v2_001')).toBeInTheDocument();
    expect(editDraft).toHaveValue('编辑失败后保留');

    await userEvent.click(within(issueCard).getByRole('button', { name: '回复' }));
    const replyDraft = within(issueCard).getByLabelText('回复意见正文');
    await userEvent.type(replyDraft, '回复失败后保留');
    await userEvent.click(within(issueCard).getByRole('button', { name: '提交回复' }));
    await waitFor(() => expect(within(issueCard).getByRole('button', { name: '提交回复' })).not.toBeDisabled());
    expect(within(issueCard).getByTestId('reply-issue-issue_v2_001')).toBeInTheDocument();
    expect(replyDraft).toHaveValue('回复失败后保留');
    cleanupRuntime(runtimeResult.runtime);
  });

  it('prevents edit and reply forms from closing while their requests are pending', async () => {
    const seed = createSeedData();
    let rejectEdit!: (error: Error) => void;
    let rejectReply!: (error: Error) => void;
    const onEditIssue = vi.fn(
      () =>
        new Promise<void>((_resolve, reject) => {
          rejectEdit = reject;
        }),
    );
    const onReplyIssue = vi.fn(
      () =>
        new Promise<void>((_resolve, reject) => {
          rejectReply = reject;
        }),
    );
    const runtimeResult = renderWithRuntime(
      <IssuePanel
        entryMode="review"
        version={seed.versions[1]}
        issues={seed.issues.filter((issue) => issue.versionId === 'ver_ep28_v2')}
        historicalIssues={[]}
        selectedIssueId="issue_v2_001"
        isCurrentVersion
        onCreateIssue={vi.fn()}
        onSelectIssue={vi.fn()}
        onEditIssue={onEditIssue}
        onReplyIssue={onReplyIssue}
        onResolve={vi.fn()}
        onReopen={vi.fn()}
        onDeleteIssue={vi.fn()}
      />,
    );
    const card = screen.getByTestId('issue-issue_v2_001');

    await userEvent.click(within(card).getByRole('button', { name: '编辑意见' }));
    const editDraft = within(card).getByLabelText('编辑意见正文');
    await userEvent.clear(editDraft);
    await userEvent.type(editDraft, '等待编辑请求完成');
    await userEvent.click(within(card).getByRole('button', { name: '保存为新修订' }));
    expect(within(card).getByRole('button', { name: '编辑意见' })).toBeDisabled();
    expect(within(card).getByRole('button', { name: '回复' })).toBeDisabled();
    expect(within(card).getByTestId('edit-issue-issue_v2_001')).toBeInTheDocument();

    await act(async () => rejectEdit(new Error('编辑失败')));
    await waitFor(() => expect(within(card).getByRole('button', { name: '编辑意见' })).not.toBeDisabled());
    expect(editDraft).toHaveValue('等待编辑请求完成');

    await userEvent.click(within(card).getByRole('button', { name: '回复' }));
    const replyDraft = within(card).getByLabelText('回复意见正文');
    await userEvent.type(replyDraft, '等待回复请求完成');
    await userEvent.click(within(card).getByRole('button', { name: '提交回复' }));
    expect(within(card).getByRole('button', { name: '编辑意见' })).toBeDisabled();
    expect(within(card).getByRole('button', { name: '回复' })).toBeDisabled();
    expect(within(card).getByTestId('reply-issue-issue_v2_001')).toBeInTheDocument();

    await act(async () => rejectReply(new Error('回复失败')));
    await waitFor(() => expect(within(card).getByRole('button', { name: '回复' })).not.toBeDisabled());
    expect(replyDraft).toHaveValue('等待回复请求完成');
    cleanupRuntime(runtimeResult.runtime);
  });

  it('blocks direct and keyboard issue submission while a shared mutation is pending', async () => {
    const seed = createSeedData();
    const onCreateIssue = vi.fn().mockResolvedValue(undefined);
    const onEditIssue = vi.fn().mockResolvedValue(undefined);
    const onReplyIssue = vi.fn().mockResolvedValue(undefined);
    function Harness() {
      const [pending, setPending] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setPending(true)}>模拟全局请求</button>
          <IssuePanel
            entryMode="review"
            version={seed.versions[1]}
            issues={seed.issues.filter((issue) => issue.versionId === 'ver_ep28_v2')}
            historicalIssues={[]}
            selectedIssueId="issue_v2_001"
            isCurrentVersion
            pending={pending}
            onCreateIssue={onCreateIssue}
            onSelectIssue={vi.fn()}
            onEditIssue={onEditIssue}
            onReplyIssue={onReplyIssue}
            onResolve={vi.fn()}
            onReopen={vi.fn()}
            onDeleteIssue={vi.fn()}
          />
        </>
      );
    }
    const runtimeResult = renderWithRuntime(<Harness />);
    const card = screen.getByTestId('issue-issue_v2_001');
    await userEvent.click(within(card).getByRole('button', { name: '编辑意见' }));
    await userEvent.click(within(card).getByRole('button', { name: '回复' }));
    await userEvent.click(screen.getByRole('button', { name: '模拟全局请求' }));

    const createDraft = screen.getByLabelText('当前版本意见正文');
    const editDraft = within(card).getByLabelText('编辑意见正文');
    const replyDraft = within(card).getByLabelText('回复意见正文');
    expect(createDraft).toBeDisabled();
    expect(editDraft).toBeDisabled();
    expect(replyDraft).toBeDisabled();
    fireEvent.keyDown(createDraft, { key: 'Enter', ctrlKey: true });
    fireEvent.keyDown(editDraft, { key: 'Enter', ctrlKey: true });
    fireEvent.keyDown(replyDraft, { key: 'Enter', ctrlKey: true });
    fireEvent.submit(screen.getByTestId('issue-form'));
    fireEvent.submit(within(card).getByTestId('edit-issue-issue_v2_001'));
    fireEvent.submit(within(card).getByTestId('reply-issue-issue_v2_001'));
    expect(onCreateIssue).not.toHaveBeenCalled();
    expect(onEditIssue).not.toHaveBeenCalled();
    expect(onReplyIssue).not.toHaveBeenCalled();
    cleanupRuntime(runtimeResult.runtime);
  });

  it('opens a second edit with the latest saved revision body', async () => {
    const seed = createSeedData();
    const original = seed.issues.find((issue) => issue.issueId === 'issue_v2_001')!;
    function Harness() {
      const [issue, setIssue] = useState(original);
      return (
        <IssuePanel
          entryMode="review"
          version={seed.versions[1]}
          issues={[issue]}
          historicalIssues={[]}
          isCurrentVersion
          onCreateIssue={vi.fn()}
          onSelectIssue={vi.fn()}
          onEditIssue={async (current, body) =>
            setIssue({
              ...current,
              body,
              currentRevisionId: `${current.currentRevisionId}-next`,
              currentRevision: { ...current.currentRevision, content: body },
            })
          }
          onReplyIssue={vi.fn()}
          onResolve={vi.fn()}
          onReopen={vi.fn()}
          onDeleteIssue={vi.fn()}
        />
      );
    }
    const runtimeResult = renderWithRuntime(<Harness />);
    const card = screen.getByTestId('issue-issue_v2_001');
    await userEvent.click(within(card).getByRole('button', { name: '编辑意见' }));
    await userEvent.clear(within(card).getByLabelText('编辑意见正文'));
    await userEvent.type(within(card).getByLabelText('编辑意见正文'), '第一次保存后的正文');
    await userEvent.click(within(card).getByRole('button', { name: '保存为新修订' }));
    await userEvent.click(within(card).getByRole('button', { name: '编辑意见' }));
    expect(within(card).getByLabelText('编辑意见正文')).toHaveValue('第一次保存后的正文');
    cleanupRuntime(runtimeResult.runtime);
  });

  it('confirms deletion for current-version issues and keeps historical issues read-only', async () => {
    const seed = createSeedData();
    const onDeleteIssue = vi.fn();
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const runtimeResult = renderWithRuntime(
      <IssuePanel
        entryMode="review"
        version={seed.versions[1]}
        versions={seed.versions}
        issues={seed.issues.filter((issue) => issue.versionId === 'ver_ep28_v2')}
        historicalIssues={seed.issues.filter((issue) => issue.versionId === 'ver_ep28_v1')}
        selectedIssueId="issue_v2_001"
        isCurrentVersion
        onCreateIssue={vi.fn()}
        onSelectIssue={vi.fn()}
        onEditIssue={vi.fn()}
        onReplyIssue={vi.fn()}
        onResolve={vi.fn()}
        onReopen={vi.fn()}
        onDeleteIssue={onDeleteIssue}
      />,
    );

    const currentCard = screen.getByTestId('issue-issue_v2_001');
    await userEvent.click(within(currentCard).getByRole('button', { name: '删除意见' }));
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining('确认删除意见 #002'));
    expect(onDeleteIssue).toHaveBeenCalledWith(expect.objectContaining({ issueId: 'issue_v2_001' }));
    expect(within(screen.getByTestId('historical-issues')).queryByRole('button', { name: '删除意见' })).not.toBeInTheDocument();
    cleanupRuntime(runtimeResult.runtime);
  });

  it('renders hostile issue and reply content as inert text', () => {
    const seed = createSeedData();
    const issue = {
      ...seed.issues.find((entry) => entry.issueId === 'issue_v2_001')!,
      body: '<script>window.__xss = true</script>',
      replies: [
        {
          messageId: 'msg-xss',
          projectRefId: 'prj_seed_final_cut',
          reviewItemId: 'item_ep28',
          versionId: 'ver_ep28_v2',
          issueId: 'issue_v2_001',
          body: '<img src=x onerror=alert(1)>',
          createdAt: '2026-07-10T00:00:00.000Z',
        },
      ],
    };
    const runtimeResult = renderWithRuntime(
      <IssuePanel
        entryMode="review"
        version={seed.versions[1]}
        issues={[issue]}
        historicalIssues={[]}
        isCurrentVersion
        onCreateIssue={vi.fn()}
        onSelectIssue={vi.fn()}
        onEditIssue={vi.fn()}
        onReplyIssue={vi.fn()}
        onResolve={vi.fn()}
        onReopen={vi.fn()}
        onDeleteIssue={vi.fn()}
      />,
    );

    expect(screen.getByText('<script>window.__xss = true</script>')).toBeInTheDocument();
    expect(screen.getByText('<img src=x onerror=alert(1)>')).toBeInTheDocument();
    expect(runtimeResult.container.querySelector('script')).toBeNull();
    expect(runtimeResult.container.querySelector('img')).toBeNull();
    cleanupRuntime(runtimeResult.runtime);
  });

  it('submits the current issue form with Ctrl+Enter', async () => {
    const seed = createSeedData();
    const onCreateIssue = vi.fn();
    const runtimeResult = renderWithRuntime(
      <IssuePanel
        entryMode="review"
        version={seed.versions[1]}
        issues={seed.issues.filter((issue) => issue.versionId === 'ver_ep28_v2')}
        historicalIssues={[]}
        isCurrentVersion
        onCreateIssue={onCreateIssue}
        onSelectIssue={vi.fn()}
        onEditIssue={vi.fn()}
        onReplyIssue={vi.fn()}
        onResolve={vi.fn()}
        onReopen={vi.fn()}
        onDeleteIssue={vi.fn()}
      />,
    );

    const textarea = screen.getByLabelText('当前版本意见正文');
    await userEvent.clear(textarea);
    await userEvent.type(textarea, '快捷键提交当前意见');
    fireEvent.keyDown(textarea, { key: 'Enter', ctrlKey: true });

    expect(onCreateIssue).toHaveBeenCalledWith('快捷键提交当前意见');
    cleanupRuntime(runtimeResult.runtime);
  });
});

describe('ProjectListPage filtering and paging', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('filters projects by search text, lifecycle, and derived completion state', async () => {
    const runtimeResult = await renderProjectList(async (runtime) => {
      const edit = runtime.getApi('edit');
      const context = edit.entryPolicy.createContext('edit');
      await edit.createProject(
        {
          name: '001模拟测试',
          code: 'FJ-001',
          description: '用于验证搜索和未创建成片筛选。',
        },
        context,
      );
      const archived = await edit.createProject(
        {
          name: '归档样片',
          code: 'FJ-ARCHIVE',
          description: '用于验证生命周期筛选。',
        },
        context,
      );
      const review = runtime.getApi('review');
      await review.archiveProject({ projectRefId: archived.projectRefId }, review.entryPolicy.createContext('review'));
    });

    expect(await screen.findByRole('heading', { name: '项目列表' })).toBeInTheDocument();
    await screen.findByText('001模拟测试');
    expect(screen.getByText('真千金是男的')).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText('搜索项目'), '001');
    expect(screen.getByText('001模拟测试')).toBeInTheDocument();
    expect(screen.queryByText('真千金是男的')).not.toBeInTheDocument();

    await userEvent.clear(screen.getByLabelText('搜索项目'));
    await userEvent.click(within(screen.getByRole('radiogroup', { name: '生命周期筛选' })).getByRole('radio', { name: '已归档' }));
    expect(await screen.findByText('归档样片')).toBeInTheDocument();
    expect(screen.queryByText('001模拟测试')).not.toBeInTheDocument();

    await userEvent.click(within(screen.getByRole('radiogroup', { name: '生命周期筛选' })).getByRole('radio', { name: '全部' }));
    await userEvent.click(within(screen.getByRole('radiogroup', { name: '完成状态筛选' })).getByRole('radio', { name: '未创建成片' }));
    expect(await screen.findByText('001模拟测试')).toBeInTheDocument();
    expect(screen.getByText('归档样片')).toBeInTheDocument();
    expect(screen.queryByText('真千金是男的')).not.toBeInTheDocument();

    await userEvent.click(within(screen.getByRole('radiogroup', { name: '完成状态筛选' })).getByRole('radio', { name: '未完成' }));
    expect(await screen.findByText('真千金是男的')).toBeInTheDocument();
    expect(screen.queryByText('001模拟测试')).not.toBeInTheDocument();

    cleanupRuntime(runtimeResult.runtime, runtimeResult.queryClient);
  });

  it('orders project cards by updated time in both directions', async () => {
    const runtimeResult = await renderProjectList(async (runtime) => {
      const edit = runtime.getApi('edit');
      const context = edit.entryPolicy.createContext('edit');
      vi.useFakeTimers();
      try {
        vi.setSystemTime(new Date('2026-01-01T00:00:00.000Z'));
        await edit.createProject({
          name: '排序旧项目',
          code: 'SORT-OLD',
          description: '用于验证更新时间升序。',
        }, context);
        vi.setSystemTime(new Date('2026-01-02T00:00:00.000Z'));
        await edit.createProject({
          name: '排序新项目',
          code: 'SORT-NEW',
          description: '用于验证更新时间降序。',
        }, context);
      } finally {
        vi.useRealTimers();
      }
    });

    const newProject = await screen.findByText('排序新项目');
    const oldProject = await screen.findByText('排序旧项目');
    expectBefore(newProject, oldProject);

    await userEvent.click(within(screen.getByRole('radiogroup', { name: '更新时间排序' })).getByRole('radio', { name: '旧到新' }));
    await waitFor(() => expectBefore(screen.getByText('排序旧项目'), screen.getByText('排序新项目')));

    await userEvent.click(within(screen.getByRole('radiogroup', { name: '更新时间排序' })).getByRole('radio', { name: '新到旧' }));
    await waitFor(() => expectBefore(screen.getByText('排序新项目'), screen.getByText('排序旧项目')));

    cleanupRuntime(runtimeResult.runtime, runtimeResult.queryClient);
  });

  it('paginates project cards with 20, 50, and 100 item page-size controls', async () => {
    const runtimeResult = await renderProjectList(async (runtime) => {
      const edit = runtime.getApi('edit');
      const context = edit.entryPolicy.createContext('edit');
      for (let index = 1; index <= 25; index += 1) {
        await edit.createProject(
          {
            name: `分页项目 ${String(index).padStart(2, '0')}`,
            code: `PAGE-${String(index).padStart(2, '0')}`,
            description: '用于验证项目列表分页。',
          },
          context,
        );
      }
    });

    expect(await screen.findByRole('heading', { name: '项目列表' })).toBeInTheDocument();
    expect(await screen.findByText(/显示 1-20 \/ 26/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: '下一页' }));
    expect(await screen.findByText(/显示 21-26 \/ 26/)).toBeInTheDocument();

    await userEvent.click(within(screen.getByRole('radiogroup', { name: '每页数量' })).getByRole('radio', { name: '50' }));
    expect(await screen.findByText(/显示 1-26 \/ 26/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '下一页' })).toBeDisabled();

    await userEvent.click(within(screen.getByRole('radiogroup', { name: '每页数量' })).getByRole('radio', { name: '100' }));
    expect(await screen.findByText(/显示 1-26 \/ 26/)).toBeInTheDocument();

    cleanupRuntime(runtimeResult.runtime, runtimeResult.queryClient);
  });
});

describe('ProjectDetailPage archive workflow', () => {
  afterEach(() => {
    window.sessionStorage.clear();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('keeps project archive and delete out of the edit project entry', async () => {
    const runtimeResult = renderProjectDetail('/edit/projects/prj_seed_final_cut');

    expect(await screen.findByRole('heading', { name: /真千金是男的/ })).toBeInTheDocument();
    expect(screen.getByTestId('create-item-upload')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '归档项目' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '删除项目' })).not.toBeInTheDocument();

    cleanupRuntime(runtimeResult.runtime, runtimeResult.queryClient);
  });

  it('keeps recent V1 title and episode after upload and refreshes the item list without opening the item workspace', async () => {
    const runtimeResult = renderProjectDetail('/edit/projects/prj_seed_final_cut');

    expect(await screen.findByRole('heading', { name: /真千金是男的/ })).toBeInTheDocument();
    await userEvent.upload(screen.getByTestId('create-item-file'), new File(['v1'], '王爷02.mp4', { type: 'video/mp4' }));
    const row = screen.getByTestId('upload-row-0');
    const titleInput = within(row).getByLabelText('成片标题');
    const episodeInput = within(row).getByLabelText('集数');
    await userEvent.clear(titleInput);
    await userEvent.type(titleInput, '不作为卡片大标题');
    await userEvent.clear(episodeInput);
    await userEvent.type(episodeInput, '02');
    await userEvent.click(screen.getByRole('button', { name: '上传 V1' }));

    expect(await screen.findByText('第 02 集', { selector: 'strong' })).toBeInTheDocument();
    expect(screen.queryByText('不作为卡片大标题', { selector: 'strong' })).not.toBeInTheDocument();
    expect(await screen.findByText('原文件：王爷02.mp4 · 1个版本 · 当前 V1')).toBeInTheDocument();
    expect(screen.queryByTestId('item-workspace')).not.toBeInTheDocument();
    expect(screen.queryByTestId('upload-row-0')).not.toBeInTheDocument();
    expect((screen.getByTestId('create-item-file') as HTMLInputElement).files).toHaveLength(0);
    expect(screen.getByRole('button', { name: '上传 V1' })).toBeInTheDocument();
    expect(screen.queryByTestId('upload-progress')).not.toBeInTheDocument();

    cleanupRuntime(runtimeResult.runtime, runtimeResult.queryClient);
  });

  it('removes the successful V1 row and reports a refresh failure without retrying the upload', async () => {
    const runtimeResult = renderProjectDetail('/edit/projects/prj_seed_final_cut');
    expect(await screen.findByRole('heading', { name: /真千金是男的/ })).toBeInTheDocument();
    const editApi = runtimeResult.runtime.getApi('edit');
    const originalGetProjectDetail = editApi.getProjectDetail.bind(editApi);
    let refreshCount = 0;
    vi.spyOn(editApi, 'getProjectDetail').mockImplementation((...args) => {
      refreshCount += 1;
      return refreshCount === 1 ? originalGetProjectDetail(...args) : Promise.reject(new Error('refresh offline'));
    });
    await userEvent.upload(screen.getByTestId('create-item-file'), new File(['v1'], 'refresh-fail.mp4', { type: 'video/mp4' }));
    const row = screen.getByTestId('upload-row-0');
    await userEvent.clear(within(row).getByLabelText('成片标题'));
    await userEvent.type(within(row).getByLabelText('成片标题'), '刷新失败保留');
    await userEvent.clear(within(row).getByLabelText('集数'));
    await userEvent.type(within(row).getByLabelText('集数'), '31');
    await userEvent.click(screen.getByRole('button', { name: '上传 V1' }));

    expect(await screen.findByText(
      '文件已上传成功，待审列表暂时刷新失败，请刷新页面查看。',
      {},
      { timeout: 6000 },
    )).toBeInTheDocument();
    expect(screen.queryByTestId('upload-row-0')).not.toBeInTheDocument();
    expect((screen.getByTestId('create-item-file') as HTMLInputElement).files).toHaveLength(0);
    expect(screen.queryByTestId('upload-progress')).not.toBeInTheDocument();

    cleanupRuntime(runtimeResult.runtime, runtimeResult.queryClient);
  }, 8000);

  it('blocks a new V1 after a lost response and page reload until the user confirms the refreshed list', async () => {
    const entryPolicy = new NoAccountEntryPolicyAdapter();
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/api/v1/final-cut-review/projects/prj_seed_final_cut')) {
        return new Response(JSON.stringify({
          data: {
            project_ref_id: 'prj_seed_final_cut',
            project_code: 'FJ-DEMO-28',
            project_name: '真千金是男的',
            source: 'local',
            lifecycle_status: 'active',
            completion_status: 'in_progress',
            lock_version: 1,
            created_at: '2026-07-13T00:00:00.000Z',
            updated_at: '2026-07-13T00:00:00.000Z',
          },
          meta: { request_id: 'req-project', contract_version: '1.0' },
        }), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      if (url.endsWith('/api/v1/final-cut-review/edit/projects/prj_seed_final_cut/items')) {
        throw new TypeError('response lost after server commit');
      }
      throw new Error(`unexpected URL ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    const httpApi = new HttpReviewApiAdapter(
      'edit',
      new FinalCutReviewClient('https://review.example'),
      entryPolicy,
      'https://review.example',
      {
        entryMode: 'edit',
        notify: () => undefined,
        downloadBlob: () => undefined,
        downloadUrl: () => undefined,
      },
    );
    vi.spyOn(
      httpApi as unknown as { uploadFile: (file: File) => Promise<{ file_id: string }> },
      'uploadFile',
    ).mockResolvedValue({ file_id: 'file_response_lost' });
    const selectedFile = new File(['v1'], 'must-not-persist.mp4', { type: 'video/mp4' });

    await expect(httpApi.createReviewItemWithVersion(
      {
        projectRefId: 'prj_seed_final_cut',
        title: '不得持久化的 V1 payload',
        episode: '31',
        file: selectedFile,
      },
      entryPolicy.createContext('edit'),
    )).rejects.toThrow('response lost after server commit');
    expect(isV1ListConfirmationRequired('prj_seed_final_cut')).toBe(true);
    const persistedEntries = Array.from({ length: window.sessionStorage.length }, (_, index) => {
      const key = window.sessionStorage.key(index) ?? '';
      return [key, window.sessionStorage.getItem(key)] as const;
    });
    expect(persistedEntries).toHaveLength(1);
    expect(persistedEntries[0]?.[1]).toBe('required');
    expect(JSON.stringify(persistedEntries)).not.toContain(selectedFile.name);
    expect(JSON.stringify(persistedEntries)).not.toContain('不得持久化的 V1 payload');
    expect(JSON.stringify(persistedEntries)).not.toContain('CreateReviewItem_');

    vi.unstubAllGlobals();
    const runtimeResult = renderProjectDetail('/edit/projects/prj_seed_final_cut');
    const createSpy = vi.spyOn(runtimeResult.runtime.getApi('edit'), 'createReviewItemWithVersion');

    expect(await screen.findByTestId('v1-list-confirmation-required')).toHaveTextContent(
      '请先确认上一笔 V1 的列表结果',
    );
    expect(screen.getByTestId('create-item-file')).toBeDisabled();
    expect(screen.getByRole('button', { name: '请先确认列表' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '请先确认列表' }));
    expect(createSpy).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole('button', { name: '我已核对列表，允许新建 V1' }));
    await waitFor(() => expect(screen.getByTestId('create-item-file')).toBeEnabled());
    expect(screen.getByRole('button', { name: '上传 V1' })).toBeDisabled();
    expect(screen.queryByTestId('v1-list-confirmation-required')).not.toBeInTheDocument();
    expect(isV1ListConfirmationRequired('prj_seed_final_cut')).toBe(false);

    cleanupRuntime(runtimeResult.runtime, runtimeResult.queryClient);
  });

  it('keeps the project protected when a concurrent success settles before an uncertain V1', async () => {
    const runtimeResult = renderProjectDetail('/edit/projects/prj_seed_final_cut');
    expect(await screen.findByRole('heading', { name: /真千金是男的/ })).toBeInTheDocument();
    const editApi = runtimeResult.runtime.getApi('edit');
    const originalCreate = editApi.createReviewItemWithVersion.bind(editApi);
    type CreateResult = Awaited<ReturnType<typeof originalCreate>>;
    type CreateArgs = Parameters<typeof originalCreate>;
    let successArgs: CreateArgs | undefined;
    let resolveSuccess!: (value: CreateResult) => void;
    let rejectUncertain!: (reason: unknown) => void;
    const successPromise = new Promise<CreateResult>((resolve) => {
      resolveSuccess = resolve;
    });
    const uncertainPromise = new Promise<CreateResult>((_resolve, reject) => {
      rejectUncertain = reject;
    });
    const createSpy = vi.spyOn(editApi, 'createReviewItemWithVersion').mockImplementation((...args) => {
      if (args[0].file.name === 'success-first.mp4') {
        successArgs = args;
        return successPromise;
      }
      return uncertainPromise;
    });

    await userEvent.upload(screen.getByTestId('create-item-file'), [
      new File(['success'], 'success-first.mp4', { type: 'video/mp4' }),
      new File(['uncertain'], 'uncertain-later.mp4', { type: 'video/mp4' }),
    ]);
    const uploadRows = screen.getAllByTestId(/upload-row-\d+$/);
    await userEvent.type(within(uploadRows[0]).getByLabelText('集数'), '41');
    await userEvent.type(within(uploadRows[1]).getByLabelText('集数'), '42');
    await userEvent.click(screen.getByRole('button', { name: '上传 V1' }));
    await waitFor(() => expect(createSpy).toHaveBeenCalledTimes(2));
    if (!successArgs) throw new Error('missing success upload arguments');
    resolveSuccess(await originalCreate(...successArgs));
    await waitFor(() => expect(screen.queryByText('success-first.mp4')).not.toBeInTheDocument());

    rejectUncertain(new V1UploadResultUncertainError(new TypeError('version response lost')));
    expect(await screen.findByTestId('upload-row-1-error')).toHaveTextContent('结果不确定');
    expect(isV1ListConfirmationRequired('prj_seed_final_cut')).toBe(true);
    expect(screen.getByTestId('v1-list-confirmation-required')).toHaveTextContent('请先确认上一笔 V1 的列表结果');

    cleanupRuntime(runtimeResult.runtime, runtimeResult.queryClient);
  });

  it('keeps a provisional V1 protected across another success and a page remount', async () => {
    const first = renderProjectDetail('/edit/projects/prj_seed_final_cut');
    expect(await screen.findByRole('heading', { name: /真千金是男的/ })).toBeInTheDocument();
    const editApi = first.runtime.getApi('edit');
    const originalCreate = editApi.createReviewItemWithVersion.bind(editApi);
    type CreateResult = Awaited<ReturnType<typeof originalCreate>>;
    let rejectProvisional!: (reason: unknown) => void;
    const provisionalPromise = new Promise<CreateResult>((_resolve, reject) => {
      rejectProvisional = reject;
    });
    const createSpy = vi.spyOn(editApi, 'createReviewItemWithVersion').mockImplementation((...args) => {
      if (args[0].file.name === 'provisional-first.mp4') {
        markV1ListConfirmationRequired('prj_seed_final_cut');
        return provisionalPromise;
      }
      return originalCreate(...args);
    });

    await userEvent.upload(screen.getByTestId('create-item-file'), [
      new File(['provisional'], 'provisional-first.mp4', { type: 'video/mp4' }),
      new File(['success'], 'success-second.mp4', { type: 'video/mp4' }),
    ]);
    const uploadRows = screen.getAllByTestId(/upload-row-\d+$/);
    await userEvent.type(within(uploadRows[0]).getByLabelText('集数'), '43');
    await userEvent.type(within(uploadRows[1]).getByLabelText('集数'), '44');
    await userEvent.click(screen.getByRole('button', { name: '上传 V1' }));
    await waitFor(() => expect(createSpy).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByText('success-second.mp4')).not.toBeInTheDocument());
    expect(isV1ListConfirmationRequired('prj_seed_final_cut')).toBe(true);

    first.unmount();
    cleanupRuntime(first.runtime, first.queryClient);
    const second = renderProjectDetail('/edit/projects/prj_seed_final_cut');
    expect(await screen.findByTestId('v1-list-confirmation-required')).toHaveTextContent(
      '请先确认上一笔 V1 的列表结果',
    );

    rejectProvisional(new V1UploadResultUncertainError(new TypeError('version response lost')));
    second.unmount();
    cleanupRuntime(second.runtime, second.queryClient);
  });

  it('does not allow early confirmation while another V1 result is still provisional', async () => {
    const first = renderProjectDetail('/edit/projects/prj_seed_final_cut');
    expect(await screen.findByRole('heading', { name: /真千金是男的/ })).toBeInTheDocument();
    const editApi = first.runtime.getApi('edit');
    type CreateResult = Awaited<ReturnType<typeof editApi.createReviewItemWithVersion>>;
    let rejectProvisional!: (reason: unknown) => void;
    const provisionalPromise = new Promise<CreateResult>((_resolve, reject) => {
      rejectProvisional = reject;
    });
    const createSpy = vi.spyOn(editApi, 'createReviewItemWithVersion').mockImplementation((input) => {
      if (input.file.name === 'uncertain-first.mp4') {
        return Promise.reject(new V1UploadResultUncertainError(new TypeError('command response lost')));
      }
      markV1ListConfirmationRequired('prj_seed_final_cut');
      return provisionalPromise;
    });

    await userEvent.upload(screen.getByTestId('create-item-file'), [
      new File(['uncertain'], 'uncertain-first.mp4', { type: 'video/mp4' }),
      new File(['provisional'], 'provisional-second.mp4', { type: 'video/mp4' }),
    ]);
    const uploadRows = screen.getAllByTestId(/upload-row-\d+$/);
    await userEvent.type(within(uploadRows[0]).getByLabelText('集数'), '45');
    await userEvent.type(within(uploadRows[1]).getByLabelText('集数'), '46');
    await userEvent.click(screen.getByRole('button', { name: '上传 V1' }));
    await waitFor(() => expect(createSpy).toHaveBeenCalledTimes(2));

    const confirm = await screen.findByRole('button', { name: '我已核对列表，允许新建 V1' });
    expect(confirm).toBeDisabled();
    expect(screen.getByTestId('v1-list-confirmation-required')).toHaveTextContent('仍有 1 条 V1 正在结算');
    fireEvent.click(confirm);
    expect(isV1ListConfirmationRequired('prj_seed_final_cut')).toBe(true);

    first.unmount();
    cleanupRuntime(first.runtime, first.queryClient);
    const second = renderProjectDetail('/edit/projects/prj_seed_final_cut');
    expect(await screen.findByTestId('v1-list-confirmation-required')).toHaveTextContent(
      '请先确认上一笔 V1 的列表结果',
    );

    rejectProvisional(new V1UploadResultUncertainError(new TypeError('version response lost')));
    second.unmount();
    cleanupRuntime(second.runtime, second.queryClient);
  });

  it('keeps V1 fail-closed across remounts when session storage writes are unavailable', async () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('storage blocked', 'QuotaExceededError');
    });

    const first = renderProjectDetail('/edit/projects/prj_seed_final_cut');
    expect(await screen.findByTestId('v1-list-confirmation-required')).toHaveTextContent(
      '浏览器会话存储不可用',
    );
    expect(screen.getByTestId('create-item-file')).toBeDisabled();
    expect(screen.getByRole('button', { name: '请先确认列表' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: '我已核对列表，允许新建 V1' })).not.toBeInTheDocument();
    first.unmount();
    cleanupRuntime(first.runtime, first.queryClient);

    const second = renderProjectDetail('/edit/projects/prj_seed_final_cut');
    expect(await screen.findByTestId('v1-list-confirmation-required')).toHaveTextContent(
      '浏览器会话存储不可用',
    );
    expect(screen.getByTestId('create-item-file')).toBeDisabled();
    second.unmount();
    cleanupRuntime(second.runtime, second.queryClient);
    setItem.mockRestore();
  });

  it('keeps one episode row while exposing a pending duplicate item for deletion', async () => {
    const runtimeResult = renderProjectDetail('/edit/projects/prj_seed_final_cut');
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);

    expect(await screen.findByRole('heading', { name: /真千金是男的/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /删除分集 第 28 集/ })).not.toBeInTheDocument();
    await userEvent.upload(screen.getByTestId('create-item-file'), new File(['v1'], '王爷28-duplicate.mp4', { type: 'video/mp4' }));
    const row = screen.getByTestId('upload-row-0');
    await userEvent.clear(within(row).getByLabelText('成片标题'));
    await userEvent.type(within(row).getByLabelText('成片标题'), '第 28 集误传');
    await userEvent.clear(within(row).getByLabelText('集数'));
    await userEvent.type(within(row).getByLabelText('集数'), '28');
    await userEvent.click(screen.getByRole('button', { name: '上传 V1' }));

    expect(screen.getAllByText(/原文件：.* · 2个版本 · 当前 V2/)).toHaveLength(1);
    await userEvent.click(await screen.findByRole('button', { name: '删除分集 第 28 集误传' }));

    expect(confirmSpy).toHaveBeenCalledWith(expect.stringContaining('审核开始前去重'));
    await waitFor(() => expect(screen.queryByRole('button', { name: '删除分集 第 28 集误传' })).not.toBeInTheDocument());
    expect(screen.getAllByText(/原文件：.* · 2个版本 · 当前 V2/)).toHaveLength(1);
    expect(await screen.findByText('分集已删除，列表已刷新。')).toBeInTheDocument();

    confirmSpy.mockRestore();
    cleanupRuntime(runtimeResult.runtime, runtimeResult.queryClient);
  });

  it('exposes project delete only through review controls', async () => {
    const runtimeResult = renderProjectDetail('/review/projects/prj_seed_final_cut');

    expect(await screen.findByRole('heading', { name: /真千金是男的/ })).toBeInTheDocument();
    expect(screen.queryByTestId('create-item-upload')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '删除项目' })).toBeInTheDocument();
    expect(screen.queryByText('进入')).not.toBeInTheDocument();

    cleanupRuntime(runtimeResult.runtime, runtimeResult.queryClient);
  });

  it('archives and restores a project through review controls while keeping descendants readable', async () => {
    const runtimeResult = renderProjectDetail('/review/projects/prj_seed_final_cut');

    expect(await screen.findByRole('heading', { name: /真千金是男的/ })).toBeInTheDocument();
    expect(screen.queryByTestId('create-item-upload')).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: '归档项目' }));
    const readonlyNotice = await screen.findByTestId('archived-readonly-notice');
    expect(readonlyNotice).toHaveTextContent('项目已归档');
    expect(readonlyNotice).toHaveTextContent('恢复项目后才能创建成片');
    expect(screen.queryByTestId('create-item-upload')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '恢复项目' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '删除项目' })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: '恢复项目' }));
    expect(await screen.findByText('项目已恢复，可继续剪辑管理。')).toBeInTheDocument();
    expect(screen.queryByTestId('create-item-upload')).not.toBeInTheDocument();

    cleanupRuntime(runtimeResult.runtime, runtimeResult.queryClient);
  });

  it('keeps archived review workspace readable while hiding review write controls', async () => {
    const runtimeResult = await renderArchivedReviewWorkspace();

    expect(await screen.findByTestId('review-player')).toBeInTheDocument();
    expect(await screen.findByTestId('archived-workspace-readonly-notice')).toHaveTextContent('归档项目只读');
    expect(screen.queryByTestId('issue-form')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '要求修改' })).not.toBeInTheDocument();
    expect(screen.queryByTestId('finalize-current')).not.toBeInTheDocument();
    expect(screen.getByTestId('package-project')).toBeInTheDocument();

    cleanupRuntime(runtimeResult.runtime, runtimeResult.queryClient);
  });
});

describe('AppendVersionPanel component', () => {
  it('blocks V1 creation when no original file is selected', () => {
    const onSubmit = vi.fn();
    render(<CreateItemUploadPanel onSubmit={onSubmit} />);

    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: '上传 V1' })).toBeDisabled();
    expect(screen.getByTestId('create-item-file')).toHaveAttribute('multiple');
  });

  it('uses adjacent matching custom actions while keeping the native file input visually hidden', () => {
    render(<CreateItemUploadPanel onSubmit={vi.fn()} />);

    const choose = screen.getByRole('button', { name: '选择文件' });
    const upload = screen.getByRole('button', { name: '上传 V1' });
    const nativeInput = screen.getByTestId('create-item-file');
    expect(choose.parentElement).toBe(upload.parentElement);
    expect(choose).toHaveClass('fj-review-primary');
    expect(upload).toHaveClass('fj-review-primary');
    expect(nativeInput).toHaveClass('fj-review-sr-only');
    expect(nativeInput).toHaveAttribute('aria-label', '原片文件（可多选）');
    expect(screen.queryByText(/fakepath|本地路径/i)).not.toBeInTheDocument();
  });

  it('derives titles and episodes without guessing ambiguous numeric candidates', () => {
    expect(titleFromUploadFileName('狼王.最终版.第02集.mov')).toBe('狼王.最终版.第02集');
    expect(episodeFromUploadFileName('狼王别跑01.mp4')).toBe('01');
    expect(episodeFromUploadFileName('狼王别跑第2集终版.mov')).toBe('2');
    expect(episodeFromUploadFileName('狼王2025_第03集_v2.mp4')).toBe('03');
    expect(episodeFromUploadFileName('狼王别跑_01_02.mp4')).toBe('');
    expect(episodeFromUploadFileName('狼王别跑_final.mp4')).toBe('');
  });

  it('keeps each selected file bound to its independently editable title and episode', async () => {
    const onSubmit = vi.fn().mockResolvedValue({ outcome: 'success' });
    const firstFile = new File(['v1'], '狼王别跑01.mp4', { type: 'video/mp4' });
    const secondFile = new File(['v1'], '狼王别跑第02集.mov', { type: 'video/quicktime' });
    render(<CreateItemUploadPanel onSubmit={onSubmit} />);

    await userEvent.upload(screen.getByTestId('create-item-file'), [firstFile, secondFile]);
    const firstRow = screen.getByTestId('upload-row-0');
    const secondRow = screen.getByTestId('upload-row-1');
    expect(within(firstRow).getByLabelText('成片标题')).toHaveValue('狼王别跑01');
    expect(within(firstRow).getByLabelText('集数')).toHaveValue('01');
    expect(within(secondRow).getByLabelText('成片标题')).toHaveValue('狼王别跑第02集');
    expect(within(secondRow).getByLabelText('集数')).toHaveValue('02');
    await userEvent.clear(within(secondRow).getByLabelText('成片标题'));
    await userEvent.type(within(secondRow).getByLabelText('成片标题'), '手工标题');
    await userEvent.click(screen.getByRole('button', { name: '上传 V1' }));

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(2));
    expect(onSubmit.mock.calls[0]?.[0]).toEqual({ title: '狼王别跑01', episode: '01', file: firstFile });
    expect(onSubmit.mock.calls[1]?.[0]).toEqual({ title: '手工标题', episode: '02', file: secondFile });
    await waitFor(() => expect(screen.queryByTestId('create-item-upload-rows')).not.toBeInTheDocument());
  });

  it('runs at most five complete V1 pipelines, keeps queued rows session-free, and backfills slots', async () => {
    const finishers: Array<() => void> = [];
    const active = new Set<string>();
    const progressByFile = new Map<string, (progress: UploadProgress) => void>();
    let maxActive = 0;
    const onSubmit = vi.fn((input: { file: File }, onProgress?: (progress: UploadProgress) => void) =>
      new Promise<{ outcome: 'success' }>((resolveResult) => {
        active.add(input.file.name);
        maxActive = Math.max(maxActive, active.size);
        if (onProgress) progressByFile.set(input.file.name, onProgress);
        finishers.push(() => {
          active.delete(input.file.name);
          resolveResult({ outcome: 'success' });
        });
      }));
    render(<CreateItemUploadPanel onSubmit={onSubmit} />);

    const files = Array.from({ length: 7 }, (_, index) =>
      new File([String(index)], `狼王0${index + 1}.mp4`, { type: 'video/mp4' }));
    await userEvent.upload(screen.getByTestId('create-item-file'), files);
    await userEvent.click(screen.getByRole('button', { name: '上传 V1' }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(5));
    expect(maxActive).toBe(5);
    expect(screen.getByTestId('upload-row-5')).toHaveTextContent('排队中');
    expect(screen.getByTestId('upload-row-6')).toHaveTextContent('排队中');
    expect(progressByFile.has('狼王06.mp4')).toBe(false);

    act(() => {
      progressByFile.get('狼王01.mp4')?.({ stage: 'uploading', percent: 11, bytesSent: 11, totalBytes: 100 });
      progressByFile.get('狼王02.mp4')?.({ stage: 'uploading', percent: 22, bytesSent: 22, totalBytes: 100 });
    });
    expect(within(screen.getByTestId('upload-row-0')).getByRole('status')).toHaveAttribute('aria-label', '上传分片 11%');
    expect(within(screen.getByTestId('upload-row-1')).getByRole('status')).toHaveAttribute('aria-label', '上传分片 22%');
    expect(screen.queryByTestId('upload-progress')).not.toBeInTheDocument();

    act(() => finishers[0]?.());
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(6));
    expect(active.size).toBe(5);
    expect(progressByFile.has('狼王06.mp4')).toBe(true);
    expect(progressByFile.has('狼王07.mp4')).toBe(false);

    act(() => finishers[1]?.());
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(7));
    expect(active.size).toBe(5);
    expect(maxActive).toBe(5);

    act(() => finishers.slice(2).forEach((finish) => finish()));
    await waitFor(() => expect(screen.queryByTestId('create-item-upload-rows')).not.toBeInTheDocument());
  });

  it('accepts one hundred episodes in one batch while only five obtain upload slots', async () => {
    const onSubmit = vi.fn(() => new Promise<{ outcome: 'success' }>(() => undefined));
    render(<CreateItemUploadPanel onSubmit={onSubmit} />);

    const files = Array.from({ length: 100 }, (_, index) =>
      new File([String(index)], `第${index + 1}集.mp4`, { type: 'video/mp4' }));
    await userEvent.upload(screen.getByTestId('create-item-file'), files);
    expect(screen.getAllByTestId(/upload-row-\d+$/)).toHaveLength(100);

    await userEvent.click(screen.getByRole('button', { name: '上传 V1' }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(5));
    expect(screen.getByTestId('upload-row-5')).toHaveTextContent('排队中 · 尚未创建上传会话');
    expect(screen.getByTestId('upload-row-99')).toHaveTextContent('排队中 · 尚未创建上传会话');
  });

  it('blocks the entire batch until every row has required metadata', async () => {
    const onSubmit = vi.fn();
    render(<CreateItemUploadPanel onSubmit={onSubmit} />);
    await userEvent.upload(screen.getByTestId('create-item-file'), [
      new File(['a'], '狼王01.mp4', { type: 'video/mp4' }),
      new File(['b'], '狼王_final.mp4', { type: 'video/mp4' }),
    ]);
    expect(screen.getByRole('button', { name: '上传 V1' })).toBeDisabled();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('continues after failed and uncertain rows, reports recovery, and retries only the chosen row', async () => {
    const attempts = new Map<string, number>();
    const onSubmit = vi.fn(async (input: { file: File }) => {
      const count = (attempts.get(input.file.name) ?? 0) + 1;
      attempts.set(input.file.name, count);
      if (input.file.name === '狼王02.mp4' && count === 1) {
        return { outcome: 'failed' as const, message: '上传校验失败' };
      }
      if (input.file.name === '狼王03.mp4' && count === 1) {
        return { outcome: 'uncertain' as const, message: '完成响应丢失，原因未确认' };
      }
      return { outcome: 'success' as const };
    });
    render(<CreateItemUploadPanel onSubmit={onSubmit} />);
    const files = Array.from({ length: 7 }, (_, index) =>
      new File([String(index)], `狼王0${index + 1}.mp4`, { type: 'video/mp4' }));
    await userEvent.upload(screen.getByTestId('create-item-file'), files);
    await userEvent.click(screen.getByRole('button', { name: '上传 V1' }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(7));
    expect(screen.queryByTestId('upload-row-0')).not.toBeInTheDocument();
    expect(screen.getByTestId('upload-row-1')).toHaveTextContent('狼王02.mp4');
    expect(screen.getByTestId('upload-row-1-error')).toHaveTextContent('上传校验失败');
    expect(screen.getByTestId('upload-row-2-error')).toHaveTextContent('结果不确定');
    expect(screen.queryByTestId('upload-row-6')).not.toBeInTheDocument();
    const report = screen.getByTestId('batch-upload-report');
    expect(report).toHaveTextContent('狼王02.mp4');
    expect(report).toHaveTextContent('失败阶段');
    expect(report).toHaveTextContent('恢复方式');
    expect(report).toHaveTextContent('狼王03.mp4');
    expect(report).toHaveTextContent('原因未确认');

    await userEvent.click(within(screen.getByTestId('upload-row-1')).getByRole('button', { name: '重试此项' }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(8));
    expect(onSubmit.mock.calls.at(-1)?.[0].file).toBe(files[1]);
    await waitFor(() => expect(screen.queryByTestId('upload-row-1')).not.toBeInTheDocument());
    expect(screen.getByTestId('upload-row-2')).toBeInTheDocument();
    expect(attempts.get('狼王01.mp4')).toBe(1);

    await userEvent.click(within(screen.getByTestId('upload-row-2')).getByRole('button', { name: '已核对未成功，重试此项' }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(9));
    expect(onSubmit.mock.calls.at(-1)?.[0].file).toBe(files[2]);
    await waitFor(() => expect(screen.queryByTestId('upload-row-2')).not.toBeInTheDocument());
  });

  it('shows appended-version upload progress without adding a reserved progress row', () => {
    render(
      <AppendVersionPanel
        nextLabel="V2"
        pending
        progress={{ stage: 'binding', percent: 95, bytesSent: 95, totalBytes: 100 }}
        onSubmit={vi.fn()}
      />,
    );

    expect(screen.getByRole('button', { name: '上传中...' })).toBeDisabled();
    expect(screen.getByTestId('append-version-file')).toBeDisabled();
    expect(screen.getByTestId('append-version-note')).toBeDisabled();
    expect(screen.getByTestId('append-version-change-summary')).toBeDisabled();
    expect(screen.getByRole('status')).toHaveAttribute('aria-label', '绑定成片记录 95%');
    const css = readFileSync(resolve(process.cwd(), 'src/modules/final-cut-review/styles/fj-review.css'), 'utf8');
    expect(css).not.toContain('"progress progress progress progress"');
    expect(css).not.toMatch(/grid-template-areas:[^;]*"progress"/s);
  });

  it('blocks appended versions when no original file is selected', async () => {
    const onSubmit = vi.fn();
    render(<AppendVersionPanel nextLabel="V2" onSubmit={onSubmit} />);

    await userEvent.click(screen.getByRole('button', { name: '确认追加 V2' }));

    expect(onSubmit).not.toHaveBeenCalled();
    const fileInput = screen.getByTestId('append-version-file');
    const fileError = screen.getByTestId('append-version-file-error');
    expect(fileError).toHaveTextContent('请选择原片文件');
    expect(fileError).toHaveAttribute('id', 'append-version-file-error');
    expect(fileError).toHaveAttribute('role', 'alert');
    expect(fileInput).toHaveAttribute('aria-invalid', 'true');
    expect(fileInput).toHaveAttribute('aria-describedby', 'append-version-file-error');
  });

  it('submits version note and change summary without a request-changes gate', async () => {
    const onSubmit = vi.fn();
    render(<AppendVersionPanel nextLabel="V2" onSubmit={onSubmit} />);
    const file = new File(['v2'], 'v2-upload.mp4', { type: 'video/mp4' });

    await userEvent.upload(screen.getByTestId('append-version-file'), file);
    await userEvent.clear(screen.getByTestId('append-version-note'));
    await userEvent.type(screen.getByTestId('append-version-note'), 'V2 画面补版说明');
    await userEvent.clear(screen.getByTestId('append-version-change-summary'));
    await userEvent.type(screen.getByTestId('append-version-change-summary'), '修正字幕边距和片尾节奏。');
    await userEvent.click(screen.getByRole('button', { name: '确认追加 V2' }));

    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({
        versionNote: 'V2 画面补版说明',
        changeSummary: '修正字幕边距和片尾节奏。',
        file,
      }),
    );
  });

  it('keeps the same appended file and metadata available for a same-page retry', async () => {
    const onSubmit = vi.fn().mockRejectedValueOnce(new Error('响应丢失')).mockResolvedValueOnce(undefined);
    render(<AppendVersionPanel nextLabel="V2" onSubmit={onSubmit} />);
    const file = new File(['v2'], 'retry-v2.mp4', { type: 'video/mp4' });
    const fileInput = screen.getByTestId('append-version-file') as HTMLInputElement;

    await userEvent.upload(fileInput, file);
    await userEvent.clear(screen.getByTestId('append-version-note'));
    await userEvent.type(screen.getByTestId('append-version-note'), '保留的 V2 说明');
    await userEvent.clear(screen.getByTestId('append-version-change-summary'));
    await userEvent.type(screen.getByTestId('append-version-change-summary'), '保留的修改说明');
    await userEvent.click(screen.getByRole('button', { name: '确认追加 V2' }));

    const submissionError = await screen.findByRole('alert');
    expect(submissionError).toHaveTextContent('响应丢失');
    expect(submissionError).toHaveAttribute('id', 'append-version-upload-error');
    expect(fileInput).toHaveAttribute('aria-invalid', 'true');
    expect(fileInput).toHaveAttribute('aria-describedby', 'append-version-upload-error');
    expect(fileInput.files?.[0]).toBe(file);
    expect(screen.getByTestId('append-version-note')).toHaveValue('保留的 V2 说明');
    expect(screen.getByTestId('append-version-change-summary')).toHaveValue('保留的修改说明');

    await userEvent.click(screen.getByRole('button', { name: '确认追加 V2' }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(2));
    expect(onSubmit.mock.calls[0]?.[0].file).toBe(onSubmit.mock.calls[1]?.[0].file);
  });

  it('resets the appended file and metadata when the next version label advances', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    const result = render(<AppendVersionPanel key="V2" nextLabel="V2" onSubmit={onSubmit} />);
    const fileInput = screen.getByTestId('append-version-file') as HTMLInputElement;
    await userEvent.upload(fileInput, new File(['v2'], 'v2.mp4', { type: 'video/mp4' }));
    await userEvent.clear(screen.getByTestId('append-version-note'));
    await userEvent.type(screen.getByTestId('append-version-note'), '旧的 V2 说明');
    result.rerender(<AppendVersionPanel key="V3" nextLabel="V3" onSubmit={onSubmit} />);

    await waitFor(() => expect((screen.getByTestId('append-version-file') as HTMLInputElement).files).toHaveLength(0));
    expect(screen.getByTestId('append-version-note')).toHaveValue('V3 版本说明');
    expect(screen.getByRole('button', { name: '确认追加 V3' })).toBeInTheDocument();
  });
});

describe('ReviewWorkspacePage issue playback', () => {
  it('keeps an appended version successful when the version-list refresh fails', async () => {
    const rendered = renderEditReviewWorkspace();
    const editApi = rendered.runtime.getApi('edit');
    const originalGetWorkspace = editApi.getWorkspace.bind(editApi);
    const appendVersion = vi.spyOn(editApi, 'appendVersion');
    let failWorkspaceRefresh = false;
    vi.spyOn(editApi, 'getWorkspace').mockImplementation((...args) => {
      if (failWorkspaceRefresh) return Promise.reject(new Error('list refresh failed'));
      return originalGetWorkspace(...args);
    });

    expect(await screen.findByRole('button', { name: '确认追加 V3' })).toBeEnabled();
    await userEvent.upload(
      screen.getByTestId('append-version-file'),
      new File(['v3'], '第003集.mp4', { type: 'video/mp4' }),
    );
    failWorkspaceRefresh = true;
    await userEvent.click(screen.getByRole('button', { name: '确认追加 V3' }));

    await waitFor(() => expect(appendVersion).toHaveBeenCalledTimes(1));
    expect(await screen.findByTestId('player-toast', {}, { timeout: 5_000 })).toHaveTextContent(
      '文件已上传成功，待审列表暂时刷新失败，请刷新页面查看。',
    );
    await new Promise((resolvePromise) => window.setTimeout(resolvePromise, 50));
    expect(appendVersion).toHaveBeenCalledTimes(1);
    rendered.unmount();
    cleanupRuntime(rendered.runtime, rendered.queryClient);
  });

  it('polls only the mounted current workspace with the bounded React Query interval', async () => {
    const current = renderReviewWorkspace();
    expect(await screen.findByTestId('review-workspace-frame')).toBeInTheDocument();
    const currentQuery = current.queryClient.getQueryCache().find({
      queryKey: ['fj-review', 'workspace', 'prj_seed_final_cut', 'item_ep28', 'current'],
    });
    const currentPollingOptions = currentQuery?.options as NonNullable<typeof currentQuery>['options'] & {
      refetchInterval?: (query: typeof currentQuery) => number | false;
      refetchIntervalInBackground?: boolean;
    };
    expect(currentPollingOptions.refetchInterval?.(currentQuery)).toBe(2_500);
    expect(currentPollingOptions.refetchIntervalInBackground).toBe(true);
    current.unmount();
    expect(currentQuery?.getObserversCount()).toBe(0);
    cleanupRuntime(current.runtime, current.queryClient);

    const historical = renderReviewWorkspace('/review/projects/prj_seed_final_cut/items/item_ep28?version=ver_ep28_v1');
    expect(await screen.findByTestId('review-workspace-frame')).toBeInTheDocument();
    const historicalQuery = historical.queryClient.getQueryCache().find({
      queryKey: ['fj-review', 'workspace', 'prj_seed_final_cut', 'item_ep28', 'ver_ep28_v1'],
    });
    const historicalPollingOptions = historicalQuery?.options as NonNullable<typeof historicalQuery>['options'] & {
      refetchInterval?: (query: typeof historicalQuery) => number | false;
    };
    expect(historicalPollingOptions.refetchInterval?.(historicalQuery)).toBe(false);
    historical.unmount();
    cleanupRuntime(historical.runtime, historical.queryClient);

    const explicitCurrent = renderReviewWorkspace('/review/projects/prj_seed_final_cut/items/item_ep28?version=ver_ep28_v2');
    expect(await screen.findByTestId('review-workspace-frame')).toBeInTheDocument();
    const explicitCurrentQuery = explicitCurrent.queryClient.getQueryCache().find({
      queryKey: ['fj-review', 'workspace', 'prj_seed_final_cut', 'item_ep28', 'ver_ep28_v2'],
    });
    const explicitCurrentPollingOptions = explicitCurrentQuery?.options as NonNullable<typeof explicitCurrentQuery>['options'] & {
      refetchInterval?: (query: typeof explicitCurrentQuery) => number | false;
    };
    expect(explicitCurrentPollingOptions.refetchInterval?.(explicitCurrentQuery)).toBe(2_500);
    explicitCurrent.unmount();
    cleanupRuntime(explicitCurrent.runtime, explicitCurrent.queryClient);
  });

  it('keeps the current issue draft when the workspace mutation rejects', async () => {
    const runtimeResult = renderReviewWorkspace();
    const reviewApi = runtimeResult.runtime.getApi('review');
    vi.spyOn(reviewApi, 'createIssue').mockRejectedValueOnce(new Error('意见写入失败'));
    await screen.findByTestId('review-player');

    const textarea = screen.getByLabelText('当前版本意见正文');
    await userEvent.clear(textarea);
    await userEvent.type(textarea, '工作台失败后仍需保留的意见');
    await userEvent.click(screen.getByRole('button', { name: '提交意见' }));

    expect(await screen.findByText('意见写入失败')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: '提交意见' })).not.toBeDisabled());
    expect(textarea).toHaveValue('工作台失败后仍需保留的意见');
    cleanupRuntime(runtimeResult.runtime, runtimeResult.queryClient);
  });

  it('toggles the compact version rail and issue drawer without changing desktop testids', async () => {
    const runtimeResult = renderReviewWorkspace();
    await screen.findByTestId('review-player');

    const workbench = screen.getByTestId('review-workspace-frame').querySelector('.fj-review-workbench')!;
    const versionRegion = document.getElementById('review-version-rail-region')!;
    const versionToggle = screen.getByRole('button', { name: '展开版本栏' });
    expect(versionToggle).toHaveAttribute('aria-expanded', 'false');
    expect(versionRegion).not.toHaveClass('is-open');
    expect(screen.getByTestId('version-V2')).toBeInTheDocument();

    await userEvent.click(versionToggle);
    expect(screen.getByRole('button', { name: '折叠版本栏' })).toHaveAttribute('aria-expanded', 'true');
    expect(versionRegion).toHaveClass('is-open');
    expect(workbench).toHaveClass('is-version-rail-open');

    const issueToggle = screen.getByRole('button', { name: '打开意见栏' });
    expect(issueToggle).toHaveAttribute('aria-expanded', 'false');
    await userEvent.click(issueToggle);
    const drawer = screen.getByRole('dialog', { name: '意见反馈' });
    expect(drawer).toHaveClass('is-open');
    expect(screen.getByTestId('issue-panel')).toBeInTheDocument();
    const closeDrawer = within(drawer).getByRole('button', { name: '关闭意见栏' });
    await waitFor(() => expect(closeDrawer).toHaveFocus());
    const drawerFocusables = Array.from(
      drawer.querySelectorAll<HTMLElement>(
        'button:not(:disabled), textarea:not(:disabled), input:not(:disabled), select:not(:disabled), a[href]',
      ),
    );
    const lastDrawerControl = drawerFocusables.at(-1)!;
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true });
    expect(lastDrawerControl).toHaveFocus();
    fireEvent.keyDown(document, { key: 'Tab' });
    expect(closeDrawer).toHaveFocus();
    screen.getByRole('link', { name: '返回项目' }).focus();
    fireEvent.keyDown(document, { key: 'Tab' });
    expect(closeDrawer).toHaveFocus();
    screen.getByRole('link', { name: '返回项目' }).focus();
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true });
    expect(lastDrawerControl).toHaveFocus();
    await userEvent.click(closeDrawer);
    expect(screen.queryByRole('dialog', { name: '意见反馈' })).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: '打开意见栏' })).toHaveFocus());

    await userEvent.click(screen.getByRole('button', { name: '打开意见栏' }));
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('dialog', { name: '意见反馈' })).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: '打开意见栏' })).toHaveFocus());
    cleanupRuntime(runtimeResult.runtime, runtimeResult.queryClient);
  });

  it('keeps the compact issue drawer open while a workspace issue mutation is pending', async () => {
    const runtimeResult = renderReviewWorkspace();
    const reviewApi = runtimeResult.runtime.getApi('review');
    let rejectCreate!: (error: Error) => void;
    vi.spyOn(reviewApi, 'createIssue').mockImplementationOnce(
      () =>
        new Promise((_resolve, reject) => {
          rejectCreate = reject;
        }),
    );
    await screen.findByTestId('review-player');

    const issueToggle = screen.getByRole('button', { name: '打开意见栏' });
    await userEvent.click(issueToggle);
    const drawer = screen.getByRole('dialog', { name: '意见反馈' });
    const closeDrawer = within(drawer).getByRole('button', { name: '关闭意见栏' });
    const createDraft = within(drawer).getByLabelText('当前版本意见正文');
    await userEvent.clear(createDraft);
    await userEvent.type(createDraft, '提交期间不能关闭意见抽屉');
    await userEvent.click(within(drawer).getByRole('button', { name: '提交意见' }));

    await waitFor(() => expect(closeDrawer).toBeDisabled());
    expect(issueToggle).toBeDisabled();
    expect(document.querySelector('.fj-review-issue-drawer-scrim')).toBeDisabled();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.getByRole('dialog', { name: '意见反馈' })).toBeInTheDocument();

    await act(async () => rejectCreate(new Error('意见写入失败')));
    expect(await screen.findByText('意见写入失败')).toBeInTheDocument();
    await waitFor(() => expect(closeDrawer).not.toBeDisabled());
    expect(createDraft).toHaveValue('提交期间不能关闭意见抽屉');
    await waitFor(() => expect(createDraft).toHaveFocus());
    await userEvent.click(closeDrawer);
    expect(screen.queryByRole('dialog', { name: '意见反馈' })).not.toBeInTheDocument();
    cleanupRuntime(runtimeResult.runtime, runtimeResult.queryClient);
  });

  it('propagates pending edit and reply mutations to the compact issue drawer lock', async () => {
    const runtimeResult = renderReviewWorkspace();
    const reviewApi = runtimeResult.runtime.getApi('review');
    let rejectEdit!: (error: Error) => void;
    let rejectReply!: (error: Error) => void;
    vi.spyOn(reviewApi, 'editIssue').mockImplementationOnce(
      () =>
        new Promise((_resolve, reject) => {
          rejectEdit = reject;
        }),
    );
    vi.spyOn(reviewApi, 'replyToIssue').mockImplementationOnce(
      () =>
        new Promise((_resolve, reject) => {
          rejectReply = reject;
        }),
    );
    await screen.findByTestId('review-player');

    const issueToggle = screen.getByRole('button', { name: '打开意见栏' });
    await userEvent.click(issueToggle);
    const drawer = screen.getByRole('dialog', { name: '意见反馈' });
    const closeDrawer = within(drawer).getByRole('button', { name: '关闭意见栏' });
    const issueCard = within(drawer).getByTestId('issue-issue_v2_001');

    await userEvent.click(within(issueCard).getByRole('button', { name: '编辑意见' }));
    const editDraft = within(issueCard).getByLabelText('编辑意见正文');
    await userEvent.clear(editDraft);
    await userEvent.type(editDraft, '抽屉中的待提交编辑');
    await userEvent.click(within(issueCard).getByRole('button', { name: '保存为新修订' }));
    await waitFor(() => expect(closeDrawer).toBeDisabled());
    expect(issueToggle).toBeDisabled();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.getByRole('dialog', { name: '意见反馈' })).toBeInTheDocument();
    await act(async () => rejectEdit(new Error('编辑失败')));
    await waitFor(() => expect(closeDrawer).not.toBeDisabled());
    await waitFor(() => expect(editDraft).toHaveFocus());

    await userEvent.click(within(issueCard).getByRole('button', { name: '回复' }));
    const replyDraft = within(issueCard).getByLabelText('回复意见正文');
    await userEvent.type(replyDraft, '抽屉中的待提交回复');
    await userEvent.click(within(issueCard).getByRole('button', { name: '提交回复' }));
    await waitFor(() => expect(closeDrawer).toBeDisabled());
    expect(document.querySelector('.fj-review-issue-drawer-scrim')).toBeDisabled();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.getByRole('dialog', { name: '意见反馈' })).toBeInTheDocument();
    await act(async () => rejectReply(new Error('回复失败')));
    await waitFor(() => expect(closeDrawer).not.toBeDisabled());
    await waitFor(() => expect(replyDraft).toHaveFocus());
    expect(editDraft).toHaveValue('抽屉中的待提交编辑');
    expect(replyDraft).toHaveValue('抽屉中的待提交回复');

    cleanupRuntime(runtimeResult.runtime, runtimeResult.queryClient);
  });

  it('removes issue drawer modal state when the workspace crosses into desktop width', async () => {
    let compact = true;
    const listeners = new Set<() => void>();
    const mediaQuery = {
      get matches() {
        return compact;
      },
      media: '(max-width: 1279px)',
      addEventListener: (_type: string, listener: () => void) => listeners.add(listener),
      removeEventListener: (_type: string, listener: () => void) => listeners.delete(listener),
    } as unknown as MediaQueryList;
    vi.stubGlobal('matchMedia', vi.fn(() => mediaQuery));
    const runtimeResult = renderReviewWorkspace();
    await screen.findByTestId('review-player');

    await userEvent.click(screen.getByRole('button', { name: '打开意见栏' }));
    expect(screen.getByRole('dialog', { name: '意见反馈' })).toBeInTheDocument();

    await act(async () => {
      compact = false;
      listeners.forEach((listener) => listener());
    });
    expect(screen.queryByRole('dialog', { name: '意见反馈' })).not.toBeInTheDocument();
    expect(document.getElementById('review-issue-drawer')).not.toHaveAttribute('aria-modal');
    expect(document.getElementById('review-issue-drawer')).not.toHaveClass('is-open');
    expect(screen.getByRole('button', { name: '打开意见栏' })).toHaveAttribute('aria-expanded', 'false');

    cleanupRuntime(runtimeResult.runtime, runtimeResult.queryClient);
    vi.unstubAllGlobals();
  });

  it('removes start-review and request-changes gates while keeping current review controls writable', async () => {
    const runtimeResult = renderReviewWorkspace();
    await screen.findByTestId('review-player');
    expect(screen.queryByRole('button', { name: '开始审阅' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '要求修改' })).not.toBeInTheDocument();
    expect(screen.getByTestId('issue-form')).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: '编辑意见' }).length).toBeGreaterThan(0);
    expect(screen.getByTestId('finalize-current')).toBeEnabled();

    cleanupRuntime(runtimeResult.runtime, runtimeResult.queryClient);
  });

  it('renders manual version comparison with dual players and independent issue layers', async () => {
    const runtimeResult = renderReviewWorkspace();

    expect(await screen.findByTestId('version-compare-panel')).toBeInTheDocument();
    expect(screen.getByTestId('version-compare-left')).toHaveTextContent('V1');
    expect(screen.getByTestId('version-compare-right')).toHaveTextContent('V2');
    expect(screen.getByLabelText('左侧版本播放器 V1')).toBeInTheDocument();
    expect(screen.getByLabelText('右侧版本播放器 V2')).toBeInTheDocument();
    expect(screen.getByTestId('version-compare-left-annotation-layer')).toBeInTheDocument();
    expect(screen.getByTestId('version-compare-right-annotation-layer')).toBeInTheDocument();
    expect(within(screen.getByTestId('version-compare-left')).getByText(/#001/)).toBeInTheDocument();
    expect(within(screen.getByTestId('version-compare-right')).getByText(/#002/)).toBeInTheDocument();
    const rightLayer = screen.getByTestId('version-compare-right-annotation-layer');
    expect(rightLayer.querySelector('path[stroke="rgba(0,0,0,0.72)"]')).not.toBeNull();
    expect(rightLayer.querySelector('path[stroke="#ffcc3d"]')).not.toBeNull();
    expect(rightLayer.querySelectorAll('line[stroke="rgba(0,0,0,0.72)"]')).toHaveLength(1);
    expect(rightLayer.querySelectorAll('line[stroke="#ffcc3d"]')).toHaveLength(1);

    cleanupRuntime(runtimeResult.runtime, runtimeResult.queryClient);
  });

  it('keeps the annotation toolbar in the title row above the player and keeps compare in the workspace scroll flow', async () => {
    const runtimeResult = renderReviewWorkspace();

    const scrollRegion = await screen.findByTestId('review-workspace-scroll-region');
    const toolbarDock = await screen.findByTestId('annotation-toolbar-dock');
    const player = await screen.findByTestId('review-player');
    const compare = await screen.findByTestId('version-compare-panel');
    const workspaceFrame = await screen.findByTestId('review-workspace-frame');

    expect(toolbarDock.closest('.fj-review-workspace-head')).not.toBeNull();
    expect(toolbarDock.closest('.fj-review-main-column')).not.toBeNull();
    expect(toolbarDock.closest('.fj-review-player-card')).toBeNull();
    expect(within(toolbarDock).getByTestId('annotation-toolbar')).toBeInTheDocument();
    expect(within(toolbarDock).getByRole('button', { name: '重置草稿' })).toBeInTheDocument();
    expectBefore(toolbarDock, player);
    expect(player.querySelector('.fj-review-toolbar-inline-dock')).toBeNull();
    expect(workspaceFrame.parentElement).toBe(scrollRegion);
    expect(compare.parentElement).toBe(workspaceFrame);
    expect(compare.closest('.fj-review-workbench')).toBeNull();

    cleanupRuntime(runtimeResult.runtime, runtimeResult.queryClient);
  });

  it('keeps redo available after undoing a draft annotation in the full workspace', async () => {
    const runtimeResult = renderReviewWorkspace();

    const player = await screen.findByTestId('review-player');
    const stage = player.querySelector('.fj-review-video-frame') as HTMLDivElement;
    vi.spyOn(stage, 'getBoundingClientRect').mockReturnValue({
      x: 20,
      y: 120,
      left: 20,
      top: 120,
      right: 820,
      bottom: 570,
      width: 800,
      height: 450,
      toJSON: () => ({}),
    } as DOMRect);

    await userEvent.click(screen.getByRole('button', { name: '矩形' }));
    fireEvent.pointerDown(stage, { clientX: 300, clientY: 220, pointerId: 12, pointerType: 'mouse', button: 0 });
    fireEvent.pointerMove(stage, { clientX: 420, clientY: 340, pointerId: 12, pointerType: 'mouse', button: 0 });
    fireEvent.pointerUp(stage, { clientX: 420, clientY: 340, pointerId: 12, pointerType: 'mouse', button: 0 });

    await waitFor(() => expect(screen.getByRole('button', { name: '撤销' })).not.toBeDisabled());
    expect(screen.getByRole('button', { name: '重做' })).toBeDisabled();

    await userEvent.click(screen.getByRole('button', { name: '撤销' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '撤销' })).toBeDisabled());
    expect(screen.getByRole('button', { name: '重做' })).not.toBeDisabled();

    await userEvent.click(screen.getByRole('button', { name: '重做' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '撤销' })).not.toBeDisabled());
    expect(screen.getByRole('button', { name: '重做' })).toBeDisabled();

    await userEvent.click(screen.getByRole('button', { name: '重置草稿' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '撤销' })).toBeDisabled());
    expect(screen.getByRole('button', { name: '重做' })).toBeDisabled();
    expect(player.querySelectorAll('.fj-review-annotation-layer rect')).toHaveLength(0);

    cleanupRuntime(runtimeResult.runtime, runtimeResult.queryClient);
  });

  it('does not show a version mismatch while a newly created current issue waits for workspace refresh', async () => {
    const runtimeResult = renderReviewWorkspace();

    await screen.findByTestId('review-player');
    const issueBody = '新建当前版本意见不能显示跨版本错误';
    await userEvent.clear(screen.getByPlaceholderText('针对当前版本输入意见'));
    await userEvent.type(screen.getByPlaceholderText('针对当前版本输入意见'), issueBody);
    await userEvent.click(screen.getByRole('button', { name: '提交意见' }));

    await waitFor(() =>
      expect(within(screen.getByTestId('issue-panel')).getByText(issueBody)).toBeInTheDocument(),
    );
    expect(screen.queryByText('目标意见不属于当前版本')).not.toBeInTheDocument();
    expect(screen.queryByText('旧回放请求已取消')).not.toBeInTheDocument();

    cleanupRuntime(runtimeResult.runtime, runtimeResult.queryClient);
  });
});

describe('ReviewPlayer component', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('requests cross-origin playback media with the same credential policy as review queries', () => {
    const seed = createSeedData();
    const { container } = render(
      <ReviewPlayer
        version={seed.versions[1]}
        issues={[]}
        selectedAnnotationSet={null}
        onTimeChange={vi.fn()}
        onDraftChange={vi.fn()}
        onSelectIssue={vi.fn()}
        onPlaybackError={vi.fn()}
      />,
    );

    expect(container.querySelector('video')).toHaveAttribute('crossorigin', 'use-credentials');
  });

  it('draws a real draft shape and supports undo/redo', async () => {
    const seed = createSeedData();
    const onDraftChange = vi.fn();
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const { container } = render(
      <ReviewPlayer
        version={seed.versions[1]}
        issues={seed.issues.filter((issue) => issue.versionId === 'ver_ep28_v2')}
        selectedAnnotationSet={null}
        onTimeChange={vi.fn()}
        onDraftChange={onDraftChange}
        onSelectIssue={vi.fn()}
        onPlaybackError={vi.fn()}
      />,
    );

    const stage = container.querySelector('.fj-review-video-frame') as HTMLDivElement;
    vi.spyOn(stage, 'getBoundingClientRect').mockReturnValue({
      x: 0,
      y: 0,
      left: 0,
      top: 0,
      right: 160,
      bottom: 90,
      width: 160,
      height: 90,
      toJSON: () => ({}),
    } as DOMRect);

    await userEvent.click(screen.getByRole('button', { name: '矩形' }));
    fireEvent.pointerDown(stage, { clientX: 40, clientY: 20, pointerId: 1 });
    fireEvent.pointerMove(stage, { clientX: 80, clientY: 50, pointerId: 1 });
    fireEvent.pointerUp(stage, { clientX: 80, clientY: 50, pointerId: 1 });

    await waitFor(() => expect(onDraftChange).toHaveBeenLastCalledWith([expect.objectContaining({ tool: 'rect' })]));
    expect(
      consoleError.mock.calls.some((call) => call.some((part) => String(part).includes('Encountered two children with the same key'))),
    ).toBe(false);
    await userEvent.click(screen.getByRole('button', { name: '撤销' }));
    await waitFor(() => expect(onDraftChange).toHaveBeenLastCalledWith([]));
    expect(screen.getByRole('button', { name: '撤销' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '重做' })).not.toBeDisabled();
    await userEvent.click(screen.getByRole('button', { name: '重做' }));
    await waitFor(() => expect(onDraftChange).toHaveBeenLastCalledWith([expect.objectContaining({ tool: 'rect' })]));
    expect(screen.getByRole('button', { name: '撤销' })).not.toBeDisabled();
    expect(screen.getByRole('button', { name: '重做' })).toBeDisabled();
  });

  it('uses a custom color for newly drawn annotation shapes', async () => {
    const seed = createSeedData();
    const onDraftChange = vi.fn();
    const { container } = render(
      <ReviewPlayer
        version={seed.versions[1]}
        issues={seed.issues.filter((issue) => issue.versionId === 'ver_ep28_v2')}
        selectedAnnotationSet={null}
        onTimeChange={vi.fn()}
        onDraftChange={onDraftChange}
        onSelectIssue={vi.fn()}
        onPlaybackError={vi.fn()}
      />,
    );

    const stage = container.querySelector('.fj-review-video-frame') as HTMLDivElement;
    vi.spyOn(stage, 'getBoundingClientRect').mockReturnValue({
      x: 0,
      y: 0,
      left: 0,
      top: 0,
      right: 160,
      bottom: 90,
      width: 160,
      height: 90,
      toJSON: () => ({}),
    } as DOMRect);

    fireEvent.change(screen.getByLabelText('自定义颜色'), { target: { value: '#123456' } });
    await userEvent.click(screen.getByRole('button', { name: '矩形' }));
    fireEvent.pointerDown(stage, { clientX: 40, clientY: 20, pointerId: 1 });
    fireEvent.pointerMove(stage, { clientX: 80, clientY: 50, pointerId: 1 });
    fireEvent.pointerUp(stage, { clientX: 80, clientY: 50, pointerId: 1 });

    await waitFor(() => expect(onDraftChange).toHaveBeenLastCalledWith([expect.objectContaining({ color: '#123456', tool: 'rect' })]));
  });

  it('edits text annotation content and font size without creating duplicates', async () => {
    const seed = createSeedData();
    const onDraftChange = vi.fn();
    const { container } = render(
      <ReviewPlayer
        version={seed.versions[1]}
        issues={seed.issues.filter((issue) => issue.versionId === 'ver_ep28_v2')}
        selectedAnnotationSet={null}
        onTimeChange={vi.fn()}
        onDraftChange={onDraftChange}
        onSelectIssue={vi.fn()}
        onPlaybackError={vi.fn()}
      />,
    );

    const stage = container.querySelector('.fj-review-video-frame') as HTMLDivElement;
    vi.spyOn(stage, 'getBoundingClientRect').mockReturnValue({
      x: 0,
      y: 0,
      left: 0,
      top: 0,
      right: 160,
      bottom: 90,
      width: 160,
      height: 90,
      toJSON: () => ({}),
    } as DOMRect);

    await userEvent.click(screen.getByRole('button', { name: '文字' }));
    expect(screen.queryByLabelText('线宽')).not.toBeInTheDocument();
    expect(screen.getByLabelText('文字字号')).toBeEnabled();
    fireEvent.pointerDown(stage, { clientX: 40, clientY: 20, pointerId: 1 });

    const editor = await screen.findByLabelText('文字批注内容');
    fireEvent.change(editor, { target: { value: '字幕边距' } });
    await waitFor(() =>
      expect(onDraftChange).toHaveBeenLastCalledWith([
        expect.objectContaining({ fontSize: 32, text: '字幕边距', tool: 'text' }),
      ]),
    );

    fireEvent.change(screen.getByLabelText('文字字号'), { target: { value: '48' } });
    await waitFor(() =>
      expect(onDraftChange).toHaveBeenLastCalledWith([
        expect.objectContaining({ fontSize: 48, text: '字幕边距', tool: 'text' }),
      ]),
    );

    const callsBeforeSelectingExistingText = onDraftChange.mock.calls.length;
    fireEvent.pointerDown(stage, { clientX: 42, clientY: 20, pointerId: 2 });
    expect(onDraftChange).toHaveBeenCalledTimes(callsBeforeSelectingExistingText);

    await userEvent.click(screen.getByRole('button', { name: '箭头' }));
    expect(screen.queryByTestId('text-annotation-editor')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('文字字号')).not.toBeInTheDocument();
    expect(screen.getByLabelText('线宽')).toBeEnabled();
  });

  it('keeps the text editor inside the video frame with clamped absolute geometry', async () => {
    const seed = createSeedData();
    const version = { ...seed.versions[1], width: 100, height: 100 };
    const onDraftChange = vi.fn();
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      if (this.classList.contains('fj-review-video-frame')) {
        return {
          x: 0,
          y: 0,
          left: 0,
          top: 0,
          right: 200,
          bottom: 100,
          width: 200,
          height: 100,
          toJSON: () => ({}),
        } as DOMRect;
      }
      return {
        x: 0,
        y: 0,
        left: 0,
        top: 0,
        right: 0,
        bottom: 0,
        width: 0,
        height: 0,
        toJSON: () => ({}),
      } as DOMRect;
    });
    const { container, unmount } = render(
      <ReviewPlayer
        version={version}
        issues={[]}
        selectedAnnotationSet={null}
        onTimeChange={vi.fn()}
        onDraftChange={onDraftChange}
        onSelectIssue={vi.fn()}
        onPlaybackError={vi.fn()}
      />,
    );
    const stage = container.querySelector('.fj-review-video-frame') as HTMLDivElement;

    await userEvent.click(screen.getByRole('button', { name: '文字' }));
    fireEvent.pointerDown(stage, { clientX: 149, clientY: 99, pointerId: 1 });

    const layer = await screen.findByTestId('text-annotation-editor-layer');
    const editor = screen.getByTestId('text-annotation-editor');
    expect(layer.parentElement).toBe(stage);
    expect(layer).toContainElement(editor);
    expect(layer).toHaveStyle({ overflow: 'hidden', position: 'absolute' });
    expect(layer).toHaveStyle({ height: '100px', left: '50px', top: '0px', width: '100px' });
    expect(editor).toHaveStyle({ position: 'absolute' });

    const frameWidth = Number.parseFloat(layer.style.width);
    const frameHeight = Number.parseFloat(layer.style.height);
    const editorLeft = Number.parseFloat(editor.style.left);
    const editorTop = Number.parseFloat(editor.style.top);
    const editorWidth = Number.parseFloat(editor.style.width);
    const editorHeight = Number.parseFloat(editor.style.height);
    expect(editorLeft).toBeGreaterThanOrEqual(0);
    expect(editorTop).toBeGreaterThanOrEqual(0);
    expect(editorLeft + editorWidth).toBeLessThanOrEqual(frameWidth);
    expect(editorTop + editorHeight).toBeLessThanOrEqual(frameHeight);

    await waitFor(() => expect(onDraftChange).toHaveBeenCalledWith([expect.objectContaining({ tool: 'text' })]));
    const draftShape = onDraftChange.mock.calls.at(-1)?.[0][0];
    if (!draftShape?.points?.[0]) throw new Error('文字批注测试草稿缺少坐标');
    draftShape.points[0] = { x: Number.POSITIVE_INFINITY, y: Number.NaN };
    fireEvent.change(screen.getByLabelText('文字批注内容'), { target: { value: '异常坐标仍需约束' } });
    expect(Number.parseFloat(editor.style.left)).toBe(0);
    expect(Number.parseFloat(editor.style.top)).toBe(0);
    expect(Number.isFinite(Number.parseFloat(editor.style.width))).toBe(true);
    expect(Number.isFinite(Number.parseFloat(editor.style.height))).toBe(true);

    const css = readFileSync(resolve(process.cwd(), 'src/modules/final-cut-review/styles/fj-review.css'), 'utf8');
    expect(css).toMatch(/\.fj-review-text-annotation-layer\s*\{[^}]*contain:\s*layout paint;[^}]*overflow:\s*hidden;[^}]*position:\s*absolute;/s);
    expect(css).not.toContain('width: min(260px, 30vw)');
    unmount();
    expect(screen.queryByTestId('text-annotation-editor')).not.toBeInTheDocument();
  });

  it('keeps dormant responsive drawer controls hidden despite the generic button display rule', () => {
    const css = readFileSync(resolve(process.cwd(), 'src/modules/final-cut-review/styles/fj-review.css'), 'utf8');

    expect(css).toMatch(
      /\.fj-review-responsive-controls,\s*\.fj-review-root \.fj-review-issue-drawer-close,\s*\.fj-review-root \.fj-review-issue-drawer-scrim\s*\{\s*display:\s*none;/s,
    );
    expect(css).toMatch(
      /@media \(max-width:\s*1279px\)[\s\S]*?\.fj-review-root \.fj-review-issue-drawer-close\s*\{[\s\S]*?display:\s*inline-flex;/,
    );
    expect(css).toMatch(
      /@media \(max-width:\s*1279px\)[\s\S]*?\.fj-review-root \.fj-review-issue-drawer-scrim\s*\{[\s\S]*?display:\s*block;/,
    );
  });

  it('closes text editing on submit, cancel, and version switch while retaining the draft', async () => {
    const seed = createSeedData();
    const onDraftChange = vi.fn();
    const commonProps = {
      issues: [],
      selectedAnnotationSet: null,
      onTimeChange: vi.fn(),
      onDraftChange,
      onSelectIssue: vi.fn(),
      onPlaybackError: vi.fn(),
    };
    const { container, rerender } = render(<ReviewPlayer version={seed.versions[1]} {...commonProps} />);
    const stage = container.querySelector('.fj-review-video-frame') as HTMLDivElement;
    vi.spyOn(stage, 'getBoundingClientRect').mockReturnValue({
      x: 0,
      y: 0,
      left: 0,
      top: 0,
      right: 160,
      bottom: 90,
      width: 160,
      height: 90,
      toJSON: () => ({}),
    } as DOMRect);

    await userEvent.click(screen.getByRole('button', { name: '文字' }));
    fireEvent.pointerDown(stage, { clientX: 40, clientY: 20, pointerId: 1 });
    let input = await screen.findByLabelText('文字批注内容');
    fireEvent.change(input, { target: { value: '保留的文字批注' } });
    fireEvent.compositionStart(input);
    fireEvent.keyDown(input, { key: 'Enter', isComposing: true });
    fireEvent.submit(screen.getByTestId('text-annotation-editor'));
    expect(screen.getByTestId('text-annotation-editor')).toBeInTheDocument();
    fireEvent.compositionEnd(input);
    fireEvent.change(input, { target: { value: '保留的中文组合输入' } });
    fireEvent.submit(screen.getByTestId('text-annotation-editor'));
    expect(screen.queryByTestId('text-annotation-editor')).not.toBeInTheDocument();
    expect(onDraftChange).toHaveBeenLastCalledWith([
      expect.objectContaining({ text: '保留的中文组合输入', tool: 'text' }),
    ]);

    fireEvent.pointerDown(stage, { clientX: 40, clientY: 20, pointerId: 2 });
    input = await screen.findByLabelText('文字批注内容');
    fireEvent.keyDown(input, { key: 'Escape' });
    expect(screen.queryByTestId('text-annotation-editor')).not.toBeInTheDocument();

    fireEvent.pointerDown(stage, { clientX: 40, clientY: 20, pointerId: 3 });
    expect(await screen.findByTestId('text-annotation-editor')).toBeInTheDocument();
    rerender(<ReviewPlayer version={seed.versions[0]} {...commonProps} />);
    expect(screen.queryByTestId('text-annotation-editor')).not.toBeInTheDocument();
  });

  it('closes text editing when the final cut changes without a remount and when review exits', async () => {
    const seed = createSeedData();
    const onDraftChange = vi.fn();
    function FinalCutHarness() {
      const [itemId, setItemId] = useState('item-a');
      const version =
        itemId === 'item-a'
          ? seed.versions[1]
          : { ...seed.versions[1], reviewItemId: 'item-b' };
      return (
        <>
          <button type="button" onClick={() => setItemId((current) => current === 'item-a' ? 'item-b' : 'item-a')}>
            切换成片
          </button>
          <ReviewPlayer
            version={version}
            issues={[]}
            selectedAnnotationSet={null}
            onTimeChange={vi.fn()}
            onDraftChange={onDraftChange}
            onSelectIssue={vi.fn()}
            onPlaybackError={vi.fn()}
          />
        </>
      );
    }

    const result = render(<FinalCutHarness />);
    const stage = result.container.querySelector('.fj-review-video-frame') as HTMLDivElement;
    vi.spyOn(stage, 'getBoundingClientRect').mockReturnValue({
      x: 0,
      y: 0,
      left: 0,
      top: 0,
      right: 160,
      bottom: 90,
      width: 160,
      height: 90,
      toJSON: () => ({}),
    } as DOMRect);
    await userEvent.click(screen.getByRole('button', { name: '文字' }));
    fireEvent.pointerDown(stage, { clientX: 40, clientY: 20, pointerId: 1 });
    expect(await screen.findByTestId('text-annotation-editor')).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: '切换成片' }));
    expect(screen.queryByTestId('text-annotation-editor')).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: '切换成片' }));
    await waitFor(() => expect(screen.queryByTestId('text-annotation-editor')).not.toBeInTheDocument());
    const callbackCountBeforeExit = onDraftChange.mock.calls.length;
    result.unmount();
    await Promise.resolve();
    expect(onDraftChange).toHaveBeenCalledTimes(callbackCountBeforeExit);
  });

  it('renders arrow drafts with a visible halo and enlarged arrow head', async () => {
    const seed = createSeedData();
    const onDraftChange = vi.fn();
    const { container } = render(
      <ReviewPlayer
        version={seed.versions[1]}
        issues={seed.issues.filter((issue) => issue.versionId === 'ver_ep28_v2')}
        selectedAnnotationSet={null}
        onTimeChange={vi.fn()}
        onDraftChange={onDraftChange}
        onSelectIssue={vi.fn()}
        onPlaybackError={vi.fn()}
      />,
    );

    const stage = container.querySelector('.fj-review-video-frame') as HTMLDivElement;
    vi.spyOn(stage, 'getBoundingClientRect').mockReturnValue({
      x: 0,
      y: 0,
      left: 0,
      top: 0,
      right: 160,
      bottom: 90,
      width: 160,
      height: 90,
      toJSON: () => ({}),
    } as DOMRect);

    await userEvent.click(screen.getByRole('button', { name: '箭头' }));
    fireEvent.pointerDown(stage, { clientX: 30, clientY: 20, pointerId: 1 });
    fireEvent.pointerMove(stage, { clientX: 130, clientY: 60, pointerId: 1 });
    fireEvent.pointerUp(stage, { clientX: 130, clientY: 60, pointerId: 1 });

    await waitFor(() => expect(onDraftChange).toHaveBeenLastCalledWith([expect.objectContaining({ tool: 'arrow' })]));
    const haloLine = container.querySelector('.fj-review-draft-layer line[stroke="rgba(0,0,0,0.72)"]');
    const arrowHead = container.querySelector('.fj-review-draft-layer path[stroke="#57e3d2"]');
    expect(haloLine).not.toBeNull();
    expect(Number(haloLine?.getAttribute('stroke-width'))).toBeGreaterThan(6);
    expect(arrowHead?.getAttribute('d')?.split(' L ')).toHaveLength(3);
  });

  it('blocks annotation drafts when the player is readonly', async () => {
    const seed = createSeedData();
    const onDraftChange = vi.fn();
    const { container } = render(
      <ReviewPlayer
        version={seed.versions[1]}
        issues={seed.issues.filter((issue) => issue.versionId === 'ver_ep28_v2')}
        selectedAnnotationSet={null}
        annotationReadonlyReason="当前版本已定稿冻结"
        onTimeChange={vi.fn()}
        onDraftChange={onDraftChange}
        onSelectIssue={vi.fn()}
        onPlaybackError={vi.fn()}
      />,
    );

    const rectButton = screen.getByRole('button', { name: '矩形' });
    expect(rectButton).toBeDisabled();
    const stage = container.querySelector('.fj-review-video-frame') as HTMLDivElement;
    expect(stage).toHaveAttribute('data-annotation-readonly', 'true');
    vi.spyOn(stage, 'getBoundingClientRect').mockReturnValue({
      x: 0,
      y: 0,
      left: 0,
      top: 0,
      right: 160,
      bottom: 90,
      width: 160,
      height: 90,
      toJSON: () => ({}),
    } as DOMRect);

    fireEvent.keyDown(document, { key: '2' });
    fireEvent.pointerDown(stage, { clientX: 40, clientY: 20, pointerId: 1 });
    fireEvent.pointerMove(stage, { clientX: 80, clientY: 50, pointerId: 1 });
    fireEvent.pointerUp(stage, { clientX: 80, clientY: 50, pointerId: 1 });

    expect(screen.getByRole('button', { name: '撤销' })).toBeDisabled();
    expect(
      onDraftChange.mock.calls.some(([shapes]) =>
        Array.isArray(shapes) && shapes.some((shape: { tool?: string }) => shape.tool === 'rect'),
      ),
    ).toBe(false);
  });

  it('positions saved annotations over the contained video rectangle and exposes required controls', async () => {
    const seed = createSeedData();
    let resizeCallback: ResizeObserverCallback | null = null;
    vi.stubGlobal(
      'ResizeObserver',
      class {
        constructor(callback: ResizeObserverCallback) {
          resizeCallback = callback;
        }
        observe() {
          return undefined;
        }
        disconnect() {
          return undefined;
        }
      },
    );

    const { container } = render(
      <ReviewPlayer
        version={{
          ...seed.versions[1],
          width: 1080,
          height: 1920,
          originalMedia: { ...seed.versions[1].originalMedia, width: 1080, height: 1920 },
        }}
        issues={seed.issues.filter((issue) => issue.versionId === 'ver_ep28_v2')}
        selectedAnnotationSet={seed.issues.find((issue) => issue.issueId === 'issue_v2_001')?.currentAnnotationSet ?? null}
        selectedIssueId="issue_v2_001"
        onTimeChange={vi.fn()}
        onDraftChange={vi.fn()}
        onSelectIssue={vi.fn()}
        onPlaybackError={vi.fn()}
      />,
    );

    const stage = container.querySelector('.fj-review-video-frame') as HTMLDivElement;
    vi.spyOn(stage, 'getBoundingClientRect').mockReturnValue({
      x: 0,
      y: 0,
      left: 0,
      top: 0,
      right: 160,
      bottom: 90,
      width: 160,
      height: 90,
      toJSON: () => ({}),
    } as DOMRect);
    act(() => {
      resizeCallback?.([], {} as ResizeObserver);
    });

    const layer = await screen.findByTestId('saved-annotation-layer');
    const video = container.querySelector('video') as HTMLVideoElement;
    await waitFor(() => expect(layer).toHaveStyle({ left: '54.6875px', width: '50.625px', height: '90px' }));
    expect(video).toHaveStyle({ left: '54.6875px', width: '50.625px', height: '90px' });
    expect(layer).toHaveAttribute('viewBox', '0 0 1080 1920');
    expect(screen.getByRole('option', { name: '0.75x' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '适应窗口' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: '原始比例' })).toHaveAttribute('aria-pressed', 'false');
    expect(screen.getByRole('button', { name: '全屏' })).toBeInTheDocument();

    fireEvent.keyDown(document, { key: 'ArrowRight' });
    await waitFor(() => expect(screen.getByTestId('current-frame')).toHaveTextContent('1'));
  });

  it('resizes the player frame and annotation viewBox from real loaded video metadata', async () => {
    const seed = createSeedData();
    const { container } = render(
      <ReviewPlayer
        version={{
          ...seed.versions[1],
          width: 1920,
          height: 1080,
          originalMedia: { ...seed.versions[1].originalMedia, width: 1920, height: 1080 },
        }}
        issues={[]}
        selectedAnnotationSet={null}
        onTimeChange={vi.fn()}
        onDraftChange={vi.fn()}
        onSelectIssue={vi.fn()}
        onPlaybackError={vi.fn()}
      />,
    );

    const frame = container.querySelector('.fj-review-video-frame') as HTMLDivElement;
    const video = container.querySelector('video') as HTMLVideoElement;
    expect(frame).toBeInTheDocument();

    Object.defineProperty(video, 'videoWidth', { configurable: true, value: 3840 });
    Object.defineProperty(video, 'videoHeight', { configurable: true, value: 2160 });
    fireEvent.loadedMetadata(video);

    await waitFor(() => expect(screen.getByTestId('saved-annotation-layer')).toHaveAttribute('viewBox', '0 0 3840 2160'));
  });

  it('uses loaded playback metadata for the visible media rectangle even when it differs from stored version dimensions', async () => {
    const seed = createSeedData();
    const playerRef = createRef<ReviewPlayerHandle>();
    let resizeCallback: ResizeObserverCallback | null = null;
    vi.stubGlobal(
      'ResizeObserver',
      class {
        constructor(callback: ResizeObserverCallback) {
          resizeCallback = callback;
        }
        observe() {
          return undefined;
        }
        disconnect() {
          return undefined;
        }
      },
    );
    const { container } = render(
      <ReviewPlayer
        ref={playerRef}
        version={seed.versions[1]}
        issues={[]}
        selectedAnnotationSet={null}
        onTimeChange={vi.fn()}
        onDraftChange={vi.fn()}
        onSelectIssue={vi.fn()}
        onPlaybackError={vi.fn()}
      />,
    );

    const stage = container.querySelector('.fj-review-video-frame') as HTMLDivElement;
    const video = container.querySelector('video') as HTMLVideoElement;
    vi.spyOn(stage, 'getBoundingClientRect').mockReturnValue({
      x: 0,
      y: 0,
      left: 0,
      top: 0,
      right: 2100,
      bottom: 900,
      width: 2100,
      height: 900,
      toJSON: () => ({}),
    } as DOMRect);
    act(() => {
      resizeCallback?.([], {} as ResizeObserver);
    });

    Object.defineProperty(video, 'videoWidth', { configurable: true, value: 90 });
    Object.defineProperty(video, 'videoHeight', { configurable: true, value: 160 });
    fireEvent.loadedMetadata(video);

    const layer = await screen.findByTestId('saved-annotation-layer');
    await waitFor(() => expect(layer).toHaveAttribute('viewBox', '0 0 90 160'));
    await waitFor(() => expect(layer).toHaveStyle({ left: '796.875px', top: '0px', width: '506.25px', height: '900px' }));
    expect(video).toHaveStyle({ left: '796.875px', top: '0px', width: '506.25px', height: '900px' });
    expect(playerRef.current?.snapshot()).toEqual(expect.objectContaining({ videoWidth: 90, videoHeight: 160 }));

    await userEvent.click(screen.getByRole('button', { name: '原始比例' }));
    await waitFor(() => expect(layer).toHaveStyle({ left: '1005px', top: '370px', width: '90px', height: '160px' }));
    expect(video).toHaveStyle({ left: '1005px', top: '370px', width: '90px', height: '160px' });
    expect(screen.getByRole('button', { name: '原始比例' })).toHaveAttribute('aria-pressed', 'true');

    await userEvent.click(screen.getByRole('button', { name: '适应窗口' }));
    await waitFor(() => expect(layer).toHaveStyle({ left: '796.875px', top: '0px', width: '506.25px', height: '900px' }));
    expect(video).toHaveStyle({ left: '796.875px', top: '0px', width: '506.25px', height: '900px' });
    expect(screen.getByRole('button', { name: '适应窗口' })).toHaveAttribute('aria-pressed', 'true');
  });

  it('submits the visible timecode input value even when state change was not dispatched', async () => {
    const seed = createSeedData();
    const onTimeChange = vi.fn();
    render(
      <ReviewPlayer
        version={seed.versions[1]}
        issues={seed.issues.filter((issue) => issue.versionId === 'ver_ep28_v2')}
        selectedAnnotationSet={null}
        onTimeChange={onTimeChange}
        onDraftChange={vi.fn()}
        onSelectIssue={vi.fn()}
        onPlaybackError={vi.fn()}
      />,
    );

    const input = screen.getByLabelText('时间码输入') as HTMLInputElement;
    input.value = '00:00:00:05';
    await userEvent.click(screen.getByRole('button', { name: '跳转' }));

    await waitFor(() => expect(screen.getByTestId('current-frame')).toHaveTextContent('5'));
    expect(screen.getByTestId('current-timecode')).toHaveTextContent('00:00:00:05');
    expect(screen.getByTestId('current-timecode')).toHaveClass('fj-review-sr-only');
    expect(screen.getByTestId('current-frame')).toHaveClass('fj-review-sr-only');
    expect(screen.getByTestId('duration-timecode')).toHaveClass('fj-review-sr-only');
    expect(screen.getByTestId('current-timecode').tagName).toBe('SPAN');
    expect(onTimeChange).toHaveBeenLastCalledWith(200);
  });

  it('seeks timecode against loaded metadata duration instead of stale version duration', async () => {
    const seed = createSeedData();
    const onTimeChange = vi.fn();
    const { container } = render(
      <ReviewPlayer
        version={{ ...seed.versions[1], durationMs: 480 }}
        issues={seed.issues.filter((issue) => issue.versionId === 'ver_ep28_v2')}
        selectedAnnotationSet={null}
        onTimeChange={onTimeChange}
        onDraftChange={vi.fn()}
        onSelectIssue={vi.fn()}
        onPlaybackError={vi.fn()}
      />,
    );
    const video = container.querySelector('video') as HTMLVideoElement;
    Object.defineProperty(video, 'duration', { configurable: true, value: 843 });
    fireEvent.loadedMetadata(video);

    const input = screen.getByLabelText('时间码输入') as HTMLInputElement;
    input.value = '00:00:05:00';
    await userEvent.click(screen.getByRole('button', { name: '跳转' }));

    await waitFor(() => expect(screen.getByTestId('current-timecode')).toHaveTextContent('00:00:05:00'));
    expect(screen.getByTestId('current-frame')).toHaveTextContent('125');
    expect(onTimeChange).toHaveBeenLastCalledWith(5000);
  });

  it('seeks continuously from pointer dragging on the visible timeline track', async () => {
    const seed = createSeedData();
    const onTimeChange = vi.fn();
    render(
      <ReviewPlayer
        version={{
          ...seed.versions[1],
          durationMs: 12000,
          originalMedia: { ...seed.versions[1].originalMedia, durationMs: 12000 },
        }}
        issues={seed.issues.filter((issue) => issue.versionId === 'ver_ep28_v2')}
        selectedAnnotationSet={null}
        onTimeChange={onTimeChange}
        onDraftChange={vi.fn()}
        onSelectIssue={vi.fn()}
        onPlaybackError={vi.fn()}
      />,
    );

    const timeline = screen.getByLabelText('视频时间轴').closest('label') as HTMLLabelElement;
    const makeTimelineRect = (left: number, width: number) =>
      ({
        x: left,
        y: 20,
        left,
        top: 20,
        right: left + width,
        bottom: 36,
        width,
        height: 16,
        toJSON: () => ({}),
      }) as DOMRect;
    vi.spyOn(timeline, 'getBoundingClientRect')
      .mockReturnValueOnce(makeTimelineRect(100, 400))
      .mockReturnValueOnce(makeTimelineRect(200, 200))
      .mockReturnValueOnce(makeTimelineRect(100, 400));
    Object.defineProperty(timeline, 'setPointerCapture', { configurable: true, value: vi.fn() });
    Object.defineProperty(timeline, 'hasPointerCapture', { configurable: true, value: vi.fn(() => true) });
    Object.defineProperty(timeline, 'releasePointerCapture', { configurable: true, value: vi.fn() });

    fireEvent.pointerDown(timeline, { clientX: 100, pointerId: 7, pointerType: 'mouse', button: 0 });
    fireEvent.pointerMove(timeline, { clientX: 300, pointerId: 7, pointerType: 'mouse', button: 0 });
    fireEvent.pointerUp(timeline, { clientX: 500, pointerId: 7, pointerType: 'mouse', button: 0 });

    await waitFor(() => expect(onTimeChange).toHaveBeenLastCalledWith(12000));
    expect(onTimeChange.mock.calls.map(([value]) => value).slice(-3)).toEqual([0, 6000, 12000]);
    expect(screen.getByLabelText('视频时间轴')).toHaveValue('12000');
    expect(timeline.releasePointerCapture).toHaveBeenCalledWith(7);
  });

  it('keeps opinion avatar hit targets below the scrubber and seeks/selects by click or keyboard', async () => {
    const seed = createSeedData();
    const baseIssue = seed.issues.find((issue) => issue.versionId === 'ver_ep28_v2')!;
    const resolvedIssue = {
      ...baseIssue,
      issueId: 'issue_avatar_resolved',
      issueNo: 99,
      status: 'resolved' as const,
      timestampMs: baseIssue.timestampMs + 80,
    };
    const startIssue = {
      ...baseIssue,
      issueId: 'issue_avatar_start',
      issueNo: 97,
      timestampMs: 0,
    };
    const endIssue = {
      ...baseIssue,
      issueId: 'issue_avatar_end',
      issueNo: 98,
      timestampMs: seed.versions[1].durationMs,
    };
    const onTimeChange = vi.fn();
    const onSelectIssue = vi.fn();
    render(
      <ReviewPlayer
        version={seed.versions[1]}
        issues={[baseIssue, resolvedIssue, startIssue, endIssue]}
        selectedAnnotationSet={null}
        onTimeChange={onTimeChange}
        onDraftChange={vi.fn()}
        onSelectIssue={onSelectIssue}
        onPlaybackError={vi.fn()}
      />,
    );

    const scrubber = screen.getByTestId('video-edge-timeline');
    const track = screen.getByTestId('opinion-avatar-track');
    const openMarker = screen.getByRole('button', { name: `意见 #${baseIssue.issueNo} 未修改` });
    const resolvedMarker = screen.getByRole('button', { name: '意见 #99 已修改' });
    expect(scrubber).not.toContainElement(openMarker);
    expect(track).toContainElement(openMarker);
    expect(track).toContainElement(resolvedMarker);
    expect(openMarker.querySelector('svg')).toBeInTheDocument();
    expect(openMarker).toHaveClass('is-open');
    expect(resolvedMarker).toHaveClass('is-resolved');
    expect(openMarker.tagName).toBe('BUTTON');
    expect(openMarker).toHaveAttribute('type', 'button');
    expect(openMarker.tabIndex).toBe(0);
    expect(screen.getByRole('button', { name: '意见 #97 未修改' })).toHaveStyle({
      left: 'clamp(22px, 0%, calc(100% - 22px))',
    });
    expect(screen.getByRole('button', { name: '意见 #98 未修改' })).toHaveStyle({
      left: 'clamp(22px, 100%, calc(100% - 22px))',
    });

    await userEvent.click(openMarker);
    expect(onTimeChange).toHaveBeenLastCalledWith(baseIssue.timestampMs);
    expect(onSelectIssue).toHaveBeenLastCalledWith(baseIssue);
    openMarker.focus();
    await userEvent.keyboard('{Enter}');
    expect(onSelectIssue).toHaveBeenCalledTimes(2);
    await userEvent.keyboard(' ');
    expect(onTimeChange).toHaveBeenLastCalledWith(baseIssue.timestampMs);
    expect(onSelectIssue).toHaveBeenCalledTimes(3);

    const css = readFileSync(resolve(process.cwd(), 'src/modules/final-cut-review/styles/fj-review.css'), 'utf8');
    expect(css).toMatch(/\.fj-review-opinion-track\s*\{[^}]*min-height:\s*44px;/s);
    expect(css).toMatch(/\.fj-review-timeline-marker\s*\{[^}]*height:\s*44px;[^}]*width:\s*44px;/s);
    expect(css).toMatch(/\.fj-review-player-card\s*\{[^}]*grid-template-rows:\s*minmax\(0,\s*1fr\)\s+auto\s+auto;/s);
  });

  it('keeps a user pause benign when it interrupts a pending play request before repeated seeks', async () => {
    const seed = createSeedData();
    const onPlaybackError = vi.fn();
    const onTimeChange = vi.fn();
    let rejectPlay!: (reason?: unknown) => void;
    const pendingPlay = new Promise<void>((_resolve, reject) => {
      rejectPlay = reject;
    });
    const { container } = render(
      <ReviewPlayer
        version={{
          ...seed.versions[1],
          durationMs: 12000,
          originalMedia: { ...seed.versions[1].originalMedia, durationMs: 12000 },
        }}
        issues={seed.issues.filter((issue) => issue.versionId === 'ver_ep28_v2')}
        selectedAnnotationSet={null}
        onTimeChange={onTimeChange}
        onDraftChange={vi.fn()}
        onSelectIssue={vi.fn()}
        onPlaybackError={onPlaybackError}
      />,
    );
    const player = screen.getByTestId('review-player');
    const video = container.querySelector('video') as HTMLVideoElement;
    let paused = true;
    Object.defineProperty(video, 'paused', { configurable: true, get: () => paused });
    Object.defineProperty(video, 'currentTime', { configurable: true, writable: true, value: 0 });
    Object.defineProperty(video, 'play', {
      configurable: true,
      value: vi.fn(() => {
        paused = false;
        return pendingPlay;
      }),
    });
    Object.defineProperty(video, 'pause', {
      configurable: true,
      value: vi.fn(() => {
        paused = true;
      }),
    });

    await userEvent.click(screen.getByRole('button', { name: '播放' }));
    fireEvent.play(video);
    await waitFor(() => expect(screen.getByRole('button', { name: '暂停' })).toBeInTheDocument());
    await userEvent.click(screen.getByRole('button', { name: '暂停' }));
    fireEvent.pause(video);
    fireEvent.change(screen.getByLabelText('视频时间轴'), { target: { value: '3000' } });
    fireEvent.change(screen.getByLabelText('视频时间轴'), { target: { value: '6000' } });
    fireEvent.change(screen.getByLabelText('视频时间轴'), { target: { value: '9000' } });

    await act(async () => {
      rejectPlay(new DOMException('The play() request was interrupted by a call to pause().', 'AbortError'));
      await Promise.resolve();
    });

    expect(onPlaybackError).not.toHaveBeenCalled();
    expect(player).toHaveAttribute('data-paused', 'true');
    expect(video.currentTime).toBe(9);
    expect(onTimeChange).toHaveBeenLastCalledWith(9000);
  });

  it('still reports a current play request that fails for a real playback reason', async () => {
    const seed = createSeedData();
    const onPlaybackError = vi.fn();
    const { container } = render(
      <ReviewPlayer
        version={seed.versions[1]}
        issues={seed.issues.filter((issue) => issue.versionId === 'ver_ep28_v2')}
        selectedAnnotationSet={null}
        onTimeChange={vi.fn()}
        onDraftChange={vi.fn()}
        onSelectIssue={vi.fn()}
        onPlaybackError={onPlaybackError}
      />,
    );
    const video = container.querySelector('video') as HTMLVideoElement;
    Object.defineProperty(video, 'paused', { configurable: true, value: true });
    Object.defineProperty(video, 'play', {
      configurable: true,
      value: vi.fn(() => Promise.reject(new DOMException('浏览器拒绝播放', 'NotAllowedError'))),
    });

    await userEvent.click(screen.getByRole('button', { name: '播放' }));

    await waitFor(() => expect(onPlaybackError).toHaveBeenCalledWith('浏览器拒绝播放'));
  });

  it('recovers from seek buffering once a current frame is available', () => {
    const seed = createSeedData();
    const { container } = render(
      <ReviewPlayer
        version={seed.versions[1]}
        issues={seed.issues.filter((issue) => issue.versionId === 'ver_ep28_v2')}
        selectedAnnotationSet={null}
        onTimeChange={vi.fn()}
        onDraftChange={vi.fn()}
        onSelectIssue={vi.fn()}
        onPlaybackError={vi.fn()}
      />,
    );
    const player = screen.getByTestId('review-player');
    const video = container.querySelector('video') as HTMLVideoElement;
    Object.defineProperty(video, 'readyState', { configurable: true, value: 2 });

    fireEvent.waiting(video);
    expect(player).toHaveAttribute('data-media-state', 'loading');
    fireEvent.seeked(video);

    expect(player).toHaveAttribute('data-media-state', 'ready');
  });

  it('keeps seek buffering visible until canplay when no current frame is available', () => {
    const seed = createSeedData();
    const { container } = render(
      <ReviewPlayer
        version={seed.versions[1]}
        issues={seed.issues.filter((issue) => issue.versionId === 'ver_ep28_v2')}
        selectedAnnotationSet={null}
        onTimeChange={vi.fn()}
        onDraftChange={vi.fn()}
        onSelectIssue={vi.fn()}
        onPlaybackError={vi.fn()}
      />,
    );
    const player = screen.getByTestId('review-player');
    const video = container.querySelector('video') as HTMLVideoElement;
    Object.defineProperty(video, 'readyState', { configurable: true, value: 1 });

    fireEvent.waiting(video);
    fireEvent.seeked(video);
    expect(player).toHaveAttribute('data-media-state', 'loading');
    fireEvent.canPlay(video);

    expect(player).toHaveAttribute('data-media-state', 'ready');
  });

  it('auto-pauses the same unresolved issue again after the user seeks back before it', async () => {
    const seed = createSeedData();
    const issue = seed.issues.find((candidate) => candidate.issueId === 'issue_v2_001');
    if (!issue) throw new Error('missing seed issue');
    const onSelectIssue = vi.fn();
    const { container } = render(
      <ReviewPlayer
        version={seed.versions[1]}
        issues={[issue]}
        selectedAnnotationSet={null}
        onTimeChange={vi.fn()}
        onDraftChange={vi.fn()}
        onSelectIssue={onSelectIssue}
        onPlaybackError={vi.fn()}
      />,
    );
    const video = container.querySelector('video') as HTMLVideoElement;
    Object.defineProperty(video, 'paused', { configurable: true, value: false });
    Object.defineProperty(video, 'seeking', { configurable: true, value: false });
    Object.defineProperty(video, 'currentTime', { configurable: true, writable: true, value: 0 });
    Object.defineProperty(video, 'pause', { configurable: true, value: vi.fn() });

    video.currentTime = (issue.timestampMs + 1000) / 1000;
    fireEvent.timeUpdate(video);
    await waitFor(() => expect(onSelectIssue).toHaveBeenCalledTimes(1));

    fireEvent.change(screen.getByLabelText('视频时间轴'), { target: { value: '0' } });
    video.currentTime = (issue.timestampMs + 1000) / 1000;
    fireEvent.timeUpdate(video);

    await waitFor(() => expect(onSelectIssue).toHaveBeenCalledTimes(2));
  });

  it('maps keyboard shortcuts to SPEC tools and one-second frame stepping', async () => {
    const seed = createSeedData();
    const keyboardVersion = {
      ...seed.versions[1],
      durationMs: 2000,
      originalMedia: { ...seed.versions[1].originalMedia, durationMs: 2000 },
    };
    const onCreateIssueShortcut = vi.fn();
    render(
      <ReviewPlayer
        version={keyboardVersion}
        issues={seed.issues.filter((issue) => issue.versionId === 'ver_ep28_v2')}
        selectedAnnotationSet={null}
        onTimeChange={vi.fn()}
        onDraftChange={vi.fn()}
        onSelectIssue={vi.fn()}
        onPlaybackError={vi.fn()}
        onCreateIssueShortcut={onCreateIssueShortcut}
      />,
    );

    fireEvent.keyDown(document, { key: '1' });
    expect(screen.getByRole('button', { name: '画笔' })).toHaveClass('is-active');
    fireEvent.keyDown(document, { key: '2' });
    expect(screen.getByRole('button', { name: '箭头' })).toHaveClass('is-active');
    fireEvent.keyDown(document, { key: '3' });
    expect(screen.getByRole('button', { name: '矩形' })).toHaveClass('is-active');
    fireEvent.keyDown(document, { key: '4' });
    expect(screen.getByRole('button', { name: '圆形' })).toHaveClass('is-active');
    fireEvent.keyDown(document, { key: '5' });
    expect(screen.getByRole('button', { name: '文字' })).toHaveClass('is-active');

    fireEvent.keyDown(document, { key: 'c' });
    expect(onCreateIssueShortcut).toHaveBeenCalledTimes(1);

    fireEvent.keyDown(document, { key: 'ArrowRight', shiftKey: true });
    await waitFor(() => expect(screen.getByTestId('current-frame')).toHaveTextContent('25'));
  });

  it('resolves issue playback immediately when the video is already at the target timestamp', async () => {
    const seed = createSeedData();
    const issue = seed.issues.find((candidate) => candidate.issueId === 'issue_v2_001');
    if (!issue) throw new Error('missing seed issue');
    const ref = createRef<ReviewPlayerHandle>();
    const onTimeChange = vi.fn();
    const { container } = render(
      <ReviewPlayer
        ref={ref}
        version={seed.versions[1]}
        issues={[issue]}
        selectedAnnotationSet={null}
        onTimeChange={onTimeChange}
        onDraftChange={vi.fn()}
        onSelectIssue={vi.fn()}
        onPlaybackError={vi.fn()}
      />,
    );
    const video = container.querySelector('video') as HTMLVideoElement;
    Object.defineProperty(video, 'readyState', { configurable: true, value: 4 });
    Object.defineProperty(video, 'currentTime', { configurable: true, writable: true, value: issue.timestampMs / 1000 });
    Object.defineProperty(video, 'pause', { configurable: true, value: vi.fn() });

    await act(async () => {
      await ref.current?.playbackToTarget(playbackTargetFromIssue(issue));
    });

    await waitFor(() => expect(screen.getByTestId('current-timecode')).toHaveTextContent('00:00:00:04'));
    expect(onTimeChange).toHaveBeenLastCalledWith(issue.timestampMs);
  });

  it('keeps a play request started during issue positioning benign when positioning pauses it', async () => {
    const seed = createSeedData();
    const issue = seed.issues.find((candidate) => candidate.issueId === 'issue_v2_001');
    if (!issue) throw new Error('missing seed issue');
    const ref = createRef<ReviewPlayerHandle>();
    const onPlaybackError = vi.fn();
    let rejectPlay!: (reason?: unknown) => void;
    const pendingPlay = new Promise<void>((_resolve, reject) => {
      rejectPlay = reject;
    });
    const { container } = render(
      <ReviewPlayer
        ref={ref}
        version={seed.versions[1]}
        issues={[issue]}
        selectedAnnotationSet={null}
        onTimeChange={vi.fn()}
        onDraftChange={vi.fn()}
        onSelectIssue={vi.fn()}
        onPlaybackError={onPlaybackError}
      />,
    );
    const video = container.querySelector('video') as HTMLVideoElement;
    let readyState = 0;
    let paused = true;
    Object.defineProperty(video, 'readyState', { configurable: true, get: () => readyState });
    Object.defineProperty(video, 'paused', { configurable: true, get: () => paused });
    Object.defineProperty(video, 'currentTime', {
      configurable: true,
      writable: true,
      value: issue.timestampMs / 1000,
    });
    Object.defineProperty(video, 'play', {
      configurable: true,
      value: vi.fn(() => {
        paused = false;
        return pendingPlay;
      }),
    });
    Object.defineProperty(video, 'pause', {
      configurable: true,
      value: vi.fn(() => {
        if (!paused) {
          paused = true;
          rejectPlay(new DOMException('The play() request was interrupted by a call to pause().', 'AbortError'));
        }
      }),
    });

    let positioning!: Promise<void>;
    act(() => {
      positioning = ref.current!.playbackToTarget(playbackTargetFromIssue(issue));
    });
    await userEvent.click(screen.getByRole('button', { name: '播放' }));
    fireEvent.play(video);

    await act(async () => {
      readyState = 1;
      fireEvent.loadedMetadata(video);
      await Promise.resolve();
      readyState = 2;
      fireEvent.canPlay(video);
      await positioning;
    });

    expect(onPlaybackError).not.toHaveBeenCalled();
    expect(screen.getByTestId('review-player')).toHaveAttribute('data-paused', 'true');
  });

  it('resets transient playback state when switching versions before new metadata arrives', async () => {
    const seed = createSeedData();
    const onTimeChange = vi.fn();
    const onDraftChange = vi.fn();
    const commonProps = {
      issues: [],
      selectedAnnotationSet: null,
      onTimeChange,
      onDraftChange,
      onSelectIssue: vi.fn(),
      onPlaybackError: vi.fn(),
    };
    const { rerender } = render(<ReviewPlayer version={{ ...seed.versions[1], durationMs: 843000 }} {...commonProps} />);

    const firstInput = screen.getByLabelText('时间码输入') as HTMLInputElement;
    firstInput.value = '00:00:05:00';
    await userEvent.click(screen.getByRole('button', { name: '跳转' }));
    await waitFor(() => expect(screen.getByTestId('current-timecode')).toHaveTextContent('00:00:05:00'));

    rerender(<ReviewPlayer version={{ ...seed.versions[0], durationMs: 480 }} {...commonProps} />);
    await waitFor(() => expect(screen.getByTestId('current-timecode')).toHaveTextContent('00:00:00:00'));
    expect(screen.getByTestId('current-frame')).toHaveTextContent('0');

    const secondInput = screen.getByLabelText('时间码输入') as HTMLInputElement;
    secondInput.value = '00:00:05:00';
    await userEvent.click(screen.getByRole('button', { name: '跳转' }));

    await waitFor(() => expect(screen.getByTestId('current-timecode')).toHaveTextContent('00:00:00:12'));
    expect(screen.getByTestId('current-frame')).toHaveTextContent('12');
    expect(onTimeChange).toHaveBeenLastCalledWith(480);
    expect(onDraftChange).toHaveBeenLastCalledWith([]);
  });
});
