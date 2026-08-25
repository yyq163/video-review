import { useState } from 'react';
import type { ResponsibleEditor } from '../contracts/types';
import { CenteredModal } from './CenteredModal';
import { actionError } from './shared';

export function ResponsibleEditorManagerModal(props: {
  editors: readonly ResponsibleEditor[];
  open: boolean;
  pending?: boolean;
  onClose(): void;
  onSave(editor: ResponsibleEditor | null, displayName: string): Promise<void>;
}) {
  const initialEditor = props.editors[0] ?? null;
  const [editingEditorId, setEditingEditorId] = useState<string | null>(
    initialEditor?.responsibleEditorId ?? null,
  );
  const [displayName, setDisplayName] = useState(initialEditor?.displayName ?? '');
  const [saveError, setSaveError] = useState<string | null>(null);

  const current = props.editors.find(
    (editor) => editor.responsibleEditorId === editingEditorId,
  ) ?? null;

  return (
    <CenteredModal
      onClose={props.onClose}
      open={props.open}
      pending={props.pending}
      testId="responsible-editor-modal"
      title="编辑责任剪辑师"
    >
      <label>
        <span>待编辑责任剪辑师</span>
        <select
          aria-label="待编辑责任剪辑师"
          disabled={props.pending}
          value={current?.responsibleEditorId ?? ''}
          onChange={(event) => {
            const editor = props.editors.find(
              (candidate) => candidate.responsibleEditorId === event.target.value,
            ) ?? null;
            setEditingEditorId(editor?.responsibleEditorId ?? null);
            setDisplayName(editor?.displayName ?? '');
            setSaveError(null);
          }}
        >
          <option value="">新建责任剪辑师</option>
          {props.editors.map((editor) => (
            <option key={editor.responsibleEditorId} value={editor.responsibleEditorId}>
              {editor.displayName}
            </option>
          ))}
        </select>
      </label>
      <label>
        <span>显示名称</span>
        <input
          aria-label="责任剪辑师显示名称"
          disabled={props.pending}
          value={displayName}
          onChange={(event) => {
            setDisplayName(event.target.value);
            setSaveError(null);
          }}
        />
      </label>
      {saveError ? <span className="fj-review-form-error" role="alert">{saveError}</span> : null}
      <button
        className="fj-review-primary"
        disabled={props.pending || !displayName.trim()}
        onClick={() => {
          setSaveError(null);
          void props.onSave(current, displayName).catch((error: unknown) => {
            setSaveError(actionError(error));
          });
        }}
        type="button"
      >
        {props.pending ? '保存中...' : '保存责任剪辑师'}
      </button>
    </CenteredModal>
  );
}
