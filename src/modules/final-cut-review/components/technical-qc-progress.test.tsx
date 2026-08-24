import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { TechnicalQcProgressIndicator } from './TechnicalQcProgressIndicator';

const episodes = [
  { episodeNo: 3, processedMs: 300, totalMs: 1_000 },
  { episodeNo: 1, processedMs: 500, totalMs: 2_000 },
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
    expect(screen.getByTestId('technical-qc-progress-1')).toHaveAttribute('value', '500');
    expect(screen.getByTestId('technical-qc-progress-1')).toHaveAttribute('max', '2000');
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
        episodes={[{ episodeNo: 4, processedMs: 100, totalMs: 800 }]}
      />,
    );
    expect(screen.queryByTestId('technical-qc-episode-1')).not.toBeInTheDocument();
    expect(screen.queryByTestId('technical-qc-episode-3')).not.toBeInTheDocument();
    expect(screen.getByRole('status', { name: '技术审查中' })).toBeInTheDocument();

    rendered.rerender(<TechnicalQcProgressIndicator episodes={[]} />);
    expect(screen.queryByRole('status', { name: '技术审查中' })).not.toBeInTheDocument();
  });
});
