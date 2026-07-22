import {
  FinalCutReviewHttpError,
  type CommandEnvelope,
  type CommandType,
  type ErrorEnvelope,
  type Envelope,
  type FinalCutReviewClient,
  type ProjectDTO,
  type ReviewIssueDTO,
  type ReviewItemDTO,
} from '../contracts-generated/backend-contract';
import type {
  EntryMode,
  ExecutionContext,
  IssueId,
  ProjectRefId,
  ReviewItemId,
  VersionId,
} from '../contracts/types';
import { ReviewDomainError } from '../core/errors';
import { createUuid } from '../core/uuid';
import type { QueryOptions } from '../ports';

type JsonHeaders = Record<string, string>;

const LIST_PAGE_SIZE = 200;
const LIST_SNAPSHOT_MAX_ATTEMPTS = 3;
const BINARY_UPLOAD_INACTIVITY_TIMEOUT_MS = 180_000;

class ListSnapshotChangedError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ListSnapshotChangedError';
  }
}

export function randomId(prefix: string): string {
  return `${prefix}_${createUuid()}`;
}

export class HttpReviewTransport {
  constructor(
    public readonly mode: EntryMode,
    public readonly client: FinalCutReviewClient,
    public readonly baseUrl: string,
  ) {}

  queryInit(options?: QueryOptions): RequestInit {
    return { signal: options?.signal, credentials: 'include' };
  }

  async requestList<T>(path: string, options?: QueryOptions): Promise<T> {
    let lastSnapshotChange: ListSnapshotChangedError | undefined;

    for (let attempt = 1; attempt <= LIST_SNAPSHOT_MAX_ATTEMPTS; attempt += 1) {
      try {
        return await this.requestListSnapshot<T>(path, options);
      } catch (error) {
        if (!(error instanceof ListSnapshotChangedError)) {
          throw error;
        }
        lastSnapshotChange = error;
      }
    }

    throw new Error(
      `HTTP list pagination did not stabilize after ${LIST_SNAPSHOT_MAX_ATTEMPTS} snapshot attempts: ${lastSnapshotChange?.message ?? 'unknown snapshot change'}`,
    );
  }

  async command<TData, TPayload extends object>(
    path: string,
    commandType: CommandType,
    payload: TPayload,
    context: ExecutionContext,
    expectedAggregateVersion?: number,
    options: { method?: 'POST' | 'PATCH'; idempotent?: boolean; commandId?: string } = {},
  ): Promise<TData> {
    this.assertWriteContext(context, ['edit', 'review']);
    const commandId = options.commandId ?? randomId(commandType);
    const envelope: CommandEnvelope<TPayload> = {
      command_id: commandId,
      command_type: commandType,
      contract_version: '1.0',
      expected_aggregate_version: expectedAggregateVersion,
      payload,
    };
    const headers: JsonHeaders = this.contextHeaders(context);
    if (options.idempotent) {
      headers['Idempotency-Key'] = commandId;
    }
    if (expectedAggregateVersion !== undefined) {
      headers['If-Match'] = String(expectedAggregateVersion);
    }
    return this.client.command<TData, TPayload>(path, envelope, {
      method: options.method ?? 'POST',
      headers,
      credentials: 'include',
    });
  }

  contextHeaders(context: ExecutionContext): JsonHeaders {
    return { 'X-Request-ID': context.requestId };
  }

  assertWriteContext(context: ExecutionContext, allowedModes: readonly EntryMode[]): void {
    if (context.entryMode !== this.mode || !allowedModes.includes(this.mode)) {
      throw new ReviewDomainError('执行上下文与入口不匹配', 'EXECUTION_CONTEXT_MISMATCH');
    }
  }

  jsonInit(
    method: 'POST' | 'PATCH',
    body: object,
    context?: ExecutionContext,
    additionalHeaders: JsonHeaders = {},
  ): RequestInit {
    return {
      method,
      body: JSON.stringify(body),
      credentials: 'include',
      headers: {
        ...(context ? this.contextHeaders(context) : {}),
        ...additionalHeaders,
        'Content-Type': 'application/json',
      },
    };
  }

  async requestJson<T>(path: string, init: RequestInit = {}): Promise<T> {
    return this.envelopeFetch<T>(path, {
      ...init,
      credentials: init.credentials ?? 'include',
      headers: {
        'Content-Type': 'application/json',
        ...(init.headers ?? {}),
      },
    });
  }

  requestBinaryJson<T>(
    path: string,
    body: Blob,
    context: ExecutionContext,
    onUploadProgress?: (loaded: number) => void,
  ): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      let inactivityTimer: number | undefined;
      let settled = false;

