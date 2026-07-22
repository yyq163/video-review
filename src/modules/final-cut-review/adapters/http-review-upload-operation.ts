import type { UploadInitRequest, UploadSessionDTO } from '../contracts-generated/backend-contract';
import type { ExecutionContext, UploadProgress } from '../contracts/types';
import { ReviewDomainError } from '../core/errors';
import { randomId, type HttpReviewTransport } from './http-review-transport';

const SERVER_COMPUTED_SHA256 = '0'.repeat(64);
const MIN_UPLOAD_PART_BYTES = 8 * 1024 * 1024;
const MAX_UPLOAD_PART_BYTES = 64 * 1024 * 1024;
const MAX_UPLOAD_PARTS = 256;
const MAX_PENDING_UPLOADS = 32;
const FILE_IDENTITY_COMPARE_CHUNK_BYTES = 1024 * 1024;

interface PendingUploadOperation {
  signature: string;
  sourceFile: File;
  initIdempotencyKey: string;
  completeIdempotencyKey: string;
  session?: UploadSessionDTO;
  completed?: UploadSessionDTO;
  nextPartIndex: number;
  uploadedBytes: number;
  reportedBytes: number;
  completeAttempted: boolean;
  inFlight?: Promise<UploadSessionDTO>;
  inFlightFile?: File;
  lastProgress?: UploadProgress;
  progressSubscribers: Set<(progress: UploadProgress) => void>;
}

export class HttpReviewUploadOperation {
  private readonly pendingUploads = new Map<string, PendingUploadOperation>();

  constructor(private readonly transport: HttpReviewTransport) {}

  async upload(
    file: File,
    context: ExecutionContext,
    onProgress?: (progress: UploadProgress) => void,
  ): Promise<UploadSessionDTO> {
    // Reject known-invalid files before consuming a bounded pending-operation slot.
    uploadMimeType(file);
    const operation = this.operationFor(file);
    if (operation.inFlight && operation.inFlightFile !== file) {
      throw fileIdentityConflict();
    }
    const unsubscribe = this.subscribe(operation, onProgress);
    if (!operation.lastProgress) {
      this.emitProgress(operation, { stage: 'validating', percent: 0, totalBytes: file.size });
    }
    if (operation.completed && operation.sourceFile === file) {
      this.emitProgress(operation, {
        stage: 'uploading',
        percent: 90,
        bytesSent: file.size,
        totalBytes: file.size,
      });
      unsubscribe();
      return operation.completed;
    }

    const inFlight = operation.inFlight ?? this.resume(operation, file, context);
    operation.inFlight = inFlight;
    operation.inFlightFile = file;
    try {
      return await inFlight;
    } finally {
      unsubscribe();
      if (operation.inFlight === inFlight) {
        operation.inFlight = undefined;
        operation.inFlightFile = undefined;
      }
    }
  }

  releaseCompleted(file: File, uploadId: string): boolean {
    const signature = fileSignature(file);
    const operation = this.pendingUploads.get(signature);
    if (
      !operation
      || operation.sourceFile !== file
      || operation.inFlight
      || operation.completed?.upload_id !== uploadId
    ) {
      return false;
    }
    operation.progressSubscribers.clear();
    return this.pendingUploads.delete(signature);
  }

  private operationFor(file: File): PendingUploadOperation {
    const signature = fileSignature(file);
    const current = this.pendingUploads.get(signature);
    if (current) {
      this.pendingUploads.delete(signature);
      this.pendingUploads.set(signature, current);
      return current;
    }
    const created: PendingUploadOperation = {
      signature,
      sourceFile: file,
      initIdempotencyKey: randomId('InitUpload'),
      completeIdempotencyKey: randomId('CompleteUpload'),
      nextPartIndex: 0,
      uploadedBytes: 0,
      reportedBytes: 0,
      completeAttempted: false,
      progressSubscribers: new Set(),
    };
    if (this.pendingUploads.size >= MAX_PENDING_UPLOADS) {
      throw new ReviewDomainError(
        '浏览器中待确认上传过多，请等待当前上传完成后重试',
        'CLIENT_UPLOAD_QUEUE_FULL',
      );
    }
    this.pendingUploads.set(signature, created);
    return created;
  }

  private async resume(
    operation: PendingUploadOperation,
    file: File,
    context: ExecutionContext,
  ): Promise<UploadSessionDTO> {
    if (operation.sourceFile !== file) {
      await assertPersistedPrefixMatches(operation.sourceFile, file, operation.uploadedBytes);
      // Do not replace the trusted source until the entire persisted prefix
      // has been verified. A failed comparison leaves every operation field
      // used for resume/complete unchanged.
      operation.sourceFile = file;
    }
    if (operation.completed) {
      this.emitProgress(operation, {
        stage: 'uploading',
        percent: 90,
        bytesSent: file.size,
        totalBytes: file.size,
      });
      return operation.completed;
    }
    return this.run(operation, file, context);
  }

