import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { useState } from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { FinalizationRevocation, ReviewItem } from '../contracts/types';
import { RevokeFinalizationResultUncertainError } from '../adapters/http-review-finalization-operation';
import { createSeedData } from '../core/seed';
import { createReviewRuntime, ReviewRuntimeProvider, type ReviewRuntime } from '../entry/runtime';
import { EpisodeStrip } from './review-workspace-elements';

let runtime: ReviewRuntime | null = null;

afterEach(() => {
  runtime?.dispose();
  runtime = null;
  vi.restoreAllMocks();
});

function renderFinalizedStrip(
  onRevoke: () => Promise<FinalizationRevocation>,
  onSelect = vi.fn(),
) {
  runtime = createReviewRuntime();
  const item = {
    ...createSeedData().items[0],
    status: 'finalized' as const,
    activeFinalizationId: 'fin_episode_strip',
  };
  render(
    <ReviewRuntimeProvider runtime={runtime}>
      <EpisodeStrip
        currentFileNames={{ [item.reviewItemId]: 'current.MOV' }}
        currentItemId={item.reviewItemId}
        currentVersionLabels={{ [item.reviewItemId]: 'V2' }}
        entryMode="review"
        isArchived={false}
        itemActionPending={false}
        items={[item]}
        onRevokeFinalization={onRevoke}
        onSelect={onSelect}
        revocationUncertainIds={new Set()}
        unresolvedCounts={{ [item.reviewItemId]: 0 }}
      />
    </ReviewRuntimeProvider>,
  );
  return { item, onSelect };
}