      const clearInactivityTimer = () => {
        if (inactivityTimer !== undefined) {
          window.clearTimeout(inactivityTimer);
          inactivityTimer = undefined;
        }
      };
      const settle = (result: { data: T } | { error: unknown }) => {
        if (settled) return false;
        settled = true;
        clearInactivityTimer();
        if ('data' in result) resolve(result.data);
        else reject(result.error);
        return true;
      };
      const abortForInactivity = () => {
        const didSettle = settle({ error: new Error('上传分片长时间无进度，请重试。') });
        if (didSettle) xhr.abort();
      };
      const resetInactivityTimer = () => {
        clearInactivityTimer();
        inactivityTimer = window.setTimeout(abortForInactivity, BINARY_UPLOAD_INACTIVITY_TIMEOUT_MS);
      };

      // Safari requires upload listeners to be attached before open().
      xhr.upload.addEventListener('progress', (event) => {
        if (settled) return;
        resetInactivityTimer();
        if (event.lengthComputable && Number.isFinite(event.loaded)) {
          onUploadProgress?.(Math.max(0, Math.min(event.loaded, body.size)));
        }
      });
      xhr.addEventListener('load', () => {
        let parsed: unknown;
        try {
          parsed = JSON.parse(xhr.responseText) as unknown;
        } catch {
          settle({ error: new Error('上传分片响应格式无效，请重试。') });
          return;
        }

        if (xhr.status >= 200 && xhr.status < 300) {
          if (!isRecord(parsed) || !Object.prototype.hasOwnProperty.call(parsed, 'data')) {
            settle({ error: new Error('上传分片响应格式无效，请重试。') });
            return;
          }
          settle({ data: (parsed as unknown as Envelope<T>).data });
          return;
        }

        const error = isRecord(parsed) ? (parsed as unknown as ErrorEnvelope).error : undefined;
        if (error) {
          settle({ error: new FinalCutReviewHttpError(error, xhr.status) });
          return;
        }
        settle({ error: new Error(`HTTP ${xhr.status}`) });
      });
      xhr.addEventListener('error', () => {
        settle({ error: new TypeError('上传分片网络请求失败，请重试。') });
      });
      xhr.addEventListener('timeout', abortForInactivity);
      xhr.addEventListener('abort', () => {
        settle({ error: new DOMException('上传分片已中止', 'AbortError') });
      });

