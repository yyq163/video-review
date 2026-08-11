import { useEffect, useId, useRef, useState } from 'react';
import type { EntryMode, PackageResult } from '../contracts/types';
import { validateUnifiedPackageName } from '../core/package-name';
import { actionError, CapabilityGate } from './shared';

export function ProjectPackageControl(props: {
  entryMode: EntryMode;
  projectRefId: string;
  disabled: boolean;
  createPending: boolean;
  downloadPending: boolean;
  onCreate(input: { projectRefId: string; unifiedName: string }): Promise<PackageResult>;
  onDownload(result: PackageResult): Promise<void>;
  onSuccess(): void;
}) {
  const [open, setOpen] = useState(false);
  const [unifiedName, setUnifiedName] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [stage, setStage] = useState<'idle' | 'preparing' | 'downloading'>('idle');
  const dialogRef = useRef<HTMLDialogElement>(null);
  const inFlightRef = useRef(false);
  const titleId = useId();
  const pending = props.createPending || props.downloadPending || stage !== 'idle';

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      if (typeof dialog.showModal === 'function') dialog.showModal();
      else dialog.setAttribute('open', '');
    } else if (!open && dialog.open) {
      if (typeof dialog.close === 'function') dialog.close();
      else dialog.removeAttribute('open');
    }
    return () => {
      if (!dialog.open) return;
      if (typeof dialog.close === 'function') dialog.close();
      else dialog.removeAttribute('open');
    };
  }, [open]);

  const dismiss = () => {
    if (pending) return;
    setOpen(false);
    setError(null);
  };

  return (
    <CapabilityGate entryMode={props.entryMode} capability="review.package.create">
      <button
        aria-haspopup="dialog"
        className="fj-review-secondary"
        data-testid="package-project"
        disabled={props.disabled || pending}
        onClick={() => {
          setUnifiedName('');
          setError(null);
          setStage('idle');
          setOpen(true);
        }}
        type="button"
      >
        {stage === 'preparing'
          ? '项目包准备中'
          : stage === 'downloading'
            ? '项目包完成，正在下载'
            : '打包项目定稿原片'}
      </button>
      {open ? (
        <dialog
          aria-labelledby={titleId}
          className="fj-review-side-form"
          data-testid="project-package-name-dialog"
          onCancel={(event) => {
            if (pending) event.preventDefault();
            else dismiss();
          }}
          ref={dialogRef}
        >
          <h2 id={titleId}>统一命名定稿原片</h2>
          <form
            className="fj-review-form"
            onSubmit={(event) => {
              event.preventDefault();
              const validationError = validateUnifiedPackageName(unifiedName);
              if (validationError) {
                setError(validationError);
                return;
              }
              if (inFlightRef.current) return;
              inFlightRef.current = true;
              setError(null);
              setStage('preparing');
              void props.onCreate({ projectRefId: props.projectRefId, unifiedName })
                .then(async (result) => {
                  setStage('downloading');
                  await props.onDownload(result);
                  props.onSuccess();
                  setOpen(false);
                })
                .catch((caught: unknown) => setError(actionError(caught)))
                .finally(() => {
                  inFlightRef.current = false;
                  setStage('idle');
                });
            }}
          >
            <label>
              <span>统一名称</span>
              <input
                aria-label="统一名称"
                aria-describedby={`${titleId}-help`}
                autoFocus
                disabled={pending}
                onChange={(event) => setUnifiedName(event.target.value)}
                required
                value={unifiedName}
              />
              <small id={`${titleId}-help`}>ZIP 内文件将使用“统一名称-第实际集数集.原扩展名”。</small>
            </label>
            <button className="fj-review-primary" disabled={pending} type="submit">
              {stage === 'preparing' ? '准备中' : stage === 'downloading' ? '正在下载' : '确认并下载'}
            </button>
            <button className="fj-review-secondary" disabled={pending} onClick={dismiss} type="button">
              取消
            </button>
            {error ? <div className="fj-review-error">{error}</div> : null}
          </form>
        </dialog>
      ) : null}
    </CapabilityGate>
  );
}
