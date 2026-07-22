import type {
  CommandType,
  ReviewIssueDTO,
  ThreadMessageDTO,
} from '../contracts-generated/backend-contract';
import type { ExecutionContext, IssueId, ProjectRefId, ReviewItemId, VersionId } from '../contracts/types';
import type { ReviewApiPort } from '../ports';
import { annotationPayload, issueFromDto } from './http-review-issue-mappers';
import type { HttpReviewQueries } from './http-review-queries';
import type { HttpReviewTransport } from './http-review-transport';

type IssueApi = Pick<
  ReviewApiPort,
  'createIssue' | 'replyToIssue' | 'editIssue' | 'resolveIssue' | 'reopenIssue' | 'deleteIssue'
>;

interface IssueStatusInput {
  projectRefId: ProjectRefId;
  reviewItemId: ReviewItemId;
  versionId: VersionId;
  issueId: IssueId;
}

export class HttpReviewIssues implements IssueApi {
  constructor(
    private readonly transport: HttpReviewTransport,
    private readonly queries: HttpReviewQueries,
  ) {}

  readonly createIssue: ReviewApiPort['createIssue'] = async (input, context) => {
    this.transport.assertWriteContext(context, ['review']);
    void input.severity;
    const item = await this.transport.itemForLock(input.projectRefId, input.reviewItemId);
    const issue = await this.transport.command<
      ReviewIssueDTO,
      {
        project_ref_id: string;
        review_item_id: string;
        version_id: string;
        content: string;
        timestamp_ms: number;
        frame_number: number;
        annotation?: ReturnType<typeof annotationPayload>;
      }
    >(
      `/api/v1/final-cut-review/review/projects/${input.projectRefId}/items/${input.reviewItemId}/versions/${input.versionId}/issues`,
      'CreateReviewIssue',
      {
        project_ref_id: input.projectRefId,
        review_item_id: input.reviewItemId,
        version_id: input.versionId,
        content: input.body,
        timestamp_ms: input.timestampMs,
        frame_number: input.frameNumber,
        annotation: annotationPayload(input),
      },
      context,
      item.lock_version,
      { idempotent: true },
    );
    return issueFromDto(issue);
  };

  readonly replyToIssue: ReviewApiPort['replyToIssue'] = async (input, context) => {
    this.transport.assertWriteContext(context, ['review']);
    await this.transport.command<
      ThreadMessageDTO,
      {
        project_ref_id: string;
        review_item_id: string;
        version_id: string;
        issue_id: string;
        content: string;
      }
    >(
      `/api/v1/final-cut-review/review/projects/${input.projectRefId}/items/${input.reviewItemId}/versions/${input.versionId}/issues/${input.issueId}/messages`,
      'AddReviewMessage',
      {
        project_ref_id: input.projectRefId,
        review_item_id: input.reviewItemId,
        version_id: input.versionId,
        issue_id: input.issueId,
        content: input.body,
      },
      context,
      undefined,
      { idempotent: true },
    );
    return this.queries.issueWithMessages(
      input.projectRefId,
      input.reviewItemId,
      input.versionId,
      input.issueId,
    );
  };

  readonly editIssue: ReviewApiPort['editIssue'] = async (input, context) => {
    this.transport.assertWriteContext(context, ['review']);
    void input.timestampMs;
    void input.frameNumber;
    const current = await this.transport.issueForLock(
      input.projectRefId,
      input.reviewItemId,
      input.versionId,
      input.issueId,
    );
    const issue = await this.transport.command<
      ReviewIssueDTO,
      {
        project_ref_id: string;
        review_item_id: string;
        version_id: string;
        issue_id: string;
        content?: string;
        annotation?: ReturnType<typeof annotationPayload>;
      }
    >(
      `/api/v1/final-cut-review/review/projects/${input.projectRefId}/items/${input.reviewItemId}/versions/${input.versionId}/issues/${input.issueId}`,
      'UpdateReviewIssue',
      {
        project_ref_id: input.projectRefId,
        review_item_id: input.reviewItemId,
        version_id: input.versionId,
        issue_id: input.issueId,
        content: input.body,
        annotation: annotationPayload(input),
      },
      context,
      current.lock_version,
      { method: 'PATCH' },
    );
    return issueFromDto(issue);
  };

  readonly resolveIssue: ReviewApiPort['resolveIssue'] = async (input, context) => {
    this.transport.assertWriteContext(context, ['review']);
    return this.issueStatusCommand(input, context, 'ResolveReviewIssue', 'resolve');
  };

  readonly reopenIssue: ReviewApiPort['reopenIssue'] = async (input, context) => {
    this.transport.assertWriteContext(context, ['review']);
    return this.issueStatusCommand(input, context, 'ReopenReviewIssue', 'reopen');
  };

  readonly deleteIssue: ReviewApiPort['deleteIssue'] = async (input, context) => {
    this.transport.assertWriteContext(context, ['review']);
    const current = await this.transport.issueForLock(
      input.projectRefId,
      input.reviewItemId,
      input.versionId,
      input.issueId,
    );
    const issue = await this.transport.command<
      ReviewIssueDTO,
      { project_ref_id: string; review_item_id: string; version_id: string; issue_id: string }
    >(
      `/api/v1/final-cut-review/review/projects/${input.projectRefId}/items/${input.reviewItemId}/versions/${input.versionId}/issues/${input.issueId}/soft-delete`,
      'SoftDeleteReviewIssue',
      {
        project_ref_id: input.projectRefId,
        review_item_id: input.reviewItemId,
        version_id: input.versionId,
        issue_id: input.issueId,
      },
      context,
      current.lock_version,
    );
    return issueFromDto(issue);
  };

  private async issueStatusCommand(
    input: IssueStatusInput,
    context: ExecutionContext,
    commandType: Extract<CommandType, 'ResolveReviewIssue' | 'ReopenReviewIssue'>,
    action: 'resolve' | 'reopen',
  ) {
    const current = await this.transport.issueForLock(
      input.projectRefId,
      input.reviewItemId,
      input.versionId,
      input.issueId,
    );
    const issue = await this.transport.command<
      ReviewIssueDTO,
      { project_ref_id: string; review_item_id: string; version_id: string; issue_id: string }
    >(
      `/api/v1/final-cut-review/review/projects/${input.projectRefId}/items/${input.reviewItemId}/versions/${input.versionId}/issues/${input.issueId}/${action}`,
      commandType,
      {
        project_ref_id: input.projectRefId,
        review_item_id: input.reviewItemId,
        version_id: input.versionId,
        issue_id: input.issueId,
      },
      context,
      current.lock_version,
    );
    return issueFromDto(issue);
  }
}
