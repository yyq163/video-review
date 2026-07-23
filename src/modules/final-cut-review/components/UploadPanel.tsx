import { useRef, useState } from 'react';
import { UploadCloud } from 'lucide-react';
import type { UploadProgress } from '../contracts/types';

const FILE_REQUIRED_MESSAGE = '请选择原片文件。';
const APPEND_VERSION_FILE_ERROR_ID = 'append-version-file-error';
const APPEND_VERSION_SUBMISSION_ERROR_ID = 'append-version-upload-error';
const ACCEPTED_VIDEO_TYPES = '.mp4,.m4v,.mov,.qt,video/mp4,video/quicktime';
const MAX_CONCURRENT_V1_UPLOADS = 5;
const UPLOAD_STAGE_LABELS: Record<UploadProgress['stage'], string> = {
  validating: '校验文件',
  initiated: '创建上传会话',
  uploading: '上传分片',
  binding: '绑定成片记录',
  completed: '上传完成',
};

export interface CreateItemUploadInput {
  title: string;
  episode: string;
  file: File;
}

export type CreateItemUploadOutcome =
  | { outcome: 'success'; stopBatch?: boolean }
  | { outcome: 'failed' | 'uncertain'; message: string; stopBatch?: boolean };

type CreateItemUploadRowStatus = 'ready' | 'queued' | 'uploading' | 'failed' | 'uncertain';

interface CreateItemUploadRow extends CreateItemUploadInput {
  id: string;
  error?: string;
  failureStage?: UploadProgress['stage'];
  progress?: UploadProgress;
  status: CreateItemUploadRowStatus;
}

export function titleFromUploadFileName(fileName: string): string {
  return fileName.replace(/\.[^.]+$/, '');
}

export function episodeFromUploadFileName(fileName: string): string {
  const title = titleFromUploadFileName(fileName);
  const explicit = title.match(/第(\d+)集/);
  if (explicit) return explicit[1];
  const candidates = title.match(/(?<!\d)\d{1,3}(?!\d)/g) ?? [];
  return candidates.length === 1 ? candidates[0] : '';
}

function UploadProgressView(props: { progress?: UploadProgress; testId?: string }) {
  if (!props.progress) return null;
  const percent = Math.min(100, Math.max(0, Math.round(props.progress.percent)));
  return (
    <div
      className="fj-review-upload-progress"
      data-testid={props.testId ?? 'upload-progress'}
      role="status"
      aria-label={`${UPLOAD_STAGE_LABELS[props.progress.stage]} ${percent}%`}
      aria-live="polite"
    >
      <span className="fj-review-sr-only">
        {UPLOAD_STAGE_LABELS[props.progress.stage]} {percent}%
      </span>
      <div className="fj-review-upload-progress-track" aria-hidden="true">
        <span style={{ width: `${percent}%` }} />
      </div>
    </div>
  );
}

