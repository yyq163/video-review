import { useState } from 'react';
import { UploadCloud } from 'lucide-react';
import type { UploadProgress } from '../contracts/types';

const FILE_REQUIRED_MESSAGE = '请选择原片文件。';
const CREATE_ITEM_FILE_ERROR_ID = 'create-item-file-error';
const CREATE_ITEM_SUBMISSION_ERROR_ID = 'create-item-upload-error';
const APPEND_VERSION_FILE_ERROR_ID = 'append-version-file-error';
const APPEND_VERSION_SUBMISSION_ERROR_ID = 'append-version-upload-error';
const APPEND_VERSION_REASON_ERROR_ID = 'append-version-supersede-reason-error';
const ACCEPTED_VIDEO_TYPES = '.mp4,.m4v,.mov,.qt,video/mp4,video/quicktime';
export const DEFAULT_CREATE_ITEM_TITLE = '第 28 集 · 最终成片';
export const DEFAULT_CREATE_ITEM_EPISODE = '28';

function UploadProgressView(props: { progress?: UploadProgress }) {
  if (!props.progress) return null;
  const percent = Math.min(100, Math.max(0, Math.round(props.progress.percent)));
  const stageLabels: Record<UploadProgress['stage'], string> = {
    validating: '校验文件',
    initiated: '创建上传会话',
    uploading: '上传分片',
    binding: '绑定成片记录',
    completed: '上传完成',
  };
  return (
    <div
      className="fj-review-upload-progress"
      data-testid="upload-progress"
      role="status"
      aria-label={`${stageLabels[props.progress.stage]} ${percent}%`}
      aria-live="polite"
    >
      <span className="fj-review-sr-only">
        {stageLabels[props.progress.stage]} {percent}%
      </span>
      <div className="fj-review-upload-progress-track" aria-hidden="true">
        <span style={{ width: `${percent}%` }} />
      </div>
    </div>
  );
}

export function CreateItemUploadPanel(props: {
  initialTitle?: string;
  initialEpisode?: string;
  pending?: boolean;
  blockedForListConfirmation?: boolean;
  progress?: UploadProgress;
  onDraftChange?(draft: { title: string; episode: string }): void;
  onSubmit(input: { title: string; episode: string; file: File }): void | Promise<void>;
}) {
  const [title, setTitle] = useState(props.initialTitle ?? DEFAULT_CREATE_ITEM_TITLE);
  const [episode, setEpisode] = useState(props.initialEpisode ?? DEFAULT_CREATE_ITEM_EPISODE);
  const [file, setFile] = useState<File | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [submissionError, setSubmissionError] = useState<string | null>(null);
  const fileErrorIds = [
    fileError ? CREATE_ITEM_FILE_ERROR_ID : null,
    submissionError ? CREATE_ITEM_SUBMISSION_ERROR_ID : null,
  ].filter(Boolean).join(' ') || undefined;
  const disabled = Boolean(props.pending || props.blockedForListConfirmation);
  const updateTitle = (nextTitle: string) => {
    setTitle(nextTitle);
    props.onDraftChange?.({ title: nextTitle, episode });
  };
  const updateEpisode = (nextEpisode: string) => {
    setEpisode(nextEpisode);
    props.onDraftChange?.({ title, episode: nextEpisode });
  };

  return (
    <form
      className="fj-review-upload-panel"
      data-testid="create-item-upload"
      onSubmit={(event) => {
        event.preventDefault();
        if (!file) {
          setFileError(FILE_REQUIRED_MESSAGE);
          return;
        }
        setFileError(null);
        setSubmissionError(null);
        void Promise.resolve(props.onSubmit({ title, episode, file })).catch((error: unknown) => {
          setSubmissionError(error instanceof Error ? error.message : '上传失败，请重试。');
        });
      }}
    >
      <div>
        <UploadCloud />
        <strong>创建成片并上传 V1</strong>
        <span>原片校验通过后会安全上传并创建 V1。</span>
      </div>
      <label>
        <span>成片标题</span>
        <input value={title} disabled={disabled} onChange={(event) => updateTitle(event.target.value)} />
      </label>
      <label>
        <span>集数</span>
        <input value={episode} disabled={disabled} onChange={(event) => updateEpisode(event.target.value)} />
      </label>
      <label>
        <span>原片文件</span>
        <input
          data-testid="create-item-file"
          type="file"
          accept={ACCEPTED_VIDEO_TYPES}
          aria-describedby={fileErrorIds}
          aria-invalid={Boolean(fileError || submissionError)}
          disabled={disabled}
          onChange={(event) => {
            setFile(event.currentTarget.files?.[0] ?? null);
            setFileError(null);
            setSubmissionError(null);
          }}
        />
      </label>
      {fileError ? <span className="fj-review-form-error" data-testid="create-item-file-error" id={CREATE_ITEM_FILE_ERROR_ID} role="alert">{fileError}</span> : null}
      {submissionError ? <span className="fj-review-form-error" id={CREATE_ITEM_SUBMISSION_ERROR_ID} role="alert">{submissionError}</span> : null}
      <UploadProgressView progress={props.progress} />
      <button className="fj-review-primary" type="submit" disabled={disabled}>
        {props.blockedForListConfirmation ? '请先确认列表' : props.pending ? '上传中...' : '上传 V1'}
      </button>
    </form>
  );
}

