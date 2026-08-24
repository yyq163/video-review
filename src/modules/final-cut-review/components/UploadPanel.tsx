import { useRef, useState } from 'react';
import { UploadCloud, X } from 'lucide-react';
import type { ResponsibleEditor, ReviewFrameRate, UploadProgress } from '../contracts/types';
import type { MediaMetadata, MediaMetadataPort } from '../ports';
import { formatEpisodeDisplayValue, normalizeEpisodeValue } from '../core/episode-dedupe';

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
  responsibleEditorId?: string;
  responsibleEditorNameSnapshot?: string;
  localProbe?: MediaMetadata;
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
  responsibleEditorId: string;
  editorDirty: boolean;
  mediaState: 'ready' | 'probing' | 'invalid';
  mediaError?: string;
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

export function CreateItemUploadPanel({
  pending,
  blockedForListConfirmation,
  setBulkActionsHost,
  existingEpisodes,
  mediaMetadata,
  onSubmit,
  onEditResponsibleEditor,
  projectFrameRate,
  responsibleEditors,
}: {
  pending?: boolean;
  blockedForListConfirmation?: boolean;
  setBulkActionsHost?: (node: HTMLSpanElement | null) => void;
  existingEpisodes?: string[];
  mediaMetadata?: MediaMetadataPort;
  onEditResponsibleEditor?(): void;
  projectFrameRate?: ReviewFrameRate | null;
  responsibleEditors?: ResponsibleEditor[];
  onSubmit(
    input: CreateItemUploadInput,
    onProgress?: (progress: UploadProgress) => void,
  ): CreateItemUploadOutcome | Promise<CreateItemUploadOutcome>;
}) {
  const nextRowId = useRef(0);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const removedRowIdsRef = useRef(new Set<string>());
  const activeBatchEpisodeKeysRef = useRef(new Set<string>());
  const mediaProbeAbortRef = useRef<AbortController | null>(null);
  const [bulkEditorId, setBulkEditorId] = useState(responsibleEditors?.[0]?.responsibleEditorId ?? '');
  const [rows, setRows] = useState<CreateItemUploadRow[]>([]);
  const [uploading, setUploading] = useState(false);
  const [completedEpisodeKeys, setCompletedEpisodeKeys] = useState<Set<string>>(() => new Set());
  const [batchCompleted, setBatchCompleted] = useState(false);
  const [batchError, setBatchError] = useState<string | null>(null);
  const editorSelectionRequired = responsibleEditors !== undefined;
  const editorCatalogUnavailable = editorSelectionRequired && responsibleEditors.length === 0;
  const disabled = Boolean(pending || uploading || blockedForListConfirmation || editorCatalogUnavailable);
  const effectiveBulkEditorId = responsibleEditors?.some(
    (editor) => editor.responsibleEditorId === bulkEditorId,
  )
    ? bulkEditorId
    : responsibleEditors?.[0]?.responsibleEditorId ?? '';
  const blockedEpisodeKeys = new Set([
    ...(existingEpisodes ?? []).map(normalizeEpisodeValue).filter(Boolean),
    ...completedEpisodeKeys,
  ]);
  const conflictsWithExisting = (row: CreateItemUploadRow) => {
    const episodeKey = normalizeEpisodeValue(row.episode);
    if (!episodeKey) return false;
    if (blockedEpisodeKeys.has(episodeKey)) return true;
    const rowIndex = rows.findIndex((candidate) => candidate.id === row.id);
    return rowIndex > 0 && rows
      .slice(0, rowIndex)
      .some((candidate) => normalizeEpisodeValue(candidate.episode) === episodeKey);
  };
  const missingRequired = rows.some((row) =>
    !row.title.trim()
    || !row.episode.trim()
    || (editorSelectionRequired && !row.responsibleEditorId),
  );
  const mediaProbePending = rows.some((row) => row.mediaState === 'probing');
  const readyRows = rows.filter(
    (row) => row.status === 'ready' && row.mediaState === 'ready' && !conflictsWithExisting(row),
  );
  const recoverableRows = rows.filter((row) => row.status === 'failed' || row.status === 'uncertain');
  const uploadDisabled = disabled || !readyRows.length || missingRequired || mediaProbePending;
  const uploadDisabledReason = uploadDisabled
    ? blockedForListConfirmation
      ? '上一笔 V1 结果不确定，请先核对待审列表'
      : uploading || pending
        ? '当前批次正在上传，请等待上传完成'
        : !rows.length
          ? '请先选择原片文件'
          : missingRequired
            ? editorSelectionRequired
              ? '请补齐每一条成片的标题、责任剪辑师和集数'
              : '请补齐每一条成片的标题和集数'
            : mediaProbePending
              ? '正在预检所选媒体'
            : rows.some((row) => conflictsWithExisting(row))
              ? '集数与已上传成片或当前队列重复，请修改集数'
              : recoverableRows.length
                ? '当前没有可上传条目，请先处理失败或结果不确定项'
                : '当前没有可上传的条目'
    : null;
  const updateRow = (id: string, patch: Partial<Pick<CreateItemUploadRow, 'title' | 'episode' | 'responsibleEditorId' | 'editorDirty'>>) => {
    setRows((current) => current.map((row) => row.id === id ? { ...row, ...patch } : row));
    setBatchError(null);
  };
  const removeRow = (row: CreateItemUploadRow) => {
    if (row.status === 'uploading') return;
    removedRowIdsRef.current.add(row.id);
    setRows((current) => current.filter((candidate) => candidate.id !== row.id));
    setBatchError(null);
  };
  const runBatch = async (batch: CreateItemUploadRow[]) => {
    const eligibleBatch = batch.filter(
      (row) => !removedRowIdsRef.current.has(row.id) && row.mediaState === 'ready' && !conflictsWithExisting(row),
    );
    if (!eligibleBatch.length || uploading) return;
    activeBatchEpisodeKeysRef.current = new Set(blockedEpisodeKeys);
    setUploading(true);
    setBatchCompleted(false);
    setBatchError(null);
    if (fileInputRef.current) fileInputRef.current.value = '';
    const batchIds = new Set(eligibleBatch.map((row) => row.id));
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
        const row = eligibleBatch[nextIndex];
        nextIndex += 1;
        if (!row) return;
        if (removedRowIdsRef.current.has(row.id)) continue;
        const episodeKey = normalizeEpisodeValue(row.episode);
        if (episodeKey && activeBatchEpisodeKeysRef.current.has(episodeKey)) {
          setRows((current) => current.map((candidate) =>
            candidate.id === row.id ? { ...candidate, status: 'ready' } : candidate,
          ));
          continue;
        }
        setRows((current) => current.map((candidate) =>
          candidate.id === row.id
            ? { ...candidate, status: 'uploading', error: undefined, progress: undefined }
            : candidate,
        ));
        let result: CreateItemUploadOutcome;
        let failureStage: UploadProgress['stage'] = 'validating';
        try {
          const selectedEditor = responsibleEditors?.find(
            (editor) => editor.responsibleEditorId === row.responsibleEditorId,
          );
          result = await onSubmit(
            {
              title: row.title,
              episode: row.episode,
              file: row.file,
              ...(selectedEditor && row.localProbe
                ? {
                    responsibleEditorId: selectedEditor.responsibleEditorId,
                    responsibleEditorNameSnapshot: selectedEditor.displayName,
                    localProbe: row.localProbe,
                  }
                : {}),
            },
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
          if (episodeKey) {
            activeBatchEpisodeKeysRef.current = new Set([
              ...activeBatchEpisodeKeysRef.current,
              episodeKey,
            ]);
            setCompletedEpisodeKeys((current) => new Set([...current, episodeKey]));
          }
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
        Array.from(
          { length: Math.min(MAX_CONCURRENT_V1_UPLOADS, eligibleBatch.length) },
          () => runWorker(),
        ),
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
      {responsibleEditors !== undefined ? (
        <div className="fj-review-upload-editor-bar">
          <label>
            <span>批量责任剪辑师</span>
            <select
              aria-label="批量责任剪辑师"
              disabled={disabled || responsibleEditors.length === 0}
              value={effectiveBulkEditorId}
              onChange={(event) => {
                const responsibleEditorId = event.target.value;
                setBulkEditorId(responsibleEditorId);
                setRows((current) => current.map((row) =>
                  row.editorDirty ? row : { ...row, responsibleEditorId },
                ));
              }}
            >
              {responsibleEditors.length === 0 ? <option value="">请先编辑责任剪辑师</option> : null}
              {responsibleEditors.map((editor) => (
                <option key={editor.responsibleEditorId} value={editor.responsibleEditorId}>{editor.displayName}</option>
              ))}
            </select>
          </label>
          {onEditResponsibleEditor ? (
            <button className="fj-review-secondary" onClick={onEditResponsibleEditor} type="button">
              编辑责任剪辑师
            </button>
          ) : null}
        </div>
      ) : null}
      <div className="fj-review-upload-actions">
        <span
          className="fj-review-upload-bulk-actions-host"
          ref={setBulkActionsHost}
        />
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
            mediaProbeAbortRef.current?.abort();
            const abortController = new AbortController();
            mediaProbeAbortRef.current = abortController;
            const nextRows: CreateItemUploadRow[] = selected.map((file) => ({
              id: `upload-row-${nextRowId.current++}`,
              file,
              title: titleFromUploadFileName(file.name),
              episode: episodeFromUploadFileName(file.name),
              status: 'ready' as const,
              responsibleEditorId: effectiveBulkEditorId,
              editorDirty: false,
              mediaState: mediaMetadata ? 'probing' as const : 'ready' as const,
            }));
            setRows(nextRows);
            setBatchCompleted(false);
            setBatchError(null);
            if (mediaMetadata) {
              for (const row of nextRows) {
                void mediaMetadata.probe(row.file, {
                  signal: abortController.signal,
                  expectedFrameRate: projectFrameRate ?? undefined,
                }).then((localProbe) => {
                  setRows((current) => current.map((candidate) =>
                    candidate.id === row.id
                      ? { ...candidate, localProbe, mediaState: 'ready', mediaError: undefined }
                      : candidate,
                  ));
                }).catch((error: unknown) => {
                  if (error instanceof DOMException && error.name === 'AbortError') return;
                  setRows((current) => current.map((candidate) =>
                    candidate.id === row.id
                      ? {
                          ...candidate,
                          mediaState: 'invalid',
                          mediaError: error instanceof Error ? error.message : '媒体预检失败',
                        }
                      : candidate,
                  ));
                });
              }
            }
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
        <span
          aria-label={uploadDisabledReason ?? undefined}
          className="fj-review-disabled-action-tooltip"
          data-tooltip={uploadDisabledReason ?? undefined}
          tabIndex={uploadDisabledReason ? 0 : undefined}
          title={uploadDisabledReason ?? undefined}
        >
          <button
            className="fj-review-primary"
            type="submit"
            disabled={uploadDisabled}
          >
            {blockedForListConfirmation ? '请先确认列表' : uploading || pending ? '上传中...' : '上传 V1'}
          </button>
        </span>
      </div>
      {rows.length ? (
        <div className="fj-review-upload-rows" data-testid="create-item-upload-rows" id="create-item-upload-queue">
          {rows.map((row, index) => {
            const titleId = `${row.id}-title`;
            const episodeId = `${row.id}-episode`;
            const episodeConflict = conflictsWithExisting(row);
            const conflictErrorId = `${row.id}-episode-conflict`;
            return (
              <section
                className={`fj-review-upload-row${episodeConflict ? ' is-conflict' : ''}${row.mediaState === 'invalid' ? ' is-media-invalid' : ''}`}
                data-testid={row.id}
                key={row.id}
              >
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
                {responsibleEditors !== undefined ? (
                  <label>
                    <span>责任剪辑师</span>
                    <select
                      aria-label="责任剪辑师"
                      disabled={disabled || responsibleEditors.length === 0}
                      value={row.responsibleEditorId}
                      onChange={(event) => updateRow(row.id, {
                        responsibleEditorId: event.target.value,
                        editorDirty: true,
                      })}
                    >
                      {responsibleEditors.length === 0 ? <option value="">未设置</option> : null}
                      {responsibleEditors.map((editor) => (
                        <option key={editor.responsibleEditorId} value={editor.responsibleEditorId}>{editor.displayName}</option>
                      ))}
                    </select>
                  </label>
                ) : null}
                <label htmlFor={episodeId}>
                  <span>集数</span>
                  <input
                    id={episodeId}
                    value={row.episode}
                    aria-describedby={episodeConflict ? conflictErrorId : undefined}
                    aria-invalid={episodeConflict}
                    disabled={disabled}
                    onChange={(event) => updateRow(row.id, { episode: event.target.value })}
                  />
                </label>
                {row.status !== 'uploading' ? (
                  <button
                    aria-label={`移除待上传条目 ${row.file.name}`}
                    className="fj-review-upload-row-remove"
                    onClick={() => removeRow(row)}
                    title="从上传队列移除"
                    type="button"
                  >
                    <X aria-hidden="true" />
                  </button>
                ) : (
                  <span
                    aria-hidden="true"
                    className="fj-review-upload-row-remove-placeholder"
                  />
                )}
                <UploadProgressView progress={row.progress} testId={`${row.id}-progress`} />
                {row.mediaState === 'probing' ? <span className="fj-review-media-preflight" role="status">媒体预检中</span> : null}
                {row.mediaError ? <span className="fj-review-form-error fj-review-media-preflight-error" role="alert">{row.mediaError}</span> : null}
                {episodeConflict ? (
                  <span
                    className="fj-review-form-error"
                    data-testid={`${row.id}-episode-conflict`}
                    id={conflictErrorId}
                    role="alert"
                  >
                    第{formatEpisodeDisplayValue(row.episode)}集已上传，请修改上传信息
                  </span>
                ) : null}
                {row.error ? <span className="fj-review-form-error" data-testid={`${row.id}-error`} role="alert">{row.error}</span> : null}
                {row.status === 'failed' || row.status === 'uncertain' ? (
                  <button
                    className="fj-review-secondary fj-review-upload-row-retry"
                    disabled={disabled || episodeConflict || !row.title.trim() || !row.episode.trim()}
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
  mediaMetadata?: MediaMetadataPort;
  projectFrameRate?: ReviewFrameRate | null;
  responsibleEditors?: ResponsibleEditor[];
  onSubmit(input: {
    file: File;
    versionNote: string;
    changeSummary: string;
    responsibleEditorId?: string;
    responsibleEditorNameSnapshot?: string;
    localProbe?: MediaMetadata;
  }): void | Promise<void>;
}) {
  const mediaProbeSequenceRef = useRef(0);
  const [file, setFile] = useState<File | null>(null);
  const [responsibleEditorId, setResponsibleEditorId] = useState(
    props.responsibleEditors?.[0]?.responsibleEditorId ?? '',
  );
  const [localProbe, setLocalProbe] = useState<MediaMetadata | null>(null);
  const [mediaProbePending, setMediaProbePending] = useState(false);
  const [mediaProbeError, setMediaProbeError] = useState<string | null>(null);
  const [versionNote, setVersionNote] = useState(`${props.nextLabel} 版本说明`);
  const [changeSummary, setChangeSummary] = useState('按审阅意见完成本轮修改。');
  const [fileError, setFileError] = useState<string | null>(null);
  const [submissionError, setSubmissionError] = useState<string | null>(null);
  const fileErrorIds = [
    fileError ? APPEND_VERSION_FILE_ERROR_ID : null,
    submissionError ? APPEND_VERSION_SUBMISSION_ERROR_ID : null,
  ].filter(Boolean).join(' ') || undefined;
  const effectiveResponsibleEditorId = props.responsibleEditors?.some(
    (editor) => editor.responsibleEditorId === responsibleEditorId,
  )
    ? responsibleEditorId
    : props.responsibleEditors?.[0]?.responsibleEditorId ?? '';

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
        if (props.responsibleEditors !== undefined && !effectiveResponsibleEditorId) {
          setSubmissionError('责任剪辑师必填。');
          return;
        }
        if (props.mediaMetadata && (!localProbe || mediaProbePending || mediaProbeError)) {
          setSubmissionError(mediaProbeError ?? '媒体预检尚未完成。');
          return;
        }
        if (!versionNote.trim() || !changeSummary.trim()) {
          setSubmissionError('版本说明和本次修改说明必填。');
          return;
        }
        setFileError(null);
        setSubmissionError(null);
        const editor = props.responsibleEditors?.find(
          (candidate) => candidate.responsibleEditorId === effectiveResponsibleEditorId,
        );
        void Promise.resolve(props.onSubmit({
          file,
          versionNote: versionNote.trim(),
          changeSummary: changeSummary.trim(),
          ...(editor && localProbe
            ? {
                responsibleEditorId: editor.responsibleEditorId,
                responsibleEditorNameSnapshot: editor.displayName,
                localProbe,
              }
            : {}),
        })).catch((error: unknown) => {
          setSubmissionError(error instanceof Error ? error.message : '上传失败，请重试。');
        });
      }}
    >
      <div className="fj-review-inline-upload-summary">
        <strong>追加 {props.nextLabel}</strong>
        <span>填写版本元数据后上传新版本。</span>
      </div>
      {props.responsibleEditors !== undefined ? (
        <label className="fj-review-inline-upload-editor">
          <span>责任剪辑师</span>
          <select
            aria-label="责任剪辑师"
            disabled={props.pending || props.responsibleEditors.length === 0}
            value={effectiveResponsibleEditorId}
            onChange={(event) => setResponsibleEditorId(event.target.value)}
          >
            {props.responsibleEditors.length === 0 ? <option value="">未设置</option> : null}
            {props.responsibleEditors.map((editor) => (
              <option key={editor.responsibleEditorId} value={editor.responsibleEditorId}>{editor.displayName}</option>
            ))}
          </select>
        </label>
      ) : null}
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
            const nextFile = event.currentTarget.files?.[0] ?? null;
            const probeSequence = ++mediaProbeSequenceRef.current;
            setFile(nextFile);
            setLocalProbe(null);
            setMediaProbeError(null);
            setFileError(null);
            setSubmissionError(null);
            if (nextFile && props.mediaMetadata) {
              setMediaProbePending(true);
              void props.mediaMetadata.probe(nextFile, {
                expectedFrameRate: props.projectFrameRate ?? undefined,
              }).then((metadata) => {
                if (mediaProbeSequenceRef.current !== probeSequence) return;
                setLocalProbe(metadata);
                setMediaProbeError(null);
              }).catch((error: unknown) => {
                if (mediaProbeSequenceRef.current !== probeSequence) return;
                setMediaProbeError(error instanceof Error ? error.message : '媒体预检失败');
              }).finally(() => {
                if (mediaProbeSequenceRef.current === probeSequence) setMediaProbePending(false);
              });
            }
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
      {mediaProbePending ? <span className="fj-review-media-preflight" role="status">媒体预检中</span> : null}
      {mediaProbeError ? <span className="fj-review-form-error" role="alert">{mediaProbeError}</span> : null}
      {submissionError ? <span className="fj-review-form-error" id={APPEND_VERSION_SUBMISSION_ERROR_ID} role="alert">{submissionError}</span> : null}
      <UploadProgressView progress={props.progress} />
      <button className="fj-review-secondary fj-review-inline-upload-submit" disabled={props.pending || mediaProbePending} type="submit">
        {props.pending ? '上传中...' : `确认追加 ${props.nextLabel}`}
      </button>
    </form>
  );
}
