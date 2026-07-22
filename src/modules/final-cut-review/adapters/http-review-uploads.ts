import {
  FinalCutReviewHttpError,
  type ReviewItemDTO,
  type ReviewVersionDTO,
  type UploadSessionDTO,
} from '../contracts-generated/backend-contract';
import type {
  ExecutionContext,
  UploadProgress,
} from '../contracts/types';
import { ReviewDomainError } from '../core/errors';
import type { ReviewApiPort } from '../ports';
import { itemFromDto, versionFromDto } from './http-review-project-mappers';
import { HttpReviewUploadOperation } from './http-review-upload-operation';
import { randomId, type HttpReviewTransport } from './http-review-transport';

const V1_LIST_CONFIRMATION_KEY_PREFIX = 'fj-final-cut-review:v1-list-confirmation:';
const APPEND_VERSION_CONFIRMATION_KEY_PREFIX = 'fj-final-cut-review:append-version-confirmation:';
const STORAGE_PROBE_KEY = 'fj-final-cut-review:confirmation-storage-probe';

export type V1ListProtectionState = 'clear' | 'required' | 'storage-unavailable';
export type AppendVersionProtectionState = V1ListProtectionState;

function v1ListConfirmationKey(projectRefId: string): string {
  return `${V1_LIST_CONFIRMATION_KEY_PREFIX}${encodeURIComponent(projectRefId)}`;
}

function appendVersionConfirmationKey(projectRefId: string, reviewItemId: string): string {
  return `${APPEND_VERSION_CONFIRMATION_KEY_PREFIX}${encodeURIComponent(projectRefId)}:${encodeURIComponent(reviewItemId)}`;
}

function sessionStorageOrNull(): Storage | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

function markListConfirmationRequired(key: string): void {
  try {
    sessionStorageOrNull()?.setItem(key, 'required');
  } catch {
    // Storage can be unavailable in privacy-restricted browser contexts.
  }
}

function getListProtectionState(key: string): V1ListProtectionState {
  const storage = sessionStorageOrNull();
  if (!storage) return 'storage-unavailable';
  try {
    const required = storage.getItem(key) === 'required';
    storage.setItem(STORAGE_PROBE_KEY, '1');
    storage.removeItem(STORAGE_PROBE_KEY);
    return required ? 'required' : 'clear';
  } catch {
    return 'storage-unavailable';
  }
}

function clearListConfirmationRequired(key: string): void {
  try {
    sessionStorageOrNull()?.removeItem(key);
  } catch {
    // A failed cleanup must not replace a completed user operation with an error.
  }
}

function markV1ListConfirmationRequired(projectRefId: string): void {
  markListConfirmationRequired(v1ListConfirmationKey(projectRefId));
}

export function getV1ListProtectionState(projectRefId: string): V1ListProtectionState {
  return getListProtectionState(v1ListConfirmationKey(projectRefId));
}

export function isV1ListConfirmationRequired(projectRefId: string): boolean {
  return getV1ListProtectionState(projectRefId) !== 'clear';
}

export function clearV1ListConfirmationRequired(projectRefId: string): void {
  clearListConfirmationRequired(v1ListConfirmationKey(projectRefId));
}

function markAppendVersionConfirmationRequired(projectRefId: string, reviewItemId: string): void {
  markListConfirmationRequired(appendVersionConfirmationKey(projectRefId, reviewItemId));
}

export function getAppendVersionProtectionState(
  projectRefId: string,
  reviewItemId: string,
): AppendVersionProtectionState {
  return getListProtectionState(appendVersionConfirmationKey(projectRefId, reviewItemId));
}

export function isAppendVersionConfirmationRequired(projectRefId: string, reviewItemId: string): boolean {
  return getAppendVersionProtectionState(projectRefId, reviewItemId) !== 'clear';
}

export function clearAppendVersionConfirmationRequired(projectRefId: string, reviewItemId: string): void {
  clearListConfirmationRequired(appendVersionConfirmationKey(projectRefId, reviewItemId));
}

interface PendingCreateReviewItemOperation {
  signature: string;
  commandId: string;
  upload?: UploadSessionDTO;
  item?: ReviewItemDTO;
  version?: ReviewVersionDTO;
}

