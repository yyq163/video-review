import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createRef } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ReviewAnnotationShape } from '../contracts/types';
import { annotationShapeBounds } from '../core/annotation-drag';
import { playbackTargetFromIssue } from '../core/playback';
import { createSeedData } from '../core/seed';
import { ReviewRuntimeProvider, createReviewRuntime, type ReviewRuntime } from '../entry/runtime';
import { EpisodeStrip } from '../pages/review-workspace-elements';
import { IssuePanel } from './IssuePanel';
import { ReviewPlayer, type ReviewPlayerHandle } from './ReviewPlayer';
import { CreateItemUploadPanel } from './UploadPanel';

const activeRuntimes: ReviewRuntime[] = [];

function renderWithRuntime(ui: React.ReactElement) {
  const runtime = createReviewRuntime();
  activeRuntimes.push(runtime);
  return render(<ReviewRuntimeProvider runtime={runtime}>{ui}</ReviewRuntimeProvider>);
}

afterEach(() => {
  cleanup();
  for (const runtime of activeRuntimes.splice(0)) runtime.dispose();
  vi.restoreAllMocks();
});

describe('frozen upload queue behavior', () => {
  it('blocks padded 03 before submit when an existing item is episode 3', async () => {
    const onSubmit = vi.fn().mockResolvedValue({ outcome: 'success' as const });
    render(
      <CreateItemUploadPanel
        existingEpisodes={['3']}
        onSubmit={onSubmit}
      />,
    );

    await userEvent.upload(
      screen.getByTestId('create-item-file'),
      new File(['three'], '03.mp4', { type: 'video/mp4' }),
    );
    expect(screen.getByTestId('upload-row-0')).toHaveClass('is-conflict');
    expect(screen.getByRole('alert')).toHaveTextContent('第03集已上传');
    await userEvent.click(screen.getByRole('button', { name: '上传 V1' }));
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('blocks only rows whose normalized episode already exists and restores them after correction', async () => {
    const onSubmit = vi.fn().mockResolvedValue({ outcome: 'success' as const });
    render(
      <CreateItemUploadPanel
        existingEpisodes={['第 02 集']}
        onSubmit={onSubmit}
      />,
    );

    const conflictFile = new File(['conflict'], '冲突第02集.mp4', { type: 'video/mp4' });
    const safeFile = new File(['safe'], '新片第03集.mp4', { type: 'video/mp4' });
    await userEvent.upload(screen.getByTestId('create-item-file'), [conflictFile, safeFile]);

    const conflictRow = screen.getByTestId('upload-row-0');
    expect(conflictRow).toHaveClass('is-conflict');
    expect(within(conflictRow).getByLabelText('集数')).toHaveAttribute('aria-invalid', 'true');
    expect(within(conflictRow).getByRole('alert')).toHaveTextContent('第02集已上传，请修改上传信息');

    await userEvent.click(screen.getByRole('button', { name: '上传 V1' }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledOnce());
    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({ episode: '03', file: safeFile }),
      expect.any(Function),
    );
    expect(screen.getByTestId('upload-row-0')).toBeInTheDocument();

    await userEvent.clear(within(conflictRow).getByLabelText('集数'));
    await userEvent.type(within(conflictRow).getByLabelText('集数'), '04');
    expect(conflictRow).not.toHaveClass('is-conflict');
    expect(within(conflictRow).queryByRole('alert')).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: '上传 V1' }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(2));
    expect(onSubmit).toHaveBeenLastCalledWith(
      expect.objectContaining({ episode: '04', file: conflictFile }),
      expect.any(Function),
    );
  });

  it('blocks a later duplicate episode added in the same file selection before upload init', async () => {
    const onSubmit = vi.fn().mockResolvedValue({ outcome: 'success' as const });
    render(<CreateItemUploadPanel existingEpisodes={[]} onSubmit={onSubmit} />);

    await userEvent.upload(
      screen.getByTestId('create-item-file'),
      [
        new File(['first'], '03.mp4', { type: 'video/mp4' }),
        new File(['duplicate'], '03改.mp4', { type: 'video/mp4' }),
      ],
    );

    const duplicateRow = screen.getByTestId('upload-row-1');
    expect(duplicateRow).toHaveClass('is-conflict');
    expect(within(duplicateRow).getByRole('alert')).toHaveTextContent(
      '第03集已上传，请修改上传信息',
    );

    await userEvent.click(screen.getByRole('button', { name: '上传 V1' }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledOnce());
    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({ episode: '03', file: expect.objectContaining({ name: '03.mp4' }) }),
      expect.any(Function),
    );
    expect(duplicateRow).toBeInTheDocument();
    expect(duplicateRow).toHaveClass('is-conflict');
    expect(onSubmit).toHaveBeenCalledOnce();
  });

  it('removes pending, conflicted, and failed queue rows with an accessible row-level close button', async () => {
    const onSubmit = vi.fn().mockResolvedValue({
      outcome: 'failed' as const,
      message: '数据库约束拒绝该操作',
    });
    render(<CreateItemUploadPanel existingEpisodes={['02']} onSubmit={onSubmit} />);

    await userEvent.upload(
      screen.getByTestId('create-item-file'),
      [
        new File(['conflict'], '02改.mp4', { type: 'video/mp4' }),
        new File(['failed'], '03.mp4', { type: 'video/mp4' }),
        new File(['pending'], '04.mp4', { type: 'video/mp4' }),
      ],
    );

    await userEvent.click(screen.getByRole('button', { name: '移除待上传条目 04.mp4' }));
    expect(screen.queryByTestId('upload-row-2')).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: '上传 V1' }));
    await waitFor(() => expect(screen.getByTestId('upload-row-1')).toHaveTextContent('上传失败'));
    expect(onSubmit).toHaveBeenCalledOnce();

    await userEvent.click(screen.getByRole('button', { name: '移除待上传条目 02改.mp4' }));
    expect(screen.queryByTestId('upload-row-0')).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: '移除待上传条目 03.mp4' }));
    await waitFor(() => expect(screen.queryByTestId('create-item-upload-rows')).not.toBeInTheDocument());
    expect(screen.queryByRole('button', { name: /关闭上传队列|重新打开上传队列/ })).not.toBeInTheDocument();
    expect(onSubmit).toHaveBeenCalledOnce();
  });

  it('never starts a queued row after its close button removes it while all five slots are occupied', async () => {
    const releaseActive: Array<(result: { outcome: 'success' }) => void> = [];
    const submittedFiles: string[] = [];
    const onSubmit = vi.fn((input: { file: File }) => {
      submittedFiles.push(input.file.name);
      if (releaseActive.length < 5) {
        return new Promise<{ outcome: 'success' }>((resolve) => {
          releaseActive.push(resolve);
        });
      }
      return Promise.resolve({ outcome: 'success' as const });
    });
    render(
      <CreateItemUploadPanel
        existingEpisodes={[]}
        onSubmit={onSubmit}
      />,
    );
    const files = Array.from({ length: 7 }, (_, index) =>
      new File([`video-${index + 1}`], `${String(index + 1).padStart(2, '0')}.mp4`, {
        type: 'video/mp4',
      }));

    await userEvent.upload(screen.getByTestId('create-item-file'), files);
    await userEvent.click(screen.getByRole('button', { name: '上传 V1' }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(5));

    await userEvent.click(screen.getByRole('button', { name: '移除待上传条目 07.mp4' }));
    expect(screen.queryByText('07.mp4')).not.toBeInTheDocument();

    for (const release of releaseActive) release({ outcome: 'success' });
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(6));
    await waitFor(() => expect(screen.queryByTestId('create-item-upload-rows')).not.toBeInTheDocument());
    expect(submittedFiles).toContain('06.mp4');
    expect(submittedFiles).not.toContain('07.mp4');
  });
});

