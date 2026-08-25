import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { TechnicalQcProgressIndicator } from './TechnicalQcProgressIndicator';

const episodes = [
  { reviewItemId: 'item-3', episodeNo: 3, processedMs: 300, totalMs: 1_000 },
  { reviewItemId: 'item-1', episodeNo: 1, processedMs: 500, totalMs: 2_000 },
];

describe('TechnicalQcProgressIndicator', () => {
  it('shows only the running label until hover or keyboard focus opens sorted per-episode progress', async () => {
    const user = userEvent.setup();
    render(<TechnicalQcProgressIndicator episodes={episodes} />);

    const indicator = screen.getByRole('status', { name: '技术审查中' });
    expect(indicator).toHaveTextContent(/^技术审查中$/);
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();

    await user.hover(indicator);
    const tooltip = screen.getByRole('tooltip');
    expect(tooltip).toHaveTextContent('正在审查：');
    const rows = screen.getAllByTestId(/technical-qc-episode-/);
    expect(rows.map((row) => row.getAttribute('data-episode-no'))).toEqual(['1', '3']);
    expect(screen.getByTestId('technical-qc-progress-item-1')).toHaveAttribute('value', '500');
    expect(screen.getByTestId('technical-qc-progress-item-1')).toHaveAttribute('max', '2000');
    expect(tooltip).not.toHaveTextContent('项目总体进度');
    expect(tooltip).not.toHaveTextContent(/已完成\s*\d+\s*\/\s*\d+/);
    expect(tooltip).not.toHaveTextContent(/技术审查已到第/);

    await user.unhover(indicator);
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
    await user.tab();
    expect(screen.getByRole('tooltip')).toBeInTheDocument();
  });

  it('removes completed rows, accepts the next running episode, and disappears when none run', () => {
    const rendered = render(<TechnicalQcProgressIndicator episodes={episodes} />);

    rendered.rerender(
      <TechnicalQcProgressIndicator
        episodes={[{ reviewItemId: 'item-4', episodeNo: 4, processedMs: 100, totalMs: 800 }]}
      />,
    );
    expect(screen.queryByTestId('technical-qc-episode-item-1')).not.toBeInTheDocument();
    expect(screen.queryByTestId('technical-qc-episode-item-3')).not.toBeInTheDocument();
    expect(screen.getByRole('status', { name: '技术审查中' })).toBeInTheDocument();

    rendered.rerender(<TechnicalQcProgressIndicator episodes={[]} />);
    expect(screen.queryByRole('status', { name: '技术审查中' })).not.toBeInTheDocument();
  });

  it('keeps duplicate and unnumbered rows stable by review item identity across list shrink', async () => {
    const user = userEvent.setup();
    const duplicateEpisodes = [
      { reviewItemId: 'item-26-a', episodeNo: 26, processedMs: 100, totalMs: 1_000 },
      { reviewItemId: 'item-26-b', episodeNo: 26, processedMs: 200, totalMs: 1_000 },
      { reviewItemId: 'item-null-a', episodeNo: null, processedMs: 300, totalMs: 1_000 },
      { reviewItemId: 'item-null-b', episodeNo: null, processedMs: 400, totalMs: 1_000 },
    ];
    const rendered = render(<TechnicalQcProgressIndicator episodes={duplicateEpisodes} />);
    await user.hover(screen.getByRole('status', { name: '技术审查中' }));

    for (const episode of duplicateEpisodes) {
      expect(screen.getByTestId(`technical-qc-episode-${episode.reviewItemId}`)).toBeInTheDocument();
      expect(screen.getByTestId(`technical-qc-progress-${episode.reviewItemId}`)).toBeInTheDocument();
    }

    rendered.rerender(<TechnicalQcProgressIndicator episodes={duplicateEpisodes.slice(1)} />);
    expect(screen.queryByTestId('technical-qc-episode-item-26-a')).not.toBeInTheDocument();
    expect(screen.getByTestId('technical-qc-episode-item-26-b')).toHaveAttribute('data-episode-no', '26');
    expect(screen.getByTestId('technical-qc-episode-item-null-a')).toHaveAttribute('data-episode-no', '');
    expect(screen.getByTestId('technical-qc-episode-item-null-b')).toHaveAttribute('data-episode-no', '');
  });

  it('computes an opaque tooltip background from the shared surface token', async () => {
    const style = document.createElement('style');
    const css = readFileSync(
      resolve(process.cwd(), 'src/modules/final-cut-review/styles/fj-review.css'),
      'utf8',
    );
    const rootRule = css.match(/\.fj-review-root \{[\s\S]*?\n\}/)?.[0];
    const tooltipRule = css.match(/\.fj-review-technical-qc-tooltip \{[\s\S]*?\n\}/)?.[0];
    expect(rootRule).toBeDefined();
    expect(tooltipRule).toBeDefined();
    style.textContent = `${rootRule}\n${tooltipRule}`;
    document.head.append(style);
    const user = userEvent.setup();
    const rendered = render(
      <div className="fj-review-root">
        <TechnicalQcProgressIndicator episodes={episodes} />
      </div>,
    );
    await user.hover(screen.getByRole('status', { name: '技术审查中' }));

    const background = getComputedStyle(screen.getByRole('tooltip')).backgroundColor;
    expect(background).not.toBe('rgba(0, 0, 0, 0)');
    expect(background).not.toBe('transparent');
    expect(background).not.toContain('var(');

    rendered.unmount();
    style.remove();
  });
});
