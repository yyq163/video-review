import type { ReviewApiPort } from '../ports';
import type { MockReviewContext } from './mock-review-context';

type WorkflowApi = Pick<ReviewApiPort, 'startReview' | 'requestChanges' | 'finalizeCurrentVersion'>;

export class MockReviewWorkflow implements WorkflowApi {
  constructor(private readonly context: MockReviewContext) {}

  readonly startReview: ReviewApiPort['startReview'] = async (input, executionContext) => {
    await this.context.ready();
    this.context.assertContext(executionContext, 'review.session.start');
    return this.context.repository.startReview(input);
  };

  readonly requestChanges: ReviewApiPort['requestChanges'] = async (input, executionContext) => {
    await this.context.ready();
    this.context.assertContext(executionContext, 'review.session.request_changes');
    return this.context.repository.requestChanges(input);
  };

  readonly finalizeCurrentVersion: ReviewApiPort['finalizeCurrentVersion'] = async (
    input,
    executionContext,
  ) => {
    await this.context.ready();
    this.context.assertContext(executionContext, 'review.finalization.create');
    if (input.confirmed !== true) throw new Error('定稿必须二次确认');
    const workspace = await this.context.repository.getWorkspace(input);
    await this.context.fileStorage.getOriginal(workspace.currentVersion.originalFileId);
    return this.context.repository.finalizeCurrentVersion(input);
  };
}