      try {
        xhr.open('PUT', `${this.baseUrl}${path}`, true);
        xhr.withCredentials = true;
        xhr.setRequestHeader('X-Request-ID', context.requestId);
        xhr.setRequestHeader('Content-Type', 'application/octet-stream');
        resetInactivityTimer();
        xhr.send(body);
      } catch (error) {
        settle({ error });
      }
    });
  }

  async requestJsonWithTimeout<T>(path: string, timeoutMs: number): Promise<T> {
    const controller = new AbortController();
    let timeoutId = 0;
    const timeout = new Promise<never>((_, reject) => {
      timeoutId = window.setTimeout(() => {
        controller.abort();
        reject(new Error('项目包状态查询超时，请重试。'));
      }, timeoutMs);
    });
    try {
      return await Promise.race([this.requestJson<T>(path, { signal: controller.signal }), timeout]);
    } finally {
      window.clearTimeout(timeoutId);
    }
  }

  async projectForWrite(projectRefId: ProjectRefId): Promise<ProjectDTO> {
    const project = await this.client.getProject(projectRefId, this.queryInit());
    if (project.lifecycle_status === 'archived') {
      throw new ReviewDomainError('归档项目只读，恢复后才能修改', 'PROJECT_ARCHIVED_READONLY');
    }
    if (project.deleted_at) {
      throw new ReviewDomainError('项目已删除', 'PROJECT_DELETED_READONLY');
    }
    return project;
  }

  itemForLock(projectRefId: ProjectRefId, reviewItemId: ReviewItemId): Promise<ReviewItemDTO> {
    return this.requestJson<ReviewItemDTO>(`/api/v1/final-cut-review/projects/${projectRefId}/items/${reviewItemId}`);
  }

  issueForLock(
    projectRefId: ProjectRefId,
    reviewItemId: ReviewItemId,
    versionId: VersionId,
    issueId: IssueId,
  ): Promise<ReviewIssueDTO> {
    return this.requestJson<ReviewIssueDTO>(
      `/api/v1/final-cut-review/projects/${projectRefId}/items/${reviewItemId}/versions/${versionId}/issues/${issueId}`,
    );
  }

  private async envelopeFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
    const envelope = await this.fetchEnvelope<T>(path, init);
    return envelope.data;
  }

  private async requestListSnapshot<T>(path: string, options?: QueryOptions): Promise<T> {
    const records: unknown[] = [];
    const resourceIds = new Set<string>();
    let expectedTotalCount: number | undefined;
    let expectedPageSize: number | undefined;

    for (let page = 1; ; page += 1) {
      const separator = path.includes('?') ? '&' : '?';
      const envelope = await this.fetchEnvelope<unknown[]>(
        `${path}${separator}page=${page}&page_size=${LIST_PAGE_SIZE}`,
        { signal: options?.signal, credentials: 'include' },
      );
      if (!Array.isArray(envelope.data)) {
        throw new Error('HTTP list response data must be an array');
      }

      const totalCount = envelope.meta.total_count;
      const responsePage = envelope.meta.page;
      const responsePageSize = envelope.meta.page_size;
      if (typeof totalCount !== 'number' || !Number.isSafeInteger(totalCount) || totalCount < 0) {
        throw new ListSnapshotChangedError(`page ${page} returned an invalid total_count`);
      }
      if (responsePage !== page) {
        throw new ListSnapshotChangedError(`requested page ${page} but received page ${String(responsePage)}`);
      }
      if (
        typeof responsePageSize !== 'number'
        || !Number.isSafeInteger(responsePageSize)
        || responsePageSize <= 0
        || responsePageSize > LIST_PAGE_SIZE
      ) {
        throw new ListSnapshotChangedError(`page ${page} returned an invalid page_size`);
      }

      if (expectedTotalCount === undefined) {
        expectedTotalCount = totalCount;
        expectedPageSize = responsePageSize;
      } else if (totalCount !== expectedTotalCount) {
        throw new ListSnapshotChangedError(
          `total_count changed from ${expectedTotalCount} to ${totalCount} on page ${page}`,
        );
      } else if (responsePageSize !== expectedPageSize) {
        throw new ListSnapshotChangedError(
          `page_size changed from ${expectedPageSize} to ${responsePageSize} on page ${page}`,
        );
      }

      const pageSize = responsePageSize;
      const snapshotTotal = totalCount;
      if (envelope.data.length > pageSize) {
        throw new ListSnapshotChangedError(
          `page ${page} returned ${envelope.data.length} records for page_size ${pageSize}`,
        );
      }

      for (const record of envelope.data) {
        const resourceId = this.listResourceId(record, page);
        if (resourceIds.has(resourceId)) {
          throw new ListSnapshotChangedError(`resource id ${resourceId} was repeated on page ${page}`);
        }
        resourceIds.add(resourceId);
        records.push(record);
      }

      const expectedPageCount = Math.max(1, Math.ceil(snapshotTotal / pageSize));
      if (page > expectedPageCount) {
        throw new ListSnapshotChangedError(
          `received page ${page} beyond expected page count ${expectedPageCount}`,
        );
      }
      if (page < expectedPageCount) {
        if (envelope.data.length !== pageSize) {
          throw new ListSnapshotChangedError(
            `page ${page} ended early with ${envelope.data.length} of ${pageSize} records`,
          );
        }
        continue;
      }

      const expectedLastPageSize = snapshotTotal - pageSize * (page - 1);
      if (envelope.data.length !== expectedLastPageSize || records.length !== snapshotTotal) {
        throw new ListSnapshotChangedError(
          `page ${page} completed with ${records.length} unique records; expected ${snapshotTotal}`,
        );
      }
      return records as T;
    }
  }

  private listResourceId(record: unknown, page: number): string {
    if (record === null || typeof record !== 'object') {
      throw new ListSnapshotChangedError(`page ${page} returned a record without a resource id`);
    }
    const values = record as Record<string, unknown>;
    const id = values.id ?? values.project_ref_id;
    if (
      (typeof id !== 'string' && typeof id !== 'number')
      || (typeof id === 'string' && id.length === 0)
      || (typeof id === 'number' && !Number.isFinite(id))
    ) {
      throw new ListSnapshotChangedError(`page ${page} returned a record without a resource id`);
    }
    return String(id);
  }

  private async fetchEnvelope<T>(path: string, init: RequestInit = {}): Promise<Envelope<T>> {
    const response = await fetch(`${this.baseUrl}${path}`, init);
    const body = (await response.json()) as Envelope<T> | ErrorEnvelope;
    if (!response.ok) {
      const error = (body as ErrorEnvelope).error;
      if (error) {
        throw new FinalCutReviewHttpError(error, response.status);
      }
      throw new Error(`HTTP ${response.status}`);
    }
    return body as Envelope<T>;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object';
}
