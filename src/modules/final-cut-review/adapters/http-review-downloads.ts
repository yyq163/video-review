import type {
  FinalizationDTO,
  PackageDownloadSessionDTO,
  PackageSnapshotDTO,
} from '../contracts-generated/backend-contract';
import type { ReviewApiPort, ReviewHostBridge } from '../ports';
import { originalMediaFromDto, packageFromDto } from './http-review-project-mappers';
import { ReviewRequestTimeoutError, type HttpReviewTransport } from './http-review-transport';

const PACKAGE_PREPARATION_POLL_INTERVAL_MS = 500;
const PACKAGE_PREPARATION_TIMEOUT_MS = 300_000;
const PACKAGE_PREPARATION_REQUEST_TIMEOUT_MS = 10_000;

type DownloadApi = Pick<
  ReviewApiPort,
  'downloadFinalizedOriginal' | 'createProjectFinalizedPackage' | 'downloadProjectFinalizedPackage'
>;

export class HttpReviewDownloads implements DownloadApi {
  private readonly packageDownloadTokens = new Map<
    string,
    { token: string; expiresAtMs: number; expiryTimer: number }
  >();

  constructor(
    private readonly transport: HttpReviewTransport,
    private readonly hostBridge: ReviewHostBridge,
  ) {}

  readonly downloadFinalizedOriginal: ReviewApiPort['downloadFinalizedOriginal'] = async (input, context) => {
    this.transport.assertWriteContext(context, ['edit', 'review']);
    void context;
    const finalization = await this.transport.requestJson<FinalizationDTO>(
      `/api/v1/final-cut-review/projects/${input.projectRefId}/items/${input.reviewItemId}/finalization`,
    );
    const media = originalMediaFromDto(finalization);
    const downloadPath = `/api/v1/final-cut-review/projects/${input.projectRefId}/items/${input.reviewItemId}/finalized-original/download`;
    this.hostBridge.downloadUrl(`${this.transport.baseUrl}${downloadPath}`, media.originalFilename);
    return {
      originalFileId: media.originalFileId,
      fileName: media.originalFilename,
      mimeType: media.mimeType,
      size: media.fileSize,
      sha256: media.sha256,
      durationMs: media.durationMs,
      width: media.width,
      height: media.height,
      fpsNum: media.fpsNum,
      fpsDen: media.fpsDen,
      playbackUrl: `${this.transport.baseUrl}${downloadPath}`,
    };
  };

  readonly createProjectFinalizedPackage: ReviewApiPort['createProjectFinalizedPackage'] = async (
    input,
    context,
    options,
  ) => {
    options?.signal?.throwIfAborted();
    this.transport.assertWriteContext(context, ['review']);
    const snapshot = await this.transport.command<PackageSnapshotDTO, { project_ref_id: string; unified_name: string }>(
      `/api/v1/final-cut-review/review/projects/${input.projectRefId}/finalized-originals/packages`,
      'PrepareFinalizedPackage',
      { project_ref_id: input.projectRefId, unified_name: input.unifiedName },
      context,
      undefined,
      { idempotent: true, signal: options?.signal },
    );
    let current = snapshot;
    const preparationDeadlineMs = performance.now() + PACKAGE_PREPARATION_TIMEOUT_MS;
    let attempt = 0;
    while (current.status === 'preparing' && performance.now() < preparationDeadlineMs) {
      if (attempt > 0) {
        await wait(
          Math.min(PACKAGE_PREPARATION_POLL_INTERVAL_MS, preparationDeadlineMs - performance.now()),
          options?.signal,
        );
      }
      const remainingMs = preparationDeadlineMs - performance.now();
      if (remainingMs <= 0) break;
      try {
        current = await this.transport.requestJsonWithTimeout<PackageSnapshotDTO>(
          `/api/v1/final-cut-review/review/projects/${input.projectRefId}/finalized-originals/packages/${snapshot.id}`,
          Math.min(PACKAGE_PREPARATION_REQUEST_TIMEOUT_MS, remainingMs),
          options?.signal,
        );
      } catch (error) {
        if (!(error instanceof ReviewRequestTimeoutError)) throw error;
      }
      attempt += 1;
    }
    if (current.status === 'preparing') {
      throw new Error('项目包准备超过 300 秒，后台可能仍在处理，请使用相同名称重试。');
    }
    options?.signal?.throwIfAborted();
    const result = packageFromDto(current);
    if (!current.download_token || !current.download_token_expires_at) {
      throw new Error('项目包下载授权缺失，请重新准备。');
    }
    this.storePackageDownloadToken(current.id, current.download_token, current.download_token_expires_at);
    return result;
  };

  readonly downloadProjectFinalizedPackage: ReviewApiPort['downloadProjectFinalizedPackage'] = async (
    result,
    context,
    options,
  ) => {
    options?.signal?.throwIfAborted();
    this.transport.assertWriteContext(context, ['review']);
    const authorization = this.packageDownloadTokens.get(result.packageId);
    if (!authorization || authorization.expiresAtMs <= Date.now()) {
      this.removePackageDownloadToken(result.packageId);
      throw new Error('项目包下载授权已失效，请重新准备。');
    }
    const token = authorization.token;
    this.removePackageDownloadToken(result.packageId);
    const downloadPath = `/api/v1/final-cut-review/review/projects/${result.projectRefId}/finalized-originals/packages/${result.packageId}/download`;
    await this.transport.requestJson<PackageDownloadSessionDTO>(`${downloadPath}-session`, {
      method: 'POST',
      headers: { 'X-Package-Download-Token': token },
      signal: options?.signal,
    });
    options?.signal?.throwIfAborted();
    this.hostBridge.downloadUrl(`${this.transport.baseUrl}${downloadPath}`, result.fileName);
  };

  private storePackageDownloadToken(packageId: string, token: string, expiresAt: string): void {
    this.removePackageDownloadToken(packageId);
    const expiresAtMs = Date.parse(expiresAt);
    if (!Number.isFinite(expiresAtMs) || expiresAtMs <= Date.now()) {
      throw new Error('项目包下载授权已失效，请重新准备。');
    }
    const delayMs = Math.min(expiresAtMs - Date.now(), 2_147_483_647);
    const expiryTimer = window.setTimeout(() => {
      const current = this.packageDownloadTokens.get(packageId);
      if (current?.token === token) {
        this.packageDownloadTokens.delete(packageId);
      }
    }, delayMs);
    this.packageDownloadTokens.set(packageId, { token, expiresAtMs, expiryTimer });
  }

  private removePackageDownloadToken(packageId: string): void {
    const current = this.packageDownloadTokens.get(packageId);
    if (current) {
      window.clearTimeout(current.expiryTimer);
      this.packageDownloadTokens.delete(packageId);
    }
  }
}

function wait(milliseconds: number, signal?: AbortSignal): Promise<void> {
  signal?.throwIfAborted();
  return new Promise((resolve, reject) => {
    const timeoutId = window.setTimeout(() => {
      signal?.removeEventListener('abort', abort);
      resolve();
    }, milliseconds);
    const abort = () => {
      window.clearTimeout(timeoutId);
      signal?.removeEventListener('abort', abort);
      reject(signal?.reason ?? new DOMException('The operation was aborted.', 'AbortError'));
    };
    signal?.addEventListener('abort', abort, { once: true });
  });
}
