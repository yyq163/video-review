import type {
  FinalizationDTO,
  PackageDownloadSessionDTO,
  PackageSnapshotDTO,
} from '../contracts-generated/backend-contract';
import type { ReviewApiPort, ReviewHostBridge } from '../ports';
import { originalMediaFromDto, packageFromDto } from './http-review-project-mappers';
import type { HttpReviewTransport } from './http-review-transport';

const PACKAGE_PREPARATION_POLL_INTERVAL_MS = 500;
const PACKAGE_PREPARATION_MAX_POLLS = 120;
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
    projectRefId,
    context,
  ) => {
    this.transport.assertWriteContext(context, ['review']);
    const snapshot = await this.transport.command<PackageSnapshotDTO, { project_ref_id: string }>(
      `/api/v1/final-cut-review/review/projects/${projectRefId}/finalized-originals/packages`,
      'PrepareFinalizedPackage',
      { project_ref_id: projectRefId },
      context,
      undefined,
      { idempotent: true },
    );
    let current = snapshot;
    for (
      let attempt = 0;
      current.status === 'preparing' && attempt < PACKAGE_PREPARATION_MAX_POLLS;
      attempt += 1
    ) {
      if (attempt > 0) {
        await wait(PACKAGE_PREPARATION_POLL_INTERVAL_MS);
      }
      current = await this.transport.requestJsonWithTimeout<PackageSnapshotDTO>(
        `/api/v1/final-cut-review/review/projects/${projectRefId}/finalized-originals/packages/${snapshot.id}`,
        PACKAGE_PREPARATION_REQUEST_TIMEOUT_MS,
      );
    }
    if (current.status === 'preparing') {
      throw new Error('项目包准备超时，请重试。');
    }
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
  ) => {
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
    });
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

function wait(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}
