import { useEffect, useId, useRef, type ReactNode } from 'react';

export function CenteredModal(props: {
  children: ReactNode;
  onClose(): void;
  open: boolean;
  pending?: boolean;
  testId: string;
  title: string;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const titleId = useId();

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (props.open && !dialog.open) {
      if (typeof dialog.showModal === 'function') dialog.showModal();
      else dialog.setAttribute('open', '');
    } else if (!props.open && dialog.open) {
      if (typeof dialog.close === 'function') dialog.close();
      else dialog.removeAttribute('open');
    }
  }, [props.open]);

  if (!props.open) return null;
  return (
    <dialog
      aria-labelledby={titleId}
      className="fj-review-centered-modal"
      data-testid={props.testId}
      onCancel={(event) => {
        if (props.pending) event.preventDefault();
        else props.onClose();
      }}
      ref={dialogRef}
    >
      <div className="fj-review-centered-modal-head">
        <h2 id={titleId}>{props.title}</h2>
        <button
          aria-label={`关闭${props.title}`}
          className="fj-review-secondary"
          disabled={props.pending}
          onClick={props.onClose}
          type="button"
        >
          关闭
        </button>
      </div>
      {props.children}
    </dialog>
  );
}