describe('frozen player controls and focus boundaries', () => {
  it('steps exactly one rational-rate frame in either direction and clamps at both ends', async () => {
    const seed = createSeedData();
    const version = {
      ...seed.versions[1],
      durationMs: 1001,
      fpsNum: 30000,
      fpsDen: 1001,
      originalMedia: {
        ...seed.versions[1].originalMedia,
        durationMs: 1001,
        fpsNum: 30000,
        fpsDen: 1001,
      },
    };
    const { container } = render(
      <ReviewPlayer
        version={version}
        issues={[]}
        selectedAnnotationSet={null}
        onTimeChange={vi.fn()}
        onDraftChange={vi.fn()}
        onSelectIssue={vi.fn()}
        onPlaybackError={vi.fn()}
      />,
    );
    const video = container.querySelector('video') as HTMLVideoElement;
    Object.defineProperty(video, 'currentTime', { configurable: true, writable: true, value: 0 });

    await userEvent.click(screen.getByRole('button', { name: '下一帧' }));
    expect(screen.getByTestId('current-frame')).toHaveTextContent('1');
    expect(video.currentTime).toBeCloseTo(0.034, 3);

    await userEvent.click(screen.getByRole('button', { name: '下一帧' }));
    expect(screen.getByTestId('current-frame')).toHaveTextContent('2');
    await userEvent.click(screen.getByRole('button', { name: '上一帧' }));
    expect(screen.getByTestId('current-frame')).toHaveTextContent('1');

    await userEvent.click(screen.getByRole('button', { name: '上一帧' }));
    await userEvent.click(screen.getByRole('button', { name: '上一帧' }));
    expect(screen.getByTestId('current-frame')).toHaveTextContent('0');
    expect(video.currentTime).toBe(0);

    video.currentTime = 1.001;
    fireEvent.timeUpdate(video);
    expect(screen.getByTestId('current-frame')).toHaveTextContent('29');
    await userEvent.click(screen.getByRole('button', { name: '下一帧' }));
    expect(screen.getByTestId('current-frame')).toHaveTextContent('29');
    expect(video.currentTime).toBeCloseTo(0.968, 3);
    await userEvent.click(screen.getByRole('button', { name: '上一帧' }));
    expect(screen.getByTestId('current-frame')).toHaveTextContent('28');
  });

  it('keeps Space and arrows native when focus is on player controls and opinion avatars', async () => {
    const seed = createSeedData();
    const issue = seed.issues.find((candidate) => candidate.issueId === 'issue_v2_001')!;
    const onSelectIssue = vi.fn();
    const { container } = render(
      <>
        <ReviewPlayer
          version={{ ...seed.versions[1], durationMs: 2000 }}
          issues={[issue]}
          selectedAnnotationSet={null}
          onTimeChange={vi.fn()}
          onDraftChange={vi.fn()}
          onSelectIssue={onSelectIssue}
          onPlaybackError={vi.fn()}
        />
        <EpisodeStrip
          items={seed.items}
          currentItemId={seed.items[0].reviewItemId}
          versionCounts={{}}
          currentLabels={{}}
          onSelect={vi.fn()}
        />
      </>,
    );
    const video = container.querySelector('video') as HTMLVideoElement;
    const play = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(video, 'play', { configurable: true, value: play });
    Object.defineProperty(video, 'currentTime', { configurable: true, writable: true, value: 0 });

    fireEvent.keyDown(document.body, { key: 'ArrowRight' });
    expect(screen.getByTestId('current-frame')).toHaveTextContent('1');

    const scrubber = screen.getByLabelText('视频时间轴');
    scrubber.focus();
    fireEvent.keyDown(scrubber, { key: 'ArrowRight' });
    expect(screen.getByTestId('current-frame')).toHaveTextContent('1');

    const timecodeInput = screen.getByLabelText('时间码输入');
    timecodeInput.focus();
    fireEvent.keyDown(timecodeInput, { key: 'ArrowRight' });
    fireEvent.keyDown(timecodeInput, { key: ' ' });
    expect(screen.getByTestId('current-frame')).toHaveTextContent('1');
    expect(play).not.toHaveBeenCalled();

    const marker = screen.getByRole('button', { name: `意见 #${issue.issueNo} 未修改` });
    marker.focus();
    await userEvent.keyboard(' ');
    expect(onSelectIssue).toHaveBeenCalledOnce();
    expect(play).not.toHaveBeenCalled();

    const nextFrame = screen.getByRole('button', { name: '下一帧' });
    nextFrame.focus();
    fireEvent.keyDown(nextFrame, { key: 'ArrowRight' });
    expect(screen.getByTestId('current-frame')).toHaveTextContent('4');

    const episodeStrip = screen.getByTestId('episode-strip-scroll');
    episodeStrip.focus();
    const episodeArrow = new KeyboardEvent('keydown', {
      bubbles: true,
      cancelable: true,
      key: 'ArrowRight',
    });
    episodeStrip.dispatchEvent(episodeArrow);
    expect(episodeArrow.defaultPrevented).toBe(false);
    expect(screen.getByTestId('current-frame')).toHaveTextContent('4');
  });

  it('keeps an explicitly selected issue on its exact frame when media time quantizes one millisecond backward', async () => {
    const seed = createSeedData();
    const version = {
      ...seed.versions[1],
      durationMs: 100_000,
      fpsNum: 30,
      fpsDen: 1,
      originalMedia: {
        ...seed.versions[1].originalMedia,
        durationMs: 100_000,
        fpsNum: 30,
        fpsDen: 1,
      },
    };
    const issue = {
      ...seed.issues[0],
      issueId: 'issue_quantized_seek',
      versionId: version.versionId,
      timestampMs: 74_885,
      frameNumber: 2246,
    };
    const playerRef = createRef<ReviewPlayerHandle>();
    const { container } = render(
      <ReviewPlayer
        ref={playerRef}
        version={version}
        issues={[issue]}
        selectedAnnotationSet={null}
        onTimeChange={vi.fn()}
        onDraftChange={vi.fn()}
        onSelectIssue={vi.fn()}
        onPlaybackError={vi.fn()}
      />,
    );
    const video = container.querySelector('video') as HTMLVideoElement;
    let mediaCurrentTime = 74.866;
    Object.defineProperty(video, 'currentTime', {
      configurable: true,
      get: () => mediaCurrentTime,
      set: (value: number) => {
        mediaCurrentTime = value;
        queueMicrotask(() => fireEvent.seeked(video));
      },
    });
    Object.defineProperty(video, 'readyState', { configurable: true, value: 4 });
    Object.defineProperty(video, 'pause', { configurable: true, value: vi.fn() });

    await act(async () => {
      await playerRef.current?.playbackToTarget(playbackTargetFromIssue(issue));
    });
    video.currentTime = 74.866;
    fireEvent.timeUpdate(video);
    fireEvent.timeUpdate(video);

    expect(screen.getByTestId('current-frame')).toHaveTextContent('2246');
    expect(screen.getByLabelText('时间码输入')).toHaveValue('00:01:14:26');
  });
});

