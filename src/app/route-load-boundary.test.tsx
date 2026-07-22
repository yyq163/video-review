import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { RouteLoadBoundary, RouteLoading } from './RouteLoadBoundary';

function BrokenRoute(): never {
  throw new Error('synthetic chunk failure');
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('route loading boundary', () => {
  it('exposes a visible loading status', () => {
    render(<RouteLoading />);
    expect(screen.getByRole('status')).toHaveTextContent('正在加载页面');
  });

  it('exposes a reload recovery command when a lazy route fails', () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    render(
      <RouteLoadBoundary>
        <BrokenRoute />
      </RouteLoadBoundary>,
    );
    expect(screen.getByRole('alert')).toHaveTextContent('页面资源加载失败');
    expect(screen.getByRole('button', { name: '重新加载页面' })).toBeInTheDocument();
  });
});
