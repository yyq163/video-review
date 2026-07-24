import { describe, expect, it, vi } from 'vitest';
import { settleWithConcurrencyLimit } from './bounded-concurrency';

describe('settleWithConcurrencyLimit', () => {
  it('caps destructive work, preserves input order, and continues after an item failure', async () => {
    let active = 0;
    let maxActive = 0;
    const task = vi.fn(async (value: number) => {
      active += 1;
      maxActive = Math.max(maxActive, active);
      await new Promise((resolve) => window.setTimeout(resolve, 5));
      active -= 1;
      if (value === 3) throw new Error('one item failed');
      return value * 10;
    });

    const results = await settleWithConcurrencyLimit(
      [1, 2, 3, 4, 5, 6, 7],
      3,
      task,
    );

    expect(maxActive).toBe(3);
    expect(task).toHaveBeenCalledTimes(7);
    expect(results).toHaveLength(7);
    expect(results[0]).toEqual({ status: 'fulfilled', value: 10 });
    expect(results[2]).toEqual({
      status: 'rejected',
      reason: expect.objectContaining({ message: 'one item failed' }),
    });
    expect(results[6]).toEqual({ status: 'fulfilled', value: 70 });
  });
});
