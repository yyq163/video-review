import type {
  IssueId,
  ProjectRefId,
  ReviewAnnotationSet,
  ReviewAnnotationShape,
  ReviewIssue,
  ReviewIssueRevision,
  ReviewItemId,
  VersionId,
} from '../contracts/types';
import { invariant } from '../core/errors';
import { createUuid } from '../core/uuid';
import { buildAnnotationSet, cloneIssue, nowIso } from './in-memory-review-clones';
import type { InMemoryReviewStore } from './in-memory-review-store';

interface IssueMutationInput {
  projectRefId: ProjectRefId;
  reviewItemId: ReviewItemId;
  versionId: VersionId;
  timestampMs: number;
  frameNumber: number;
  body: string;
  shapes: ReviewAnnotationShape[];
  canvasWidth: number;
  canvasHeight: number;
  videoWidth: number;
  videoHeight: number;
}

export class InMemoryReviewIssues {
  constructor(private readonly store: InMemoryReviewStore) {}

  readonly createIssue = async (
    input: IssueMutationInput & { severity: 'normal' | 'blocking' },
  ): Promise<ReviewIssue> => {
    this.store.assertProjectWritable(input.projectRefId);
    const item = this.store.getItem(input.projectRefId, input.reviewItemId);
    const version = this.store.getVersion(input.projectRefId, input.reviewItemId, input.versionId);
    invariant(item.status !== 'finalized', '已定稿后不能创建意见', 'FINALIZED_READONLY');
    invariant(
      item.currentVersionId === input.versionId,
      '历史版本只允许查看所属意见',
      'HISTORICAL_VERSION_READONLY',
    );
    invariant(input.body.trim().length > 0, '意见正文不能为空', 'ISSUE_BODY_REQUIRED');

    const timestamp = nowIso();
    if (item.status === 'pending_review') {
      this.store.items.set(item.reviewItemId, { ...item, status: 'in_review', updatedAt: timestamp });
      this.store.versions.set(version.versionId, { ...version, status: 'in_review' });
    } else {
      invariant(item.status === 'in_review', '只有审阅中版本可以创建意见', 'INVALID_STATUS_TRANSITION');
    }

    const issueId = `issue_${createUuid()}`;
    const issueNo = this.store
      .getIssuesForItem(input.projectRefId, input.reviewItemId, { includeDeleted: true })
      .reduce((max, issue) => Math.max(max, issue.issueNo), 0) + 1;
    const revisionId = `rev_${createUuid()}`;
    const annotationSetId = input.shapes.length ? `aset_${createUuid()}` : undefined;
    const revision: ReviewIssueRevision = {
      revisionId,
      projectRefId: input.projectRefId,
      reviewItemId: input.reviewItemId,
      versionId: input.versionId,
      issueId,
      revisionNo: 1,
      content: input.body.trim(),
      annotationSetId,
      timestampMs: input.timestampMs,
      frameNumber: input.frameNumber,
      createdAt: timestamp,
    };
    const annotationSet: ReviewAnnotationSet | null = annotationSetId
      ? buildAnnotationSet({ ...input, annotationSetId, issueId, revisionId, createdAt: timestamp })
      : null;
    const issue: ReviewIssue = {
      issueId,
      issueNo,
      projectRefId: input.projectRefId,
      reviewItemId: input.reviewItemId,
      versionId: input.versionId,
      status: 'unresolved',
      severity: input.severity,
      currentRevisionId: revisionId,
      timestampMs: input.timestampMs,
      frameNumber: input.frameNumber,
      lockVersion: 1,
      body: revision.content,
      annotationSetId,
      currentRevision: revision,
      currentAnnotationSet: annotationSet,
      revisions: [revision],
      replies: [],
      deletedAt: null,
      createdAt: timestamp,
      updatedAt: timestamp,
    };
    this.store.issues.set(issue.issueId, issue);
    this.store.emitChange();
    return cloneIssue(issue);
  };

  readonly editIssue = async (input: IssueMutationInput & { issueId: IssueId }): Promise<ReviewIssue> => {
    invariant(input.body.trim().length > 0, '意见正文不能为空', 'ISSUE_BODY_REQUIRED');
    this.store.assertProjectWritable(input.projectRefId);
    const item = this.store.getItem(input.projectRefId, input.reviewItemId);
    invariant(item.currentVersionId === input.versionId, '历史版本只读', 'HISTORICAL_VERSION_READONLY');
    invariant(item.status !== 'finalized', '已定稿后不能编辑意见', 'FINALIZED_READONLY');
    invariant(item.status === 'in_review', '只有审阅中版本可以编辑意见', 'INVALID_STATUS_TRANSITION');
    const issue = this.store.getIssue(input.projectRefId, input.reviewItemId, input.versionId, input.issueId);
    invariant(issue.status === 'unresolved', '已解决意见需要重新打开后才能编辑', 'ISSUE_RESOLVED_READONLY');

    const timestamp = nowIso();
    const revisionId = `rev_${createUuid()}`;
    const annotationSetId = input.shapes.length ? `aset_${createUuid()}` : undefined;
    const revision: ReviewIssueRevision = {
      revisionId,
      projectRefId: input.projectRefId,
      reviewItemId: input.reviewItemId,
      versionId: input.versionId,
      issueId: input.issueId,
      revisionNo: issue.revisions.length + 1,
      content: input.body.trim(),
      annotationSetId,
      timestampMs: input.timestampMs,
      frameNumber: input.frameNumber,
      createdAt: timestamp,
    };
    const annotationSet = annotationSetId
      ? buildAnnotationSet({ ...input, annotationSetId, revisionId, createdAt: timestamp })
      : null;
    const next: ReviewIssue = {
      ...issue,
      currentRevisionId: revisionId,
      timestampMs: input.timestampMs,
      frameNumber: input.frameNumber,
      lockVersion: issue.lockVersion + 1,
      body: revision.content,
      annotationSetId,
      currentRevision: revision,
      currentAnnotationSet: annotationSet,
      revisions: [...issue.revisions, revision],
      updatedAt: timestamp,
    };
    this.store.issues.set(next.issueId, next);
    this.store.emitChange();
    return cloneIssue(next);
  };