interface PendingAppendVersionOperation {
  signature: string;
  commandId: string;
  item?: ReviewItemDTO;
  upload?: UploadSessionDTO;
  version?: ReviewVersionDTO;
}

type UploadApi = Pick<
  ReviewApiPort,
  'createReviewItemWithVersion' | 'deleteReviewItem' | 'appendVersion'
>;

export class HttpReviewUploads implements UploadApi {
  private readonly pendingCreateReviewItems = new WeakMap<File, PendingCreateReviewItemOperation>();
  private readonly pendingAppendVersions = new WeakMap<File, PendingAppendVersionOperation>();
  private readonly uploadOperation: HttpReviewUploadOperation;
  private readonly upload: (
    file: File,
    context: ExecutionContext,
    onProgress?: (progress: UploadProgress) => void,
  ) => Promise<UploadSessionDTO>;

  constructor(
    private readonly transport: HttpReviewTransport,
    upload?: (
      file: File,
      context: ExecutionContext,
      onProgress?: (progress: UploadProgress) => void,
    ) => Promise<UploadSessionDTO>,
  ) {
    this.uploadOperation = new HttpReviewUploadOperation(transport);
    this.upload = upload ?? ((file, context, onProgress) => this.uploadFile(file, context, onProgress));
  }

  readonly createReviewItemWithVersion: ReviewApiPort['createReviewItemWithVersion'] = async (input, context) => {
    this.transport.assertWriteContext(context, ['edit']);
    await this.transport.projectForWrite(input.projectRefId);
    const signature = JSON.stringify([
      input.projectRefId,
      input.title,
      input.episode,
      input.file.name,
      input.file.size,
      input.file.type,
      input.file.lastModified,
    ]);
    let operation = this.pendingCreateReviewItems.get(input.file);
    if (!operation || operation.signature !== signature) {
      operation = { signature, commandId: randomId('CreateReviewItem') };
      this.pendingCreateReviewItems.set(input.file, operation);
    }
    const upload = operation.upload ?? (await this.upload(input.file, context, input.onProgress));
    operation.upload = upload;
    input.onProgress?.({
      stage: 'binding',
      percent: 95,
      bytesSent: input.file.size,
      totalBytes: input.file.size,
    });
    let itemDto = operation.item;
    if (!itemDto) {
      try {
        itemDto = await this.transport.command<
          ReviewItemDTO,
          {
            project_ref_id: string;
            item_code: string;
            episode_no?: number;
            title: string;
            original_file_id: string;
            version_note?: string;
          }
        >(
          `/api/v1/final-cut-review/edit/projects/${input.projectRefId}/items`,
          'CreateReviewItem',
          {
            project_ref_id: input.projectRefId,
            item_code: input.episode || input.title,
            episode_no: Number.isFinite(Number(input.episode)) ? Number(input.episode) : undefined,
            title: input.title,
            original_file_id: upload.file_id ?? '',
          },
          context,
          undefined,
          { idempotent: true, commandId: operation.commandId },
        );
      } catch (error) {
        if (!(error instanceof FinalCutReviewHttpError) || error.httpStatus >= 500) {
          markV1ListConfirmationRequired(input.projectRefId);
        }
        throw error;
      }
      operation.item = itemDto;
      markV1ListConfirmationRequired(input.projectRefId);
    }
    this.uploadOperation.releaseCompleted(input.file, upload.upload_id);
    const versionDto =
      operation.version ??
      (await this.transport.requestJson<ReviewVersionDTO>(
        `/api/v1/final-cut-review/projects/${input.projectRefId}/items/${itemDto.id}/versions/${itemDto.current_version_id}`,
      ));
    operation.version = versionDto;
    input.onProgress?.({
      stage: 'completed',
      percent: 100,
      bytesSent: input.file.size,
      totalBytes: input.file.size,
    });
    return {
      item: itemFromDto(itemDto),
      version: versionFromDto(versionDto, this.transport.baseUrl, itemDto),
    };
  };