export function CreateItemUploadPanel(props: {
  pending?: boolean;
  blockedForListConfirmation?: boolean;
  onSubmit(
    input: CreateItemUploadInput,
    onProgress?: (progress: UploadProgress) => void,
  ): CreateItemUploadOutcome | Promise<CreateItemUploadOutcome>;
}) {
  const nextRowId = useRef(0);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [rows, setRows] = useState<CreateItemUploadRow[]>([]);
  const [uploading, setUploading] = useState(false);
  const [batchCompleted, setBatchCompleted] = useState(false);
  const [batchError, setBatchError] = useState<string | null>(null);
  const disabled = Boolean(props.pending || uploading || props.blockedForListConfirmation);
  const missingRequired = rows.some((row) => !row.title.trim() || !row.episode.trim());
  const readyRows = rows.filter((row) => row.status === 'ready');
  const recoverableRows = rows.filter((row) => row.status === 'failed' || row.status === 'uncertain');
  const updateRow = (id: string, patch: Partial<Pick<CreateItemUploadRow, 'title' | 'episode'>>) => {
    setRows((current) => current.map((row) => row.id === id ? { ...row, ...patch } : row));
    setBatchError(null);
  };
  const runBatch = async (batch: CreateItemUploadRow[]) => {
    if (!batch.length || uploading) return;
    setUploading(true);
    setBatchCompleted(false);
    setBatchError(null);
    if (fileInputRef.current) fileInputRef.current.value = '';
    const batchIds = new Set(batch.map((row) => row.id));
    setRows((current) => current.map((row) =>
      batchIds.has(row.id)
        ? { ...row, status: 'queued', error: undefined, progress: undefined }
        : row,
    ));

    let nextIndex = 0;
    let stopQueuedWork = false;
    let stoppedForSafety = false;
    const runWorker = async () => {
      while (!stopQueuedWork) {
        const row = batch[nextIndex];
        nextIndex += 1;
        if (!row) return;
        setRows((current) => current.map((candidate) =>
          candidate.id === row.id
            ? { ...candidate, status: 'uploading', error: undefined, progress: undefined }
            : candidate,
        ));
        let result: CreateItemUploadOutcome;
        let failureStage: UploadProgress['stage'] = 'validating';
        try {
          result = await props.onSubmit(
            { title: row.title, episode: row.episode, file: row.file },
            (progress) => {
              failureStage = progress.stage;
              setRows((current) => current.map((candidate) =>
                candidate.id === row.id ? { ...candidate, progress } : candidate,
              ));
            },
          );
        } catch {
          result = {
            outcome: 'uncertain',
            message: '结果不确定/原因未确认：上传响应丢失，请先核对待审列表。',
          };
        }
        if (result.outcome === 'success') {
          setRows((current) => current.filter((candidate) => candidate.id !== row.id));
        } else {
          const message = result.outcome === 'uncertain' && !result.message.includes('结果不确定')
            ? `结果不确定/原因未确认：${result.message}`
            : result.message;
          setRows((current) => current.map((candidate) =>
            candidate.id === row.id
              ? { ...candidate, status: result.outcome, error: message, failureStage, progress: undefined }
              : candidate,
          ));
        }
        if (result.stopBatch) {
          stopQueuedWork = true;
          stoppedForSafety = true;
        }
      }
    };

    try {
      await Promise.all(
        Array.from({ length: Math.min(MAX_CONCURRENT_V1_UPLOADS, batch.length) }, () => runWorker()),
      );
    } finally {
      if (stoppedForSafety) {
        setRows((current) => current.map((row) =>
          batchIds.has(row.id) && row.status === 'queued' ? { ...row, status: 'ready' } : row,
        ));
        setBatchError('安全保护不可用，尚未开始的素材已停止；已成功项不会重传。');
      }
      setUploading(false);
      setBatchCompleted(true);
    }
  };

  return (
    <form
      className="fj-review-upload-panel"
      data-testid="create-item-upload"
      onSubmit={(event) => {
        event.preventDefault();
        if (!rows.length) {
          setBatchError(FILE_REQUIRED_MESSAGE);
          return;
        }
        if (missingRequired) {
          setBatchError('请补齐每一条成片的标题和集数后再上传。');
          return;
        }
        void runBatch(readyRows);
      }}
    >
      <div>
        <UploadCloud />
        <strong>创建成片并上传 V1</strong>
        <span>原片校验通过后会安全上传并创建 V1。</span>
      </div>
      <div className="fj-review-upload-actions">
        <input
          ref={fileInputRef}
          aria-label="原片文件（可多选）"
          className="fj-review-sr-only"
          data-testid="create-item-file"
          type="file"
          multiple
          accept={ACCEPTED_VIDEO_TYPES}
          aria-invalid={Boolean(batchError)}
          disabled={disabled}
          onChange={(event) => {
            const selected = Array.from(event.currentTarget.files ?? []);
            setRows(selected.map((file) => ({
              id: `upload-row-${nextRowId.current++}`,
              file,
              title: titleFromUploadFileName(file.name),
              episode: episodeFromUploadFileName(file.name),
              status: 'ready',
            })));
            setBatchCompleted(false);
            setBatchError(null);
          }}
        />
        <button
          className="fj-review-primary"
          disabled={disabled}
          onClick={() => fileInputRef.current?.click()}
          type="button"
        >
          选择文件
        </button>
        <button
          className="fj-review-primary"
          type="submit"
          disabled={disabled || !readyRows.length || missingRequired}
        >
          {props.blockedForListConfirmation ? '请先确认列表' : uploading || props.pending ? '上传中...' : '上传 V1'}
        </button>
      </div>
      {rows.length ? (
        <div className="fj-review-upload-rows" data-testid="create-item-upload-rows">
          {rows.map((row, index) => {
            const titleId = `${row.id}-title`;
            const episodeId = `${row.id}-episode`;
            return (
              <section className="fj-review-upload-row" data-testid={row.id} key={row.id}>
                <div className="fj-review-upload-row-file">
                  <strong>{row.file.name}</strong>
                  <span>{uploadRowStatus(row.status, index)}</span>
                </div>
                <label htmlFor={titleId}>
                  <span>成片标题</span>
                  <input
                    id={titleId}
                    value={row.title}
                    disabled={disabled}
                    onChange={(event) => updateRow(row.id, { title: event.target.value })}
                  />
                </label>
                <label htmlFor={episodeId}>
                  <span>集数</span>
                  <input
                    id={episodeId}
                    value={row.episode}
                    disabled={disabled}
                    onChange={(event) => updateRow(row.id, { episode: event.target.value })}
                  />
                </label>
                <UploadProgressView progress={row.progress} testId={`${row.id}-progress`} />
                {row.error ? <span className="fj-review-form-error" data-testid={`${row.id}-error`} role="alert">{row.error}</span> : null}
                {row.status === 'failed' || row.status === 'uncertain' ? (
                  <button
                    className="fj-review-secondary fj-review-upload-row-retry"
                    disabled={disabled || !row.title.trim() || !row.episode.trim()}
                    onClick={() => void runBatch([row])}
                    type="button"
                  >
                    {row.status === 'uncertain' ? '已核对未成功，重试此项' : '重试此项'}
                  </button>
                ) : null}
              </section>
            );
          })}
        </div>
      ) : null}
      {batchError ? <span className="fj-review-form-error" data-testid="create-item-batch-error" role="alert">{batchError}</span> : null}
      {batchCompleted && recoverableRows.length ? (
        <section className="fj-review-batch-report" data-testid="batch-upload-report" role="status" aria-live="polite">
          <strong>批次完成：以下素材需要恢复</strong>
          <ul>
            {recoverableRows.map((row) => (
              <li key={row.id}>
                {row.file.name}：{row.status === 'uncertain' ? '结果不确定/原因未确认' : '明确失败'}；失败阶段：
                {UPLOAD_STAGE_LABELS[row.failureStage ?? 'validating']}；原因：{row.error}
                {' '}恢复方式：{row.status === 'uncertain'
                  ? '先核对待审列表，确认未成功后点击“已核对未成功，重试此项”。'
                  : '修正原因后点击“重试此项”。'}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </form>
  );
}

function uploadRowStatus(status: CreateItemUploadRowStatus, index: number): string {
  switch (status) {
    case 'ready': return `第 ${index + 1} 条 · 待上传`;
    case 'queued': return '排队中 · 尚未创建上传会话';
    case 'uploading': return '上传中...';
    case 'failed': return '上传失败';
    case 'uncertain': return '结果不确定';
  }
}

export function AppendVersionPanel(props: {
  nextLabel: string;
  pending?: boolean;
  progress?: UploadProgress;
  onSubmit(input: { file: File; versionNote: string; changeSummary: string }): void | Promise<void>;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [versionNote, setVersionNote] = useState(`${props.nextLabel} 版本说明`);
  const [changeSummary, setChangeSummary] = useState('按审阅意见完成本轮修改。');
  const [fileError, setFileError] = useState<string | null>(null);
  const [submissionError, setSubmissionError] = useState<string | null>(null);
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
      {fileError ? <span className="fj-review-form-error fj-review-inline-upload-file-error" data-testid="append-version-file-error" id={APPEND_VERSION_FILE_ERROR_ID} role="alert">{fileError}</span> : null}
      {submissionError ? <span className="fj-review-form-error" id={APPEND_VERSION_SUBMISSION_ERROR_ID} role="alert">{submissionError}</span> : null}
      <UploadProgressView progress={props.progress} />
      <button className="fj-review-secondary fj-review-inline-upload-submit" disabled={props.pending} type="submit">
        {props.pending ? '上传中...' : `确认追加 ${props.nextLabel}`}
      </button>
    </form>
  );
}