  readonly replyToIssue = async (input: {
    projectRefId: ProjectRefId;
    reviewItemId: ReviewItemId;
    versionId: VersionId;
    issueId: IssueId;
    body: string;
  }): Promise<ReviewIssue> => {
    invariant(input.body.trim().length > 0, '回复不能为空', 'REPLY_BODY_REQUIRED');
    this.store.assertProjectWritable(input.projectRefId);
    const item = this.store.getItem(input.projectRefId, input.reviewItemId);
    invariant(item.currentVersionId === input.versionId, '历史版本只读', 'HISTORICAL_VERSION_READONLY');
    invariant(item.status !== 'finalized', '已定稿后不能回复', 'FINALIZED_READONLY');
    const issue = this.store.getIssue(input.projectRefId, input.reviewItemId, input.versionId, input.issueId);
    invariant(item.status === 'in_review', '当前版本意见已只读', 'INVALID_STATUS_TRANSITION');
    const next: ReviewIssue = {
      ...issue,
      lockVersion: issue.lockVersion + 1,
      replies: [
        ...issue.replies,
        {
          messageId: `msg_${createUuid()}`,
          projectRefId: input.projectRefId,
          reviewItemId: input.reviewItemId,
          versionId: input.versionId,
          issueId: input.issueId,
          body: input.body.trim(),
          createdAt: nowIso(),
        },
      ],
      updatedAt: nowIso(),
    };
    this.store.issues.set(next.issueId, next);
    this.store.emitChange();
    return cloneIssue(next);
  };

  readonly setIssueStatus = async (input: {
    projectRefId: ProjectRefId;
    reviewItemId: ReviewItemId;
    versionId: VersionId;
    issueId: IssueId;
    status: 'unresolved' | 'resolved';
  }): Promise<ReviewIssue> => {
    this.store.assertProjectWritable(input.projectRefId);
    const item = this.store.getItem(input.projectRefId, input.reviewItemId);
    this.store.getVersion(input.projectRefId, input.reviewItemId, input.versionId);
    invariant(item.status !== 'finalized', '已定稿后不能处理意见', 'FINALIZED_READONLY');
    invariant(item.currentVersionId === input.versionId, '只能处理当前版本意见', 'HISTORICAL_VERSION_READONLY');
    const issue = this.store.getIssue(input.projectRefId, input.reviewItemId, input.versionId, input.issueId);
    invariant(item.status === 'in_review', '当前版本意见已只读', 'INVALID_STATUS_TRANSITION');
    const next: ReviewIssue = {
      ...issue,
      status: input.status,
      lockVersion: issue.lockVersion + 1,
      updatedAt: nowIso(),
    };
    this.store.issues.set(next.issueId, next);
    this.store.emitChange();
    return cloneIssue(next);
  };

  readonly deleteIssue = async (input: {
    projectRefId: ProjectRefId;
    reviewItemId: ReviewItemId;
    versionId: VersionId;
    issueId: IssueId;
  }): Promise<ReviewIssue> => {
    this.store.assertProjectWritable(input.projectRefId);
    const item = this.store.getItem(input.projectRefId, input.reviewItemId);
    this.store.getVersion(input.projectRefId, input.reviewItemId, input.versionId);
    invariant(item.status !== 'finalized', '已定稿后不能删除意见', 'FINALIZED_READONLY');
    invariant(item.currentVersionId === input.versionId, '历史版本只读', 'HISTORICAL_VERSION_READONLY');
    const issue = this.store.getIssue(input.projectRefId, input.reviewItemId, input.versionId, input.issueId, {
      includeDeleted: true,
    });
    invariant(item.status === 'in_review', '当前版本意见已只读', 'INVALID_STATUS_TRANSITION');
    invariant(!issue.deletedAt, '意见已删除', 'RESOURCE_STATE_CONFLICT');
    const next: ReviewIssue = {
      ...issue,
      deletedAt: nowIso(),
      lockVersion: issue.lockVersion + 1,
      updatedAt: nowIso(),
    };
    this.store.issues.set(next.issueId, next);
    this.store.emitChange();
    return cloneIssue(next);
  };
}
