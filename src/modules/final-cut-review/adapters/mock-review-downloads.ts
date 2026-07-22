import type { ReviewApiPort } from '../ports';
import type { MockReviewContext } from './mock-review-context';

type DownloadApi = Pick<
  ReviewApiPort,
  'downloadFinalizedOriginal' | 'createProjectFinalizedPackage' | 'downloadProjectFinalizedPackage'
>;

export class MockReviewDownloads implements DownloadApi {
  constructor(private readonly context: MockReviewContext) {}

  readonly downloadFinalizedOriginal: ReviewApiPort['downloadFinalizedOriginal'] = async (
    input,
    executionContext,
  ) => {
    await this.context.ready();
    this.context.assertContext(executionContext, 'review.download.finalized_original');
    const workspace = await this.context.repository.getWorkspace(input);
    if (
      !workspace.activeFinalization ||
      workspace.activeFinalization.versionId !== workspace.item.currentVersionId
    ) {
      throw new Error('当前版本未定稿，不能下载原片');
    }
    const original = await this.context.fileStorage.getOriginal(
      workspace.activeFinalization.originalFileId,
    );
    if (!original.blob) throw new Error('本地原片内容不可用');
    this.context.hostBridge.downloadBlob(
      original.blob,
      workspace.activeFinalization.originalMedia.originalFilename,
    );
    return original;
  };

  readonly createProjectFinalizedPackage: ReviewApiPort['createProjectFinalizedPackage'] = async (
    projectRefId,
    executionContext,
  ) => {
    await this.context.ready();
    this.context.assertContext(executionContext, 'review.package.create');
    this.context.repository.ensureProjectNotDeleted(projectRefId);
    const detail = await this.context.repository.getProjectDetail(projectRefId);
    return this.context.packageAdapter.createProjectPackage({
      project: detail.project,
      items: this.context.repository.getAllProjectItems(projectRefId),
      versions: this.context.repository.getAllProjectVersions(projectRefId),
      finalizations: this.context.repository.getActiveFinalizations(projectRefId),
      fileStorage: this.context.fileStorage,
    });
  };

  readonly downloadProjectFinalizedPackage: ReviewApiPort['downloadProjectFinalizedPackage'] = async (
    result,
    executionContext,
  ) => {
    await this.context.ready();
    this.context.assertContext(executionContext, 'review.package.download');
    if (!result.blob) throw new Error('本地项目包内容不可用');
    this.context.hostBridge.downloadBlob(result.blob, result.fileName);
  };
}