  private async run(
    operation: PendingUploadOperation,
    file: File,
    context: ExecutionContext,
  ): Promise<UploadSessionDTO> {
    if (!operation.session) {
      operation.session = await this.transport.requestJson<UploadSessionDTO>(
        '/api/v1/files/uploads/init',
        this.transport.jsonInit(
          'POST',
          {
            original_filename: file.name,
            mime_type: uploadMimeType(file),
            file_size: file.size,
            sha256: SERVER_COMPUTED_SHA256,
          },
          context,
          { 'Idempotency-Key': operation.initIdempotencyKey },
        ),
      );
    }
    this.emitProgress(operation, {
      stage: 'initiated',
      percent: uploadPercent(operation.reportedBytes, file.size),
      bytesSent: operation.reportedBytes,
      totalBytes: file.size,
    });

    if (!operation.completeAttempted) {
      const parts = uploadParts(file);
      for (let index = operation.nextPartIndex; index < parts.length; index += 1) {
        const part = parts[index];
        const partSession = await this.transport.requestBinaryJson<unknown>(
          `/api/v1/files/uploads/${operation.session.upload_id}/parts/${part.partNo}`,
          part.blob,
          context,
          (loaded) => {
            operation.reportedBytes = Math.max(
              operation.reportedBytes,
              Math.min(operation.uploadedBytes + loaded, file.size),
            );
            this.emitProgress(operation, {
              stage: 'uploading',
              percent: uploadPercent(operation.reportedBytes, file.size),
              bytesSent: operation.reportedBytes,
              totalBytes: file.size,
            });
          },
        );
        operation.session = parseUploadPartSession(partSession, operation.session.upload_id);
        operation.nextPartIndex = index + 1;
        operation.uploadedBytes += part.blob.size;
        operation.reportedBytes = Math.max(operation.reportedBytes, operation.uploadedBytes);
        this.emitProgress(operation, {
          stage: 'uploading',
          percent: uploadPercent(operation.reportedBytes, file.size),
          bytesSent: Math.min(operation.reportedBytes, file.size),
          totalBytes: file.size,
        });
      }
    } else {
      this.emitProgress(operation, {
        stage: 'uploading',
        percent: 85,
        bytesSent: file.size,
        totalBytes: file.size,
      });
    }

    operation.completeAttempted = true;
    const completed = await this.transport.requestJson<UploadSessionDTO>(
      `/api/v1/files/uploads/${operation.session.upload_id}/complete`,
      {
        method: 'POST',
        headers: {
          ...this.transport.contextHeaders(context),
          'Idempotency-Key': operation.completeIdempotencyKey,
        },
      },
    );
    operation.session = completed;
    operation.completed = completed;
    this.emitProgress(operation, {
      stage: 'uploading',
      percent: 90,
      bytesSent: file.size,
      totalBytes: file.size,
    });
    return completed;
  }

  private subscribe(
    operation: PendingUploadOperation,
    subscriber?: (progress: UploadProgress) => void,
  ): () => void {
    if (!subscriber) return () => undefined;
    const subscription = (progress: UploadProgress) => subscriber(progress);
    operation.progressSubscribers.add(subscription);
    if (operation.lastProgress) subscription(operation.lastProgress);
    return () => operation.progressSubscribers.delete(subscription);
  }

  private emitProgress(operation: PendingUploadOperation, progress: UploadProgress): void {
    const previous = operation.lastProgress;
    const next: UploadProgress = {
      ...progress,
      stage: previous && progressStageRank(previous.stage) > progressStageRank(progress.stage)
        ? previous.stage
        : progress.stage,
      percent: Math.max(previous?.percent ?? 0, progress.percent),
      bytesSent: progress.bytesSent === undefined
        ? previous?.bytesSent
        : Math.max(previous?.bytesSent ?? 0, progress.bytesSent),
      totalBytes: progress.totalBytes ?? previous?.totalBytes,
    };
    operation.lastProgress = next;
    for (const subscriber of operation.progressSubscribers) subscriber(next);
  }
}

function parseUploadPartSession(value: unknown, expectedUploadId: string): UploadSessionDTO {
  if (!isRecord(value)) {
    throw invalidUploadPartResponse();
  }
  if (value.upload_id !== expectedUploadId) {
    throw new ReviewDomainError('上传分片响应会话不匹配，请重试', 'UPLOAD_RESPONSE_ID_MISMATCH');
  }
  if (
    value.status !== 'receiving'
    || typeof value.original_filename !== 'string'
    || value.original_filename.length === 0
    || typeof value.mime_type !== 'string'
    || value.mime_type.length === 0
    || !isNonNegativeSafeInteger(value.declared_size)
    || !isNonNegativeSafeInteger(value.received_size)
    || value.received_size > value.declared_size
    || (value.file_id !== undefined && value.file_id !== null)
  ) {
    throw invalidUploadPartResponse();
  }
  return {
    upload_id: value.upload_id,
    status: value.status,
    original_filename: value.original_filename,
    mime_type: value.mime_type,
    declared_size: value.declared_size,
    received_size: value.received_size,
    file_id: value.file_id,
  };
}

