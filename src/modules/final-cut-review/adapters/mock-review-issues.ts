import type { ReviewApiPort } from '../ports';
import type { MockReviewContext } from './mock-review-context';

type IssueApi = Pick<
  ReviewApiPort,
  'createIssue' | 'replyToIssue' | 'editIssue' | 'resolveIssue' | 'reopenIssue' | 'deleteIssue'
>;

export class MockReviewIssues implements IssueApi {
  constructor(private readonly context: MockReviewContext) {}

  readonly createIssue: ReviewApiPort['createIssue'] = async (input, executionContext) => {
    await this.context.ready();
    this.context.assertContext(executionContext, 'review.issue.create');
    return this.context.repository.createIssue(input);
  };

  readonly replyToIssue: ReviewApiPort['replyToIssue'] = async (input, executionContext) => {
    await this.context.ready();
    this.context.assertContext(executionContext, 'review.issue.reply');
    return this.context.repository.replyToIssue(input);
  };

  readonly editIssue: ReviewApiPort['editIssue'] = async (input, executionContext) => {
    await this.context.ready();
    this.context.assertContext(executionContext, 'review.issue.update');
    return this.context.repository.editIssue(input);
  };

  readonly resolveIssue: ReviewApiPort['resolveIssue'] = async (input, executionContext) => {
    await this.context.ready();
    this.context.assertContext(executionContext, 'review.issue.resolve');
    return this.context.repository.setIssueStatus({ ...input, status: 'resolved' });
  };

  readonly reopenIssue: ReviewApiPort['reopenIssue'] = async (input, executionContext) => {
    await this.context.ready();
    this.context.assertContext(executionContext, 'review.issue.reopen');
    return this.context.repository.setIssueStatus({ ...input, status: 'unresolved' });
  };

  readonly deleteIssue: ReviewApiPort['deleteIssue'] = async (input, executionContext) => {
    await this.context.ready();
    this.context.assertContext(executionContext, 'review.issue.delete');
    return this.context.repository.deleteIssue(input);
  };
}
