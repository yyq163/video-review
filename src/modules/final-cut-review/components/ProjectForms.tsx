import { zodResolver } from '@hookform/resolvers/zod';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import { createDemoVideoFile } from '../core/demo-video';

export const projectSchema = z.object({
  name: z.string().trim().min(1, '项目名称必填').max(255, '项目名称最多 255 个字符'),
  code: z.string().trim().min(1, '项目编号必填').max(128, '项目编号最多 128 个字符'),
  description: z.string().trim().max(2000, '项目说明最多 2000 个字符'),
});

export type ProjectFormValues = z.infer<typeof projectSchema>;

export function ProjectForm(props: {
  defaultValues?: ProjectFormValues;
  codeReadOnly?: boolean;
  submitLabel: string;
  pending?: boolean;
  onSubmit(values: ProjectFormValues): void;
}) {
  const form = useForm<ProjectFormValues>({
    resolver: zodResolver(projectSchema),
    defaultValues: props.defaultValues ?? {
      name: '',
      code: '',
      description: '',
    },
  });

  return (
    <form className="fj-review-form" onSubmit={form.handleSubmit(props.onSubmit)} data-testid="project-form">
      <label>
        <span>项目名称</span>
        <input {...form.register('name')} placeholder="真千金是男的" />
        {form.formState.errors.name && <em>{form.formState.errors.name.message}</em>}
      </label>
      <label>
        <span>{props.codeReadOnly ? '项目编号' : '项目编码'}</span>
        <input {...form.register('code')} placeholder="FJ-EP28" readOnly={props.codeReadOnly} />
        {form.formState.errors.code && <em>{form.formState.errors.code.message}</em>}
      </label>
      <label>
        <span>项目说明</span>
        <textarea {...form.register('description')} placeholder="用于成片审阅和定稿下载的项目说明" />
        {form.formState.errors.description && <em>{form.formState.errors.description.message}</em>}
      </label>
      <button className="fj-review-primary" type="submit" disabled={props.pending}>
        {props.pending ? '提交中' : props.submitLabel}
      </button>
    </form>
  );
}

export const uploadSchema = z.object({
  title: z.string().trim().min(1, '成片标题必填').max(512, '成片标题最多 512 个字符'),
  episode: z
    .string()
    .trim()
    .min(1, '集数必填')
    .refine((value) => {
      const numericValue = Number(value);
      return !Number.isFinite(numericValue) || (Number.isInteger(numericValue) && numericValue >= 1);
    }, '数字集数必须是大于等于 1 的整数'),
  file: z.instanceof(File, { message: '请选择原片文件' }),
});

export type UploadFormValues = z.infer<typeof uploadSchema>;

export function makeDemoFile(name: string): File {
  return createDemoVideoFile(name.replace(/\.[^.]+$/, '.webm'));
}