describe('EpisodeStrip finalized revoke control', () => {
  it('uses sibling controls and does not switch episodes when revoking', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const onRevoke = vi.fn().mockResolvedValue({} as FinalizationRevocation);
    const { item, onSelect } = renderFinalizedStrip(onRevoke);
    const select = screen.getByTestId(`episode-item-${item.reviewItemId}`);
    const revoke = screen.getByRole('button', { name: `撤销第${item.episode}集定稿` });

    expect(select.contains(revoke)).toBe(false);
    expect(select.closest('article')).toContainElement(revoke);
    await userEvent.click(revoke);

    await waitFor(() => expect(onRevoke).toHaveBeenCalledOnce());
    expect(onSelect).not.toHaveBeenCalled();
  });

  it('keeps the finalized state and sends no request when confirmation is cancelled', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    const onRevoke = vi.fn().mockResolvedValue({} as FinalizationRevocation);
    const { onSelect } = renderFinalizedStrip(onRevoke);

    await userEvent.click(screen.getByRole('button', { name: /撤销第.*集定稿/ }));

    expect(onRevoke).not.toHaveBeenCalled();
    expect(onSelect).not.toHaveBeenCalled();
    expect(screen.getByText('已定稿')).toBeInTheDocument();
  });

  it('does not optimistically revoke after a definite failure and locks an uncertain result', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const onRevoke = vi
      .fn()
      .mockRejectedValueOnce(new Error('definite failure'))
      .mockRejectedValueOnce(new RevokeFinalizationResultUncertainError(new TypeError('network')));
    renderFinalizedStrip(onRevoke);
    const revoke = screen.getByRole('button', { name: /撤销第.*集定稿/ });

    await userEvent.click(revoke);
    await waitFor(() => expect(revoke).not.toBeDisabled());
    expect(screen.getByText('已定稿')).toBeInTheDocument();

    await userEvent.click(revoke);
    await waitFor(() => expect(revoke).toBeDisabled());
    expect(screen.getByText('确认中')).toBeInTheDocument();
  });

  it('synchronously blocks a double click so an older failure cannot race a newer uncertain result', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    let rejectFirst: ((reason?: unknown) => void) | undefined;
    const firstAttempt = new Promise<FinalizationRevocation>((_resolve, reject) => {
      rejectFirst = reject;
    });
    const onRevoke = vi
      .fn()
      .mockImplementationOnce(() => firstAttempt)
      .mockRejectedValueOnce(
        new RevokeFinalizationResultUncertainError(new TypeError('network')),
      );
    renderFinalizedStrip(onRevoke);
    const revoke = screen.getByRole('button', { name: /撤销第.*集定稿/ });

    fireEvent.click(revoke);
    fireEvent.click(revoke);

    expect(window.confirm).toHaveBeenCalledOnce();
    expect(onRevoke).toHaveBeenCalledOnce();
    expect(revoke).toBeDisabled();
    expect(screen.getByText('撤销中')).toBeInTheDocument();

    await act(async () => {
      rejectFirst?.(new Error('definite failure'));
      await firstAttempt.catch(() => undefined);
    });
    await waitFor(() => expect(revoke).not.toBeDisabled());

    await userEvent.click(revoke);
    await waitFor(() => expect(onRevoke).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(revoke).toBeDisabled());
    expect(window.confirm).toHaveBeenCalledTimes(2);
    expect(screen.getByText('确认中')).toBeInTheDocument();
  });

  it('keeps the uncertain revoke control visible and locked when authority masks the active finalization id', () => {
    runtime = createReviewRuntime();
    const item = {
      ...createSeedData().items[0],
      status: 'finalized' as const,
      activeFinalizationId: null,
    };
    render(
      <ReviewRuntimeProvider runtime={runtime}>
        <EpisodeStrip
          currentFileNames={{ [item.reviewItemId]: 'current.MOV' }}
          currentItemId={item.reviewItemId}
          currentVersionLabels={{ [item.reviewItemId]: 'V2' }}
          entryMode="review"
          isArchived={false}
          itemActionPending={false}
          items={[item]}
          onRevokeFinalization={vi.fn()}
          onSelect={vi.fn()}
          revocationUncertainIds={new Set([item.reviewItemId])}
          unresolvedCounts={{ [item.reviewItemId]: 0 }}
        />
      </ReviewRuntimeProvider>,
    );

    const revoke = screen.getByRole('button', {
      name: `撤销第${item.episode}集定稿`,
    });
    expect(revoke).toBeDisabled();
    expect(revoke).toHaveTextContent('确认中');
    expect(revoke).toHaveClass('fj-review-finalized-revoke');
  });

  it('waits for authority to acknowledge local uncertainty before accepting its removal', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    let nextFrameId = 1;
    const frames = new Map<number, FrameRequestCallback>();
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => {
      const frameId = nextFrameId;
      nextFrameId += 1;
      frames.set(frameId, callback);
      return frameId;
    });
    vi.spyOn(window, 'cancelAnimationFrame').mockImplementation((frameId) => {
      frames.delete(frameId);
    });
    runtime = createReviewRuntime();
    const item = {
      ...createSeedData().items[0],
      status: 'finalized' as const,
      activeFinalizationId: 'fin_episode_authority_handshake',
    };
    const onRevoke = vi
      .fn()
      .mockRejectedValue(
        new RevokeFinalizationResultUncertainError(new TypeError('network')),
      );
    const view = (uncertainIds: ReadonlySet<string>) => (
      <ReviewRuntimeProvider runtime={runtime!}>
        <EpisodeStrip
          currentFileNames={{ [item.reviewItemId]: 'current.MOV' }}
          currentItemId={item.reviewItemId}
          currentVersionLabels={{ [item.reviewItemId]: 'V2' }}
          entryMode="review"
          isArchived={false}
          itemActionPending={false}
          items={[item]}
          onRevokeFinalization={onRevoke}
          onSelect={vi.fn()}
          revocationUncertainIds={uncertainIds}
          unresolvedCounts={{ [item.reviewItemId]: 0 }}
        />
      </ReviewRuntimeProvider>
    );
    const rendered = render(view(new Set()));
    const revoke = screen.getByRole('button', {
      name: `撤销第${item.episode}集定稿`,
    });

    fireEvent.click(revoke);
    await waitFor(() => expect(revoke).toHaveTextContent('确认中'));
    act(() => {
      for (const callback of frames.values()) callback(0);
      frames.clear();
    });
    expect(revoke).toBeDisabled();

    rendered.rerender(view(new Set([item.reviewItemId])));
    rendered.rerender(view(new Set()));
    act(() => {
      for (const callback of frames.values()) callback(0);
      frames.clear();
    });

    await waitFor(() => expect(revoke).not.toBeDisabled());
    expect(revoke).toHaveTextContent('已定稿');
  });

  it('refreshes the successful non-current card without selecting another episode', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    runtime = createReviewRuntime();
    const current = {
      ...createSeedData().items[0],
      status: 'finalized' as const,
      activeFinalizationId: 'fin_episode_28',
    };
    const other: ReviewItem = {
      ...current,
      reviewItemId: 'item_ep29',
      episode: '29',
      title: '第 29 集',
      currentVersionId: 'ver_ep29_v1',
      activeFinalizationId: 'fin_episode_29',
    };
    const onSelect = vi.fn();
    const onRevoke = vi.fn();

    function Harness() {
      const [items, setItems] = useState<ReviewItem[]>([current, other]);
      return (
        <EpisodeStrip
          currentFileNames={{
            [current.reviewItemId]: 'episode-28.MOV',
            [other.reviewItemId]: 'episode-29.MOV',
          }}
          currentItemId={current.reviewItemId}
          currentVersionLabels={{
            [current.reviewItemId]: 'V2',
            [other.reviewItemId]: 'V1',
          }}
          entryMode="review"
          isArchived={false}
          itemActionPending={false}
          items={items}
          onRevokeFinalization={async (item) => {
            onRevoke(item);
            setItems((existing) =>
              existing.map((candidate) =>
                candidate.reviewItemId === item.reviewItemId
                  ? {
                      ...candidate,
                      status: 'in_review' as const,
                      activeFinalizationId: null,
                    }
                  : candidate,
              ),
            );
            return {} as FinalizationRevocation;
          }}
          onSelect={onSelect}
          revocationUncertainIds={new Set()}
          unresolvedCounts={{
            [current.reviewItemId]: 0,
            [other.reviewItemId]: 0,
          }}
        />
      );
    }

    render(
      <ReviewRuntimeProvider runtime={runtime}>
        <Harness />
      </ReviewRuntimeProvider>,
    );

    await userEvent.click(
      screen.getByRole('button', { name: '撤销第29集定稿' }),
    );

    await waitFor(() => expect(onRevoke).toHaveBeenCalledWith(other));
    expect(onSelect).not.toHaveBeenCalled();
    expect(screen.getByTestId(`episode-item-${current.reviewItemId}`)).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    await waitFor(() =>
      expect(
        screen.queryByRole('button', { name: '撤销第29集定稿' }),
      ).not.toBeInTheDocument(),
    );
    expect(screen.getByTestId(`episode-item-${other.reviewItemId}`).closest('article')).toHaveTextContent(
      '审阅中',
    );
  });

  it('uses the existing finalized green style and exposes yellow revoke feedback only on hover or focus', () => {
    const css = readFileSync(
      resolve(process.cwd(), 'src/modules/final-cut-review/styles/fj-review.css'),
      'utf8',
    );

    expect(css).toMatch(
      /\.fj-review-finalized-revoke\s*\{[^}]*var\(--fj-review-green\);/s,
    );
    expect(css).toMatch(
      /\.fj-review-episode-card\s*\{[^}]*border-radius:\s*6px;/s,
    );
    expect(css).toMatch(
      /\.fj-review-finalized-revoke:hover:not\(:disabled\),[\s\S]*?\{[^}]*var\(--fj-review-yellow\);/s,
    );
    expect(css).toMatch(
      /\.fj-review-finalized-revoke:hover:not\(:disabled\) \.fj-review-finalized-label,[\s\S]*?\{[^}]*display:\s*none;/s,
    );
    expect(css).toMatch(
      /\.fj-review-finalized-revoke:hover:not\(:disabled\) \.fj-review-revoke-label,[\s\S]*?\{[^}]*display:\s*inline;/s,
    );
  });
});
