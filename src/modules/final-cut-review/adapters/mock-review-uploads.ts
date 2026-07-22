import type { UploadProgress } from '../contracts/types';
import type { ReviewApiPort } from '../ports';
import type { MockReviewContext } from './mock-review-context';

type UploadApi = Pick<
  ReviewApiPort,
  'createReviewItemWithVersion' | 'updateReviewItem' | 'deleteReviewItem' | 'appendVersion'
>;

function reportUploadProgress(
  callback: ((progress: UploadProgress) => void) | undefined,
  progress: UploadProgress,
): void {
  callback?.({ ...progress, percent: Math.min(100, Math.max(0, progress.percent)) });
}

async function reportMockUploadProgress(
  callback: ((progress: UploadProgress) => void) | undefined,
  progress: UploadProgress,
): Promise<void> {
  reportUploadProgress(callback, progress);
  if (callback) {
    await new Promise((resolve) => globalThis.setTimeout(resolve, 60));
  }
}

export class MockReviewUploads implements UploadApi {
  constructor(private readonly context: MockReviewContext) {}

  readonly createReviewItemWithVersion: ReviewApiPort['createReviewItemWithVersion'] = async (
    input,
    executionContext,
  ) => {
    await this.context.ready();
    this.context.assertContext(executionContext, 'review.item.create');
    this.context.repository.ensureProjectWritable(input.projectRefId);
    await reportMockUploadProgress(input.onProgress, {
      stage: 'validating',
      percent: 0,
      totalBytes: input.file.size,
    });
    await reportMockUploadProgress(input.onProgress, {
      stage: 'initiated',
      percent: 5,
      totalBytes: input.file.size,
    });
    const storedFile = await this.context.fileStorage.storeOriginal(input.file);
    await reportMockUploadProgress(input.onProgress, {
      stage: 'uploading',
      percent: 85,
      bytesSent: input.file.size,
      totalBytes: input.file.size,
    });
    await reportMockUploadProgress(input.onProgress, {
      stage: 'binding',
      percent: 95,
      bytesSent: input.file.size,
      totalBytes: input.file.size,
    });
    const result = await this.context.repository.createReviewItemWithVersion({ ...input, file: storedFile });
    await reportMockUploadProgress(input.onProgress, {
      stage: 'completed',
      percent: 100,
      bytesSent: input.file.size,
      totalBytes: input.file.size,
    });
    return result;
  };

  readonly updateReviewItem: ReviewApiPort['updateReviewItem'] = async (input, executionContext) => {
    await this.context.ready();
    this.context.assertContext(executionContext, 'review.item.update');
    return this.context.repository.updateReviewItem(input);
  };

  readonly deleteReviewItem: ReviewApiPort['deleteReviewItem'] = async (input, executionContext) => {
    await this.context.ready();
    this.context.assertContext(executionContext, 'review.item.delete');
    return this.context.repository.deleteReviewItem(input);
  };

  readonly appendVersion: ReviewApiPort['appendVersion'] = async (input, executionContext) => {
    await this.context.ready();
    this.context.assertContext(executionContext, 'review.version.upload');
    this.context.repository.ensureAppendVersionWritable(input);
    await reportMockUploadProgress(input.onProgress, {
      stage: 'validating',
      percent: 0,
      totalBytes: input.file.size,
    });
    await reportMockUploadProgress(input.onProgress, {
      stage: 'initiated',
      percent: 5,
      totalBytes: input.file.size,
    });
    const storedFile = await this.context.fileStorage.storeOriginal(input.file);
    await reportMockUploadProgress(input.onProgress, {
      stage: 'uploading',
      percent: 85,
      bytesSent: input.file.size,
      totalBytes: input.file.size,
    });
    await reportMockUploadProgress(input.onProgress, {
      stage: 'binding',
      percent: 95,
      bytesSent: input.file.size,
      totalBytes: input.file.size,
    });
    const version = await this.context.repository.appendVersion({ ...input, file: storedFile });
    await reportMockUploadProgress(input.onProgress, {
      stage: 'completed',
      percent: 100,
      bytesSent: input.file.size,
      totalBytes: input.file.size,
    });
    return version;
  };
}