function invalidUploadPartResponse(): ReviewDomainError {
  return new ReviewDomainError('上传分片响应数据无效，请重试', 'UPLOAD_RESPONSE_INVALID');
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isNonNegativeSafeInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
}

function progressStageRank(stage: UploadProgress['stage']): number {
  switch (stage) {
    case 'validating': return 0;
    case 'initiated': return 1;
    case 'uploading': return 2;
    case 'binding': return 3;
    case 'completed': return 4;
  }
}

function uploadMimeType(file: File): UploadInitRequest['mime_type'] {
  const dotIndex = file.name.lastIndexOf('.');
  const extension = dotIndex >= 0 ? file.name.slice(dotIndex).toLowerCase() : '';
  const declared = file.type.trim().toLowerCase().split(';', 1)[0].trim();
  const isMp4 = extension === '.mp4' || extension === '.m4v';
  const isQuickTime = extension === '.mov' || extension === '.qt';
  if (!isMp4 && !isQuickTime) {
    throw new ReviewDomainError('仅支持 MP4、M4V、MOV 或 QT 原片', 'FILE_TYPE_NOT_ALLOWED');
  }
  if (!declared || declared === 'application/octet-stream') {
    return isMp4 ? 'video/mp4' : 'video/quicktime';
  }
  if (isMp4) {
    if (declared === 'video/mp4' || declared === 'video/x-m4v' || declared === 'application/mp4') {
      return 'video/mp4';
    }
  }
  if (isQuickTime) {
    if (
      declared === 'video/quicktime'
      || declared === 'video/x-quicktime'
      || declared === 'application/quicktime'
    ) {
      return 'video/quicktime';
    }
  }
  throw new ReviewDomainError('仅支持 MP4、M4V、MOV 或 QT 原片', 'FILE_TYPE_NOT_ALLOWED');
}

function fileSignature(file: File): string {
  return JSON.stringify([file.name, file.size, file.type, file.lastModified]);
}

async function assertPersistedPrefixMatches(
  trustedFile: File,
  candidateFile: File,
  uploadedBytes: number,
): Promise<void> {
  if (uploadedBytes === 0) return;
  if (
    !Number.isSafeInteger(uploadedBytes)
    || uploadedBytes < 0
    || uploadedBytes > trustedFile.size
    || uploadedBytes > candidateFile.size
  ) {
    throw fileIdentityConflict();
  }

  try {
    for (let offset = 0; offset < uploadedBytes; offset += FILE_IDENTITY_COMPARE_CHUNK_BYTES) {
      const end = Math.min(offset + FILE_IDENTITY_COMPARE_CHUNK_BYTES, uploadedBytes);
      const [trustedChunk, candidateChunk] = await Promise.all([
        readBlobBytes(trustedFile.slice(offset, end)),
        readBlobBytes(candidateFile.slice(offset, end)),
      ]);
      if (!equalBytes(trustedChunk, candidateChunk)) {
        throw fileIdentityConflict();
      }
    }
  } catch (error) {
    if (error instanceof ReviewDomainError) throw error;
    throw fileIdentityConflict();
  }
}

function readBlobBytes(blob: Blob): Promise<Uint8Array> {
  return new Promise<Uint8Array>((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener('load', () => {
      if (reader.result instanceof ArrayBuffer) {
        resolve(new Uint8Array(reader.result));
      } else {
        reject(new Error('Unable to read upload identity bytes'));
      }
    });
    reader.addEventListener('error', () => reject(reader.error ?? new Error('Unable to read upload identity bytes')));
    reader.addEventListener('abort', () => reject(new DOMException('Upload identity read aborted', 'AbortError')));
    reader.readAsArrayBuffer(blob);
  });
}

function equalBytes(left: Uint8Array, right: Uint8Array): boolean {
  if (left.byteLength !== right.byteLength) return false;
  for (let index = 0; index < left.byteLength; index += 1) {
    if (left[index] !== right[index]) return false;
  }
  return true;
}

function fileIdentityConflict(): ReviewDomainError {
  return new ReviewDomainError(
    '检测到同名同大小文件内容与已上传分片不一致，请重新上传',
    'CLIENT_UPLOAD_FILE_IDENTITY_CONFLICT',
  );
}

function uploadParts(file: File): Array<{ partNo: number; blob: Blob }> {
  const partSize = Math.max(MIN_UPLOAD_PART_BYTES, Math.ceil(file.size / MAX_UPLOAD_PARTS));
  if (partSize > MAX_UPLOAD_PART_BYTES) {
    throw new ReviewDomainError('文件过大，无法在 256 个分片内上传', 'FILE_TOO_LARGE');
  }
  const parts: Array<{ partNo: number; blob: Blob }> = [];
  for (let offset = 0, partNo = 1; offset < file.size; offset += partSize, partNo += 1) {
    parts.push({ partNo, blob: file.slice(offset, Math.min(offset + partSize, file.size)) });
  }
  return parts;
}

function uploadPercent(uploadedBytes: number, totalBytes: number): number {
  if (totalBytes <= 0) return 85;
  return Math.min(85, Math.max(5, (uploadedBytes / totalBytes) * 80 + 5));
}
