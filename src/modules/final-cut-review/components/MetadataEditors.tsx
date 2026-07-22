import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { zodResolver } from '@hookform/resolvers/zod';
import { Pencil } from 'lucide-react';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import type { Project } from '../contracts/types';
import type { ReviewItemWithMetadata } from '../ports';
import { ProjectForm, type ProjectFormValues } from './ProjectForms';
import { actionError } from './shared';

const reviewItemMetadataSchema = z.object({
  title: z.string().trim().min(1, '成片标题必填').max(512, '成片标题最多 512 个字符'),
  episode: z
    .string()
    .trim()
    .min(1, '集数必填')
    .refine((value) => {
      const numericValue = Number(value);
      return !Number.isFinite(numericValue) || (Number.isInteger(numericValue) && numericValue >= 1);
    }, '数字集数必须是大于等于 1 的整数'),
});

export type ReviewItemMetadataValues = z.infer<typeof reviewItemMetadataSchema>;

function MetadataDialog(props: {
  children: ReactNode;
  onDismiss(): void;
  open: boolean;
  pending: boolean;
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
    return () => {
      if (!dialog.open) return;
      if (typeof dialog.close === 'function') dialog.close();
      else dialog.removeAttribute('open');
    };
  }, [props.open]);

  if (!props.open) return null;

  return (
    <dialog
      aria-labelledby={titleId}
      className="fj-review-side-form"
      data-testid={props.testId}
      onCancel={(event) => {
        if (props.pending) event.preventDefault();
        else props.onDismiss();
      }}
      ref={dialogRef}
    >
      <h2 id={titleId}>{props.title}</h2>
      {props.children}
    </dialog>
  );
}

function EditMetadataLabel(props: { children: ReactNode }) {
  return (
    <span className="fj-review-icon-text">
      <Pencil />
      {props.children}
    </span>
  );
}

export function ProjectMetadataEditor(props: {
  project: Project;
  pending: boolean;
  onSubmit(values: ProjectFormValues): Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  return (
    <>
      <button
        aria-haspopup="dialog"
        className="fj-review-secondary"
        data-testid="project-metadata-edit-trigger"
        disabled={props.pending}
        type="button"
        onClick={() => {
          setError(null);
          setEditing(true);
        }}
      >
        <EditMetadataLabel>编辑项目资料</EditMetadataLabel>
      </button>
      <MetadataDialog
        onDismiss={() => setEditing(false)}
        open={editing}
        pending={props.pending}
        testId="project-metadata-editor"
        title="编辑项目资料"
      >
        <ProjectForm
          codeReadOnly
          defaultValues={{
            name: props.project.name,
            code: props.project.code,
            description: props.project.description,
          }}
          pending={props.pending}
          submitLabel="保存项目资料"
          onSubmit={async (values) => {
            setError(null);
            try {
              await props.onSubmit(values);
              setEditing(false);
            } catch (caught) {
              setError(actionError(caught));
            }
          }}
        />
        <button className="fj-review-secondary" disabled={props.pending} type="button" onClick={() => setEditing(false)}>
          取消
        </button>
        {error ? <div className="fj-review-error">{error}</div> : null}
      </MetadataDialog>
    </>
  );
}

export function ReviewItemMetadataEditor(props: {
  item: ReviewItemWithMetadata;
  pending: boolean;
  onSubmit(item: ReviewItemWithMetadata, values: ReviewItemMetadataValues): Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const form = useForm<ReviewItemMetadataValues>({
    resolver: zodResolver(reviewItemMetadataSchema),
    defaultValues: { title: props.item.title, episode: props.item.episode },
  });
  const finalized = props.item.status === 'finalized';

  const beginEditing = () => {
    form.reset({ title: props.item.title, episode: props.item.episode });
    setError(null);
    setEditing(true);
  };

  if (finalized) return null;

  return (
    <>
      <button
        aria-haspopup="dialog"
        aria-label={`编辑成片元数据 ${props.item.title}`}
        className="fj-review-secondary"
        data-testid={`review-item-metadata-trigger-${props.item.reviewItemId}`}
        disabled={props.pending}
        type="button"
        onClick={beginEditing}
      >
        <EditMetadataLabel>编辑资料</EditMetadataLabel>
      </button>
      <MetadataDialog
        onDismiss={() => setEditing(false)}
        open={editing}
        pending={props.pending}
        testId={`review-item-metadata-${props.item.reviewItemId}`}
        title={`编辑成片元数据：${props.item.title}`}
      >
        <form
          className="fj-review-form"
          onSubmit={form.handleSubmit(async (values) => {
            setError(null);
            if (!/^\d+$/.test(values.episode) && values.episode !== props.item.itemCode) {
              form.setError('episode', { message: '非数字成片编号不可修改' });
              return;
            }
            try {
              await props.onSubmit(props.item, values);
              setEditing(false);
            } catch (caught) {
              setError(actionError(caught));
            }
          })}
        >
          <label>
            <span>成片编号</span>
            <input aria-label={`成片编号 ${props.item.title}`} readOnly value={props.item.itemCode} />
          </label>
          <label>
            <span>成片标题</span>
            <input {...form.register('title')} />
            {form.formState.errors.title ? <em>{form.formState.errors.title.message}</em> : null}
          </label>
          <label>
            <span>集数</span>
            <input
              inputMode={/^\d+$/.test(props.item.episode) ? 'numeric' : 'text'}
              readOnly={!/^\d+$/.test(props.item.episode)}
              {...form.register('episode')}
            />
            {form.formState.errors.episode ? <em>{form.formState.errors.episode.message}</em> : null}
            {!/^\d+$/.test(props.item.episode) ? <small>非数字成片编号保持不变，本次只更新标题。</small> : null}
          </label>
          <button className="fj-review-primary" disabled={props.pending} type="submit">
            {props.pending ? '保存中' : '保存成片元数据'}
          </button>
          <button className="fj-review-secondary" disabled={props.pending} type="button" onClick={() => setEditing(false)}>
            取消
          </button>
        </form>
        {error ? <div className="fj-review-error">{error}</div> : null}
      </MetadataDialog>
    </>
  );
}