describe('frozen direct annotation manipulation', () => {
  it.each([
    ['rect', '矩形'],
    ['circle', '圆形'],
    ['arrow', '箭头'],
    ['pen', '画笔'],
    ['text', '文字'],
  ] as const)('creates, selects, and moves the complete %s object without reverting', async (tool, label) => {
    const seed = createSeedData();
    const onDraftChange = vi.fn<(shapes: ReviewAnnotationShape[]) => void>();
    const { container } = render(
      <ReviewPlayer
        version={seed.versions[1]}
        issues={[]}
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
      right: 200,
      bottom: 100,
      width: 200,
      height: 100,
      toJSON: () => ({}),
    } as DOMRect);

    await userEvent.click(screen.getByRole('button', { name: label }));
    fireEvent.pointerDown(stage, { clientX: 60, clientY: 32, pointerId: 1 });
    if (tool !== 'text') {
      fireEvent.pointerMove(stage, { clientX: 100, clientY: 62, pointerId: 1 });
      fireEvent.pointerUp(stage, { clientX: 100, clientY: 62, pointerId: 1 });
    }
    await waitFor(() => expect(onDraftChange).toHaveBeenLastCalledWith([
      expect.objectContaining({ tool }),
    ]));
    const before = structuredClone(onDraftChange.mock.calls.at(-1)![0][0]);
    const bounds = annotationShapeBounds(before, 1920, 1080);
    const videoLeft = (200 - (100 * 16) / 9) / 2;
    const videoWidth = (100 * 16) / 9;
    const hitX = videoLeft + (bounds.x + bounds.width / 2) * videoWidth;
    const hitY = (bounds.y + bounds.height / 2) * 100;

    fireEvent.pointerDown(stage, { clientX: hitX, clientY: hitY, pointerId: 2 });
    fireEvent.pointerMove(stage, { clientX: hitX + 2, clientY: hitY + 1, pointerId: 2 });
    fireEvent.pointerUp(stage, { clientX: hitX + 2, clientY: hitY + 1, pointerId: 2 });
    expect(onDraftChange.mock.calls.at(-1)![0][0]).toEqual(before);

    fireEvent.pointerDown(stage, { clientX: hitX, clientY: hitY, pointerId: 3 });
    fireEvent.pointerMove(stage, { clientX: hitX + 18, clientY: hitY + 9, pointerId: 3 });
    fireEvent.pointerUp(stage, { clientX: hitX + 18, clientY: hitY + 9, pointerId: 3 });
    await waitFor(() => {
      const after = onDraftChange.mock.calls.at(-1)![0][0];
      expect(after.shapeId).toBe(before.shapeId);
      expect(after.tool).toBe(before.tool);
      expect(after).not.toEqual(before);
    });
    expect(container.querySelector('.fj-review-draft-shape.is-selected')).toHaveAttribute(
      'data-selected',
      'true',
    );
  });
});

