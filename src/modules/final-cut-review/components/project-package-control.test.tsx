import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { PackageResult } from '../contracts/types';
import { createReviewRuntime, ReviewRuntimeProvider, type ReviewRuntime } from '../entry/runtime';
import { ProjectPackageControl } from './ProjectPackageControl';

const result: PackageResult = {
  packageId: 'pkg_test',
  projectRefId: 'prj_test',
  packageFilename: 'existing-name.zip',
  fileName: 'existing-name.zip',
  createdAt: '2026-08-11T00:00:00.000Z',
  entries: [],
};

let runtime: ReviewRuntime | null = null;

afterEach(() => {
  runtime?.dispose();
  runtime = null;
});

function renderControl(overrides?: Partial<React.ComponentProps<typeof ProjectPackageControl>>) {
  runtime = createReviewRuntime();
  const props: React.ComponentProps<typeof ProjectPackageControl> = {
    createPending: false,
    disabled: false,
    downloadPending: false,
    entryMode: 'review',
    projectRefId: 'prj_test',
    onCreate: vi.fn().mockResolvedValue(result),
    onDownload: vi.fn().mockResolvedValue(undefined),
    onSuccess: vi.fn(),
    ...overrides,
  };
  render(
    <ReviewRuntimeProvider runtime={runtime}>
      <ProjectPackageControl {...props} />
    </ReviewRuntimeProvider>,
  );
  return props;
}

