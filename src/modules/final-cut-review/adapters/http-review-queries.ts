import {
  FinalCutReviewHttpError,
  type FinalizationDTO,
  type ReviewIssueDTO,
  type ReviewIssueRevisionDTO,
  type ThreadMessageDTO,
} from '../contracts-generated/backend-contract';
import type {
  FinalizationRecord,
  IssueId,
  ProjectRefId,
  ReviewIssue,
  ReviewItemId,
  VersionId,
} from '../contracts/types';
import type { QueryOptions } from '../ports';
import { issueFromDto } from './http-review-issue-mappers';
import { finalizationFromDto } from './http-review-project-mappers';
import type { HttpReviewTransport } from './http-review-transport';

export class HttpReviewQueries {
  constructor(private readonly transport: HttpReviewTransport) {}

  async optionalFinalization(
    projectRefId: ProjectRefId,
    reviewItemId: ReviewItemId,
    options?: QueryOptions,
  ): Promise<FinalizationRecord | null> {
    try {
      const finalization = await this.transport.requestJson<FinalizationDTO | null>(
        `/api/v1/final-cut-review/projects/${projectRefId}/items/${reviewItemId}/finalization`,
        { signal: options?.signal },
      );
      return finalization ? finalizationFromDto(finalization) : null;
    } catch (error) {
      if (error instanceof FinalCutReviewHttpError && error.httpStatus === 404) {
        return null;
      }
      throw error;
    }
  }

  async issuesForVersion(
    projectRefId: ProjectRefId,
    reviewItemId: ReviewItemId,
    versionId: VersionId,
    options?: QueryOptions,
  ): Promise<ReviewIssue[]> {
    const issues = await this.transport.requestList<ReviewIssueDTO[]>(
      `/api/v1/final-cut-review/projects/${projectRefId}/items/${reviewItemId}/versions/${versionId}/issues`,
      options,
    );
    return Promise.all(
      issues.map((issue) => this.issueWithMessages(projectRefId, reviewItemId, versionId, issue.id, issue, options)),
    );
  }

  async issueWithMessages(
    projectRefId: ProjectRefId,
    reviewItemId: ReviewItemId,
    versionId: VersionId,
    issueId: IssueId,
    issueDto?: ReviewIssueDTO,
    options?: QueryOptions,
  ): Promise<ReviewIssue> {
    const [issue, messages, revisions] = await Promise.all([
      issueDto ??
        this.transport.requestJson<ReviewIssueDTO>(
          `/api/v1/final-cut-review/projects/${projectRefId}/items/${reviewItemId}/versions/${versionId}/issues/${issueId}`,
          { signal: options?.signal },
        ),
      this.transport.requestList<ThreadMessageDTO[]>(
        `/api/v1/final-cut-review/projects/${projectRefId}/items/${reviewItemId}/versions/${versionId}/issues/${issueId}/messages`,
        options,
      ),
      this.transport.requestList<ReviewIssueRevisionDTO[]>(
        `/api/v1/final-cut-review/projects/${projectRefId}/items/${reviewItemId}/versions/${versionId}/issues/${issueId}/revisions`,
        options,
      ),
    ]);
    return issueFromDto(issue, messages, revisions);
  }
}