describe('frozen episode strip behavior', () => {
  it('keeps twelve episodes reachable and scrolls the selected card into view', async () => {
    const seed = createSeedData();
    const items = Array.from({ length: 12 }, (_, index) => ({
      ...seed.items[0],
      reviewItemId: `episode-${index + 1}`,
      episode: String(index + 1),
      title: `测试剧集 ${index + 1}`,
    }));
    const scrollIntoView = vi.fn();
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
      configurable: true,
      value: scrollIntoView,
    });
    const onSelect = vi.fn();
    render(
      <EpisodeStrip
        items={items}
        currentItemId="episode-12"
        versionCounts={Object.fromEntries(items.map((item) => [item.reviewItemId, 1]))}
        currentLabels={Object.fromEntries(items.map((item) => [item.reviewItemId, 'V1']))}
        onSelect={onSelect}
      />,
    );

    const strip = screen.getByTestId('episode-strip-scroll');
    expect(screen.getAllByRole('button', { name: /第 \d+ 集/ })).toHaveLength(12);
    expect(strip).toHaveAttribute('tabindex', '0');
    await waitFor(() => expect(scrollIntoView).toHaveBeenCalled());

    await userEvent.click(screen.getByRole('button', { name: /第 1 集/ }));
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ reviewItemId: 'episode-1' }));
    expect(scrollIntoView).toHaveBeenCalledWith(expect.objectContaining({ inline: 'nearest' }));
  });
});

