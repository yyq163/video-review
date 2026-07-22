import { describe, expect, it } from 'vitest';
import type { EntryMode } from '../contracts/types';
import { entryLinksFor } from './entry-links';

describe.each([
  {
    entryMode: 'edit',
    expected: [{ href: '/edit/projects', label: '剪辑入口', active: true }],
  },
  {
    entryMode: 'review',
    expected: [
      { href: '/edit/projects', label: '剪辑入口', active: false },
      { href: '/review/projects', label: '成片审阅', active: true },
    ],
  },
] satisfies Array<{ entryMode: EntryMode; expected: ReturnType<typeof entryLinksFor> }>)('$entryMode entry links', ({ entryMode, expected }) => {
  it('returns the approved asymmetric navigation', () => {
    expect(entryLinksFor(entryMode)).toEqual(expected);
  });
});