describe('ProjectPackageControl', () => {
  it('uses the dedicated centered modal surface instead of only the inline side-form styling', async () => {
    renderControl();
    await userEvent.click(screen.getByTestId('package-project'));

    expect(screen.getByRole('dialog', { name: '统一命名定稿原片' })).toHaveClass(
      'fj-review-side-form',
      'fj-review-modal',
    );
  });

  it('keeps the dedicated modal visible and all actions locked while preparation is pending', async () => {
    let resolveCreate!: (value: PackageResult) => void;
    const onCreate = vi.fn(
      () =>
        new Promise<PackageResult>((resolve) => {
          resolveCreate = resolve;
        }),
    );
    renderControl({ onCreate });

    await userEvent.click(screen.getByTestId('package-project'));
    await userEvent.type(screen.getByLabelText('统一名称'), '准备中视觉状态');
    await userEvent.click(screen.getByRole('button', { name: '确认并下载' }));

    const dialog = screen.getByRole('dialog', { name: '统一命名定稿原片' });
    expect(dialog).toHaveClass('fj-review-modal');
    expect(screen.getByLabelText('统一名称')).toBeDisabled();
    expect(screen.getByRole('button', { name: '准备中' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '取消' })).toBeDisabled();

    resolveCreate(result);
    await waitFor(() => expect(dialog).not.toBeInTheDocument());
  });

  it('describes the real download-launch stage without claiming the file is already downloading', async () => {
    let resolveDownload!: () => void;
    const onDownload = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          resolveDownload = resolve;
        }),
    );
    renderControl({ onDownload });

    await userEvent.click(screen.getByTestId('package-project'));
    await userEvent.type(screen.getByLabelText('统一名称'), '启动下载反馈');
    await userEvent.click(screen.getByRole('button', { name: '确认并下载' }));

    expect(await screen.findByRole('button', { name: '正在启动下载' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: '正在下载' })).not.toBeInTheDocument();

    resolveDownload();
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '统一命名定稿原片' })).not.toBeInTheDocument());
  });

  it('does not start a hidden download after the user leaves during package preparation', async () => {
    let resolveCreate!: (value: PackageResult) => void;
    const onCreate = vi.fn(
      () =>
        new Promise<PackageResult>((resolve) => {
          resolveCreate = resolve;
        }),
    );
    const onDownload = vi.fn().mockResolvedValue(undefined);
    const onSuccess = vi.fn();
    runtime = createReviewRuntime();
    const { unmount } = render(
      <ReviewRuntimeProvider runtime={runtime}>
        <ProjectPackageControl
          createPending={false}
          disabled={false}
          downloadPending={false}
          entryMode="review"
          projectRefId="prj_test"
          onCreate={onCreate}
          onDownload={onDownload}
          onSuccess={onSuccess}
        />
      </ReviewRuntimeProvider>,
    );

    await userEvent.click(screen.getByTestId('package-project'));
    await userEvent.type(screen.getByLabelText('统一名称'), '离开页面不自动下载');
    await userEvent.click(screen.getByRole('button', { name: '确认并下载' }));
    expect(onCreate).toHaveBeenCalledOnce();

    unmount();
    resolveCreate(result);
    await Promise.resolve();
    await Promise.resolve();

    expect(onDownload).not.toHaveBeenCalled();
    expect(onSuccess).not.toHaveBeenCalled();
  });

  it('aborts the download session when the user leaves during download authorization', async () => {
    let resolveDownload!: () => void;
    let downloadSignal: AbortSignal | undefined;
    const onDownload = vi.fn(
      (_result: PackageResult, signal: AbortSignal) =>
        new Promise<void>((resolve) => {
          downloadSignal = signal;
          resolveDownload = resolve;
        }),
    );
    const onSuccess = vi.fn();
    runtime = createReviewRuntime();
    const { unmount } = render(
      <ReviewRuntimeProvider runtime={runtime}>
        <ProjectPackageControl
          createPending={false}
          disabled={false}
          downloadPending={false}
          entryMode="review"
          projectRefId="prj_test"
          onCreate={vi.fn().mockResolvedValue(result)}
          onDownload={onDownload}
          onSuccess={onSuccess}
        />
      </ReviewRuntimeProvider>,
    );

    await userEvent.click(screen.getByTestId('package-project'));
    await userEvent.type(screen.getByLabelText('统一名称'), '离开授权不自动下载');
    await userEvent.click(screen.getByRole('button', { name: '确认并下载' }));
    await waitFor(() => expect(onDownload).toHaveBeenCalledOnce());
    expect(downloadSignal?.aborted).toBe(false);

    unmount();
    expect(downloadSignal?.aborted).toBe(true);
    resolveDownload();
    await Promise.resolve();
    await Promise.resolve();

    expect(onSuccess).not.toHaveBeenCalled();
  });

  it('closes on cancel without preparing, downloading, or changing data', async () => {
    const props = renderControl();
    await userEvent.click(screen.getByTestId('package-project'));
    await userEvent.type(screen.getByLabelText('统一名称'), '取消名称');
    await userEvent.click(screen.getByRole('button', { name: '取消' }));

    expect(screen.queryByRole('dialog', { name: '统一命名定稿原片' })).not.toBeInTheDocument();
    expect(props.onCreate).not.toHaveBeenCalled();
    expect(props.onDownload).not.toHaveBeenCalled();
    expect(props.onSuccess).not.toHaveBeenCalled();
  });

  it('preserves the raw valid name and automatically downloads exactly once after preparation', async () => {
    const sequence: string[] = [];
    const onCreate = vi.fn(async (input: { projectRefId: string; unifiedName: string }) => {
      sequence.push(`prepare:${input.unifiedName}`);
      return result;
    });
    const onDownload = vi.fn(async () => {
      sequence.push('download');
    });
    const onSuccess = vi.fn(() => sequence.push('success'));
    renderControl({ onCreate, onDownload, onSuccess });

    await userEvent.click(screen.getByTestId('package-project'));
    const input = screen.getByLabelText('统一名称');
    await userEvent.type(input, '  中文 Name.标点!  ');
    await userEvent.click(screen.getByRole('button', { name: '确认并下载' }));

    await waitFor(() => expect(onDownload).toHaveBeenCalledOnce());
    expect(onCreate).toHaveBeenCalledWith({
      projectRefId: 'prj_test',
      unifiedName: '  中文 Name.标点!  ',
    }, expect.any(AbortSignal));
    expect(onDownload).toHaveBeenCalledWith(result, expect.any(AbortSignal));
    expect(sequence).toEqual(['prepare:  中文 Name.标点!  ', 'download', 'success']);
    expect(screen.queryByRole('dialog', { name: '统一命名定稿原片' })).not.toBeInTheDocument();
  });

  it('rejects blank and unsafe path input before any request', async () => {
    const props = renderControl();
    await userEvent.click(screen.getByTestId('package-project'));
    const input = screen.getByLabelText('统一名称');

    await userEvent.type(input, '../escape');
    await userEvent.click(screen.getByRole('button', { name: '确认并下载' }));
    expect(await screen.findByText('统一名称不能包含目录分隔符。')).toBeInTheDocument();
    expect(props.onCreate).not.toHaveBeenCalled();
    expect(props.onDownload).not.toHaveBeenCalled();
  });

  it('keeps the name and error visible after preparation fails, then retries safely', async () => {
    const onCreate = vi.fn()
      .mockRejectedValueOnce(new Error('项目包准备失败'))
      .mockResolvedValueOnce(result);
    const onDownload = vi.fn().mockResolvedValue(undefined);
    renderControl({ onCreate, onDownload });

    await userEvent.click(screen.getByTestId('package-project'));
    const input = screen.getByLabelText('统一名称');
    await userEvent.type(input, '失败后重试');
    await userEvent.click(screen.getByRole('button', { name: '确认并下载' }));

    expect(await screen.findByText('项目包准备失败')).toBeInTheDocument();
    expect(input).toHaveValue('失败后重试');
    expect(onDownload).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: '确认并下载' }));

    await waitFor(() => expect(onDownload).toHaveBeenCalledOnce());
    expect(onCreate).toHaveBeenCalledTimes(2);
    expect(onCreate).toHaveBeenNthCalledWith(
      1,
      { projectRefId: 'prj_test', unifiedName: '失败后重试' },
      expect.any(AbortSignal),
    );
    expect(onCreate).toHaveBeenNthCalledWith(
      2,
      { projectRefId: 'prj_test', unifiedName: '失败后重试' },
      expect.any(AbortSignal),
    );
  });

  it('keeps the name and error visible after download fails, then prepares and downloads safely again', async () => {
    const onCreate = vi.fn().mockResolvedValue(result);
    const onDownload = vi.fn()
      .mockRejectedValueOnce(new Error('自动下载失败'))
      .mockResolvedValueOnce(undefined);
    renderControl({ onCreate, onDownload });

    await userEvent.click(screen.getByTestId('package-project'));
    const input = screen.getByLabelText('统一名称');
    await userEvent.type(input, '下载重试');
    await userEvent.click(screen.getByRole('button', { name: '确认并下载' }));

    expect(await screen.findByText('自动下载失败')).toBeInTheDocument();
    expect(input).toHaveValue('下载重试');
    await userEvent.click(screen.getByRole('button', { name: '确认并下载' }));

    await waitFor(() => expect(onDownload).toHaveBeenCalledTimes(2));
    expect(onCreate).toHaveBeenCalledTimes(2);
    expect(onCreate).toHaveBeenLastCalledWith(
      { projectRefId: 'prj_test', unifiedName: '下载重试' },
      expect.any(AbortSignal),
    );
  });
});