  readonly deleteReviewItem: ReviewApiPort['deleteReviewItem'] = async (input, context) => {
    this.transport.assertWriteContext(context, ['edit']);
    if (input.confirmed !== true) {
      throw new ReviewDomainError('删除分集必须二次确认', 'RESOURCE_STATE_CONFLICT');
    }
    await this.transport.projectForWrite(input.projectRefId);
    const item = await this.transport.itemForLock(input.projectRefId, input.reviewItemId);
    const deleted = await this.transport.command<
      ReviewItemDTO,
      { project_ref_id: string; review_item_id: string; confirmed: true }
    >(
      `/api/v1/final-cut-review/edit/projects/${input.projectRefId}/items/${input.reviewItemId}/delete`,
      'DeleteReviewItem',
      { project_ref_id: input.projectRefId, review_item_id: input.reviewItemId, confirmed: input.confirmed },
      context,
      item.lock_version,
    );
    return itemFromDto(deleted);
  };

  readonly appendVersion: ReviewApiPort['appendVersion'] = async (input, context) => {
    this.transport.assertWriteContext(context, ['edit']);
    await this.transport.projectForWrite(input.projectRefId);
    const signature = JSON.stringify([
      input.projectRefId,
      input.reviewItemId,
      input.versionNote,
      input.changeSummary,
      input.supersedeReason,
      input.file.name,
      input.file.size,
      input.file.type,
      input.file.lastModified,
    ]);
    let operation = this.pendingAppendVersions.get(input.file);
    if (!operation || operation.signature !== signature) {
      operation = { signature, commandId: randomId('UploadReviewVersion') };
      this.pendingAppendVersions.set(input.file, operation);
    }
    if (!operation.item) {
      const itemDto = await this.transport.requestJson<ReviewItemDTO>(
        `/api/v1/final-cut-review/projects/${input.projectRefId}/items/${input.reviewItemId}`,
      );
      assertCanAppendVersion(itemDto);
      operation.item = itemDto;
    }
    const itemDto = operation.item;
    const upload = operation.upload ?? (await this.upload(input.file, context, input.onProgress));
    operation.upload = upload;
    input.onProgress?.({
      stage: 'binding',
      percent: 95,
      bytesSent: input.file.size,
      totalBytes: input.file.size,
    });
    let version = operation.version;
    if (!version) {
      try {
        version = await this.transport.command<
          ReviewVersionDTO,
          {
            project_ref_id: string;
            review_item_id: string;
            original_file_id: string;
            version_note?: string;
            change_summary?: string;
            supersede_reason?: string;
          }
        >(
          `/api/v1/final-cut-review/edit/projects/${input.projectRefId}/items/${input.reviewItemId}/versions`,
          'UploadReviewVersion',
          {
            project_ref_id: input.projectRefId,
            review_item_id: input.reviewItemId,
            original_file_id: upload.file_id ?? '',
            version_note: input.versionNote,
            change_summary: input.changeSummary,
            supersede_reason: input.supersedeReason,
          },
          context,
          itemDto.lock_version,
          { idempotent: true, commandId: operation.commandId },
        );
      } catch (error) {
        if (!(error instanceof FinalCutReviewHttpError) || error.httpStatus >= 500) {
          markAppendVersionConfirmationRequired(input.projectRefId, input.reviewItemId);
        }
        throw error;
      }
      operation.version = version;
      markAppendVersionConfirmationRequired(input.projectRefId, input.reviewItemId);
    }
    this.uploadOperation.releaseCompleted(input.file, upload.upload_id);
    const updatedItem = await this.transport.requestJson<ReviewItemDTO>(
      `/api/v1/final-cut-review/projects/${input.projectRefId}/items/${input.reviewItemId}`,
    );
    operation.item = updatedItem;
    input.onProgress?.({
      stage: 'completed',
      percent: 100,
      bytesSent: input.file.size,
      totalBytes: input.file.size,
    });
    return versionFromDto(version, this.transport.baseUrl, updatedItem);
  };

  async uploadFile(
    file: File,
    context: ExecutionContext,
    onProgress?: (progress: UploadProgress) => void,
  ): Promise<UploadSessionDTO> {
    return this.uploadOperation.upload(file, context, onProgress);
  }
}

function assertCanAppendVersion(item: ReviewItemDTO): void {
  if (item.workflow_status === 'finalized') {
    throw new ReviewDomainError('已定稿后不能追加版本', 'FINALIZED_READONLY');
  }
  if (item.unresolved_current_version_count + item.resolved_current_version_count < 1) {
    throw new ReviewDomainError('当前版本至少需要一条意见才能上传下一版本', 'NEXT_VERSION_REQUIRES_ISSUE');
  }
}
