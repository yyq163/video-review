import type { EntryMode } from '../contracts/types';
import type {
  EntryPolicyPort,
  FileStoragePort,
  FinalizedPackagePort,
  PrincipalAuthorizationPort,
  ReviewApiPort,
  ReviewHostBridge,
  WriteGuardPort,
} from '../ports';
import type { InMemoryReviewRepository } from './in-memory-review-repository';
import { MockReviewContext } from './mock-review-context';
import { MockReviewDownloads } from './mock-review-downloads';
import { MockReviewIssues } from './mock-review-issues';
import { MockReviewProjects } from './mock-review-projects';
import { MockReviewUploads } from './mock-review-uploads';
import { MockReviewWorkflow } from './mock-review-workflow';

export class MockReviewApiAdapter implements ReviewApiPort {
  readonly listProjects: ReviewApiPort['listProjects'];
  readonly getProjectDetail: ReviewApiPort['getProjectDetail'];
  readonly getWorkspace: ReviewApiPort['getWorkspace'];
  readonly createProject: ReviewApiPort['createProject'];
  readonly updateProject: ReviewApiPort['updateProject'];
  readonly archiveProject: ReviewApiPort['archiveProject'];
  readonly restoreProject: ReviewApiPort['restoreProject'];
  readonly deleteProject: ReviewApiPort['deleteProject'];
  readonly createReviewItemWithVersion: ReviewApiPort['createReviewItemWithVersion'];
  readonly updateReviewItem: ReviewApiPort['updateReviewItem'];
  readonly deleteReviewItem: ReviewApiPort['deleteReviewItem'];
  readonly appendVersion: ReviewApiPort['appendVersion'];
  readonly startReview: ReviewApiPort['startReview'];
  readonly createIssue: ReviewApiPort['createIssue'];
  readonly replyToIssue: ReviewApiPort['replyToIssue'];
  readonly editIssue: ReviewApiPort['editIssue'];
  readonly resolveIssue: ReviewApiPort['resolveIssue'];
  readonly reopenIssue: ReviewApiPort['reopenIssue'];
  readonly deleteIssue: ReviewApiPort['deleteIssue'];
  readonly requestChanges: ReviewApiPort['requestChanges'];
  readonly finalizeCurrentVersion: ReviewApiPort['finalizeCurrentVersion'];
  readonly downloadFinalizedOriginal: ReviewApiPort['downloadFinalizedOriginal'];
  readonly createProjectFinalizedPackage: ReviewApiPort['createProjectFinalizedPackage'];
  readonly downloadProjectFinalizedPackage: ReviewApiPort['downloadProjectFinalizedPackage'];

  constructor(
    public readonly mode: EntryMode,
    repository: InMemoryReviewRepository,
    fileStorage: FileStoragePort,
    packageAdapter: FinalizedPackagePort,
    writeGuard: WriteGuardPort,
    authorization: PrincipalAuthorizationPort,
    public readonly entryPolicy: EntryPolicyPort,
    hostBridge: ReviewHostBridge,
    beforeOperation: (() => Promise<void>) | undefined = undefined,
  ) {
    const context = new MockReviewContext(
      mode,
      repository,
      fileStorage,
      packageAdapter,
      writeGuard,
      authorization,
      hostBridge,
      beforeOperation,
    );
    const projects = new MockReviewProjects(context);
    const uploads = new MockReviewUploads(context);
    const issues = new MockReviewIssues(context);
    const workflow = new MockReviewWorkflow(context);
    const downloads = new MockReviewDownloads(context);

    this.listProjects = projects.listProjects;
    this.getProjectDetail = projects.getProjectDetail;
    this.getWorkspace = projects.getWorkspace;
    this.createProject = projects.createProject;
    this.updateProject = projects.updateProject;
    this.archiveProject = projects.archiveProject;
    this.restoreProject = projects.restoreProject;
    this.deleteProject = projects.deleteProject;
    this.createReviewItemWithVersion = uploads.createReviewItemWithVersion;
    this.updateReviewItem = uploads.updateReviewItem;
    this.deleteReviewItem = uploads.deleteReviewItem;
    this.appendVersion = uploads.appendVersion;
    this.startReview = workflow.startReview;
    this.createIssue = issues.createIssue;
    this.replyToIssue = issues.replyToIssue;
    this.editIssue = issues.editIssue;
    this.resolveIssue = issues.resolveIssue;
    this.reopenIssue = issues.reopenIssue;
    this.deleteIssue = issues.deleteIssue;
    this.requestChanges = workflow.requestChanges;
    this.finalizeCurrentVersion = workflow.finalizeCurrentVersion;
    this.downloadFinalizedOriginal = downloads.downloadFinalizedOriginal;
    this.createProjectFinalizedPackage = downloads.createProjectFinalizedPackage;
    this.downloadProjectFinalizedPackage = downloads.downloadProjectFinalizedPackage;
  }
}
