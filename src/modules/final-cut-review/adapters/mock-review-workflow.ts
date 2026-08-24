import type { ReviewApiPort } from '../ports';
import type { MockReviewContext } from './mock-review-context';

type WorkflowApi = Pick<
  ReviewApiPort,
  'startReview' | 'finalizeProject' | 'revokeProjectFinalization' | 'finalizeCurrentVersion' | 'revokeFinalization' | 'retryFinalizationCleanup'
>;

export class MockReviewWorkflow implements WorkflowApi {
  constructor(private readonly context: MockReviewContext) {}

  readonly startReview: ReviewApiPort['startReview'] = async (input, executionContext) => {
    await this.context.ready();
    this.context.assertContext(executionContext, 'review.session.start');
    return this.context.repository.startReview(input);
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

  readonly finalizeProject: ReviewApiPort['finalizeProject'] = async (input, executionContext) => {
    await this.context.ready();
    this.context.assertContext(executionContext, 'review.finalization.create');
    await this.context.repository.finalizeProject(input);
    return this.context.repository.getProjectSummary(input.projectRefId);
  };

  readonly revokeProjectFinalization: ReviewApiPort['revokeProjectFinalization'] = async (input, executionContext) => {
    await this.context.ready();
    this.context.assertContext(executionContext, 'review.finalization.revoke');
    await this.context.repository.revokeProjectFinalization(input);
    return this.context.repository.getProjectSummary(input.projectRefId);
  };

  readonly revokeFinalization: ReviewApiPort['revokeFinalization'] = async (input, executionContext) => {
    await this.context.ready();
    this.context.assertContext(executionContext, 'review.finalization.revoke');
    if (input.confirmed !== true) throw new Error('撤销定稿必须二次确认');
    return this.context.repository.revokeFinalization(input);
  };

  readonly retryFinalizationCleanup: ReviewApiPort['retryFinalizationCleanup'] = async (
    input,
    executionContext,
  ) => {
    await this.context.ready();
    this.context.assertContext(executionContext, 'review.finalization.revoke');
    return this.context.repository.retryFinalizationCleanup(input);
  };
}
