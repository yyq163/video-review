import type { EntryMode } from '../contracts/types';

export interface EntryNavigationLink {
  readonly href: '/edit/projects' | '/review/projects';
  readonly label: '剪辑入口' | '成片审阅';
  readonly active: boolean;
}

const EDIT_ENTRY_LINKS = [
  { href: '/edit/projects', label: '剪辑入口', active: true },
] as const satisfies readonly EntryNavigationLink[];

const REVIEW_ENTRY_LINKS = [
  { href: '/edit/projects', label: '剪辑入口', active: false },
  { href: '/review/projects', label: '成片审阅', active: true },
] as const satisfies readonly EntryNavigationLink[];

export function entryLinksFor(entryMode: EntryMode): readonly EntryNavigationLink[] {
  return entryMode === 'edit' ? EDIT_ENTRY_LINKS : REVIEW_ENTRY_LINKS;
}