export function AppendVersionPanel(props: {
  nextLabel: string;
  pending?: boolean;
  progress?: UploadProgress;
  requiresSupersedeReason?: boolean;
  onSubmit(input: { file: File; versionNote: string; changeSummary: string; supersedeReason: string }): void | Promise<void>;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [versionNote, setVersionNote] = useState(`${props.nextLabel} 版本说明`);
  const [changeSummary, setChangeSummary] = useState('按审阅意见完成本轮修改。');
  const [supersedeReason, setSupersedeReason] = useState(props.requiresSupersedeReason ? '审阅前主动补版。' : '');
  const [fileError, setFileError] = useState<string | null>(null);
  const [submissionError, setSubmissionError] = useState<string | null>(null);
  const supersedeMissing = props.requiresSupersedeReason && !supersedeReason.trim();
  const fileErrorIds = [
    fileError ? APPEND_VERSION_FILE_ERROR_ID : null,
    submissionError ? APPEND_VERSION_SUBMISSION_ERROR_ID : null,
  ].filter(Boolean).join(' ') || undefined;

  return (
    <form
      className="fj-review-inline-upload"
      data-testid="append-version-panel"
      onSubmit={(event) => {
        event.preventDefault();
        if (supersedeMissing) return;
        if (!file) {
          setFileError(FILE_REQUIRED_MESSAGE);
          return;
        }
        setFileError(null);
        setSubmissionError(null);
        void Promise.resolve(props.onSubmit({
          file,
          versionNote,
          changeSummary,
          supersedeReason,
        })).catch((error: unknown) => {
          setSubmissionError(error instanceof Error ? error.message : '上传失败，请重试。');
        });
      }}
    >
      <div className="fj-review-inline-upload-summary">
        <strong>追加 {props.nextLabel}</strong>
        <span>填写版本元数据后上传新版本。</span>
      </div>
      <label className="fj-review-inline-upload-file">
        <span>原始视频文件</span>
        <input
          data-testid="append-version-file"
          type="file"
          accept={ACCEPTED_VIDEO_TYPES}
          aria-describedby={fileErrorIds}
          aria-invalid={Boolean(fileError || submissionError)}
          disabled={props.pending}
          onChange={(event) => {
            setFile(event.currentTarget.files?.[0] ?? null);
            setFileError(null);
            setSubmissionError(null);
          }}
        />
      </label>
      <label className="fj-review-inline-upload-note">
        <span>版本说明</span>
        <input
          data-testid="append-version-note"
          value={versionNote}
          disabled={props.pending}
          onChange={(event) => setVersionNote(event.target.value)}
        />
      </label>
      <label className="fj-review-inline-upload-change">
        <span>本次修改说明</span>
        <textarea
          data-testid="append-version-change-summary"
          value={changeSummary}
          disabled={props.pending}
          onChange={(event) => setChangeSummary(event.target.value)}
        />
      </label>
      <label className="fj-review-inline-upload-reason">
        <span>{props.requiresSupersedeReason ? '主动补版原因（必填）' : '主动补版原因'}</span>
        <textarea
          data-testid="append-version-supersede-reason"
          value={supersedeReason}
          aria-describedby={supersedeMissing ? APPEND_VERSION_REASON_ERROR_ID : undefined}
          aria-invalid={supersedeMissing}
          disabled={props.pending}
          onChange={(event) => setSupersedeReason(event.target.value)}
        />
      </label>
      {fileError ? <span className="fj-review-form-error fj-review-inline-upload-file-error" data-testid="append-version-file-error" id={APPEND_VERSION_FILE_ERROR_ID} role="alert">{fileError}</span> : null}
      {submissionError ? <span className="fj-review-form-error" id={APPEND_VERSION_SUBMISSION_ERROR_ID} role="alert">{submissionError}</span> : null}
      <UploadProgressView progress={props.progress} />
      <button className="fj-review-secondary fj-review-inline-upload-submit" disabled={props.pending || supersedeMissing} type="submit">
        {props.pending ? '上传中...' : `确认追加 ${props.nextLabel}`}
      </button>
      {supersedeMissing ? <span className="fj-review-form-error fj-review-inline-upload-reason-error" id={APPEND_VERSION_REASON_ERROR_ID} role="alert">待审状态主动补版必须填写原因。</span> : null}
    </form>
  );
}