describe('frozen edit-entry bulk issue status behavior', () => {
  it('continues after one resolve failure, keeps its reason, and does not reprocess successes', async () => {
    const seed = createSeedData();
    const issues = seed.issues.filter((issue) => issue.versionId === 'ver_ep28_v2').slice(0, 2);
    const attempts = new Map<string, number>();
    const onResolve = vi.fn(async (issue: (typeof issues)[number]) => {
      attempts.set(issue.issueId, (attempts.get(issue.issueId) ?? 0) + 1);
      if (issue.issueId === issues[1].issueId && attempts.get(issue.issueId) === 1) {
        throw new Error('该意见暂时无法更新');
      }
    });
    renderWithRuntime(
      <IssuePanel
        entryMode="edit"
        version={seed.versions[1]}
        issues={issues}
        historicalIssues={[]}
        isCurrentVersion
        onCreateIssue={vi.fn()}
        onSelectIssue={vi.fn()}
        onEditIssue={vi.fn()}
        onReplyIssue={vi.fn()}
        onResolve={onResolve}
        onReopen={vi.fn()}
        onDeleteIssue={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByRole('checkbox', { name: '全选未修改意见' }));
    await userEvent.click(screen.getByRole('button', { name: '批量标记已修改（2）' }));

    await waitFor(() => expect(onResolve).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId(`issue-${issues[0].issueId}`)).toHaveTextContent('已修改');
    expect(screen.getByTestId(`issue-${issues[1].issueId}`)).toHaveTextContent('该意见暂时无法更新');
    expect(screen.getByRole('button', { name: '批量标记已修改（1）' })).toBeEnabled();

    await userEvent.click(screen.getByRole('button', { name: '批量标记已修改（1）' }));
    await waitFor(() => expect(onResolve).toHaveBeenCalledTimes(3));
    expect(attempts.get(issues[0].issueId)).toBe(1);
    expect(attempts.get(issues[1].issueId)).toBe(2);
  });
});
