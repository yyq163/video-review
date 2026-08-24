import type { ResponsibleEditor } from '../contracts/types';
import { invariant } from '../core/errors';
import { createUuid } from '../core/uuid';
import { nowIso } from './in-memory-review-clones';
import type { InMemoryReviewStore } from './in-memory-review-store';

function validateDisplayName(value: string): string {
  const displayName = value.trim();
  invariant(
    displayName.length >= 1 && displayName.length <= 255,
    '责任剪辑师名称长度必须在 1 到 255 之间',
    'VALIDATION_ERROR',
  );
  return displayName;
}

export class InMemoryReviewEditors {
  constructor(private readonly store: InMemoryReviewStore) {}

  readonly listResponsibleEditors = async (): Promise<ResponsibleEditor[]> =>
    [...this.store.responsibleEditors.values()]
      .sort((left, right) => left.displayName.localeCompare(right.displayName, 'zh-CN'))
      .map((editor) => ({ ...editor }));

  readonly createResponsibleEditor = async (input: { displayName: string }): Promise<ResponsibleEditor> => {
    const timestamp = nowIso();
    const editor: ResponsibleEditor = {
      responsibleEditorId: `editor_${createUuid()}`,
      displayName: validateDisplayName(input.displayName),
      lockVersion: 1,
      createdAt: timestamp,
      updatedAt: timestamp,
    };
    this.store.responsibleEditors.set(editor.responsibleEditorId, editor);
    this.store.emitChange();
    return { ...editor };
  };

  readonly renameResponsibleEditor = async (input: {
    responsibleEditorId: string;
    displayName: string;
    lockVersion?: number;
  }): Promise<ResponsibleEditor> => {
    const current = this.store.getResponsibleEditor(input.responsibleEditorId);
    if (input.lockVersion !== undefined) {
      invariant(
        input.lockVersion === current.lockVersion,
        '责任剪辑师资料已变化，请刷新后重试',
        'OPTIMISTIC_LOCK_CONFLICT',
      );
    }
    const next = {
      ...current,
      displayName: validateDisplayName(input.displayName),
      lockVersion: current.lockVersion + 1,
      updatedAt: nowIso(),
    };
    this.store.responsibleEditors.set(next.responsibleEditorId, next);
    this.store.emitChange();
    return { ...next };
  };
}
