import type { FinalCutReviewClient, UploadSessionDTO } from '../contracts-generated/backend-contract';
import type { EntryMode, ExecutionContext, UploadProgress } from '../contracts/types';
import type { EntryPolicyPort, ReviewApiPort, ReviewHostBridge } from '../ports';
import { HttpReviewDownloads } from './http-review-downloads';
import { HttpReviewIssues } from './http-review-issues';
import { HttpReviewProjects } from './http-review-projects';
import { HttpReviewQueries } from './http-review-queries';
import { HttpReviewTransport } from './http-review-transport';
import { HttpReviewUploads } from './http-review-uploads';
import { HttpReviewWorkflow } from './http-review-workflow';

export class HttpReviewApiAdapter implements ReviewApiPort {
  private readonly uploads: HttpReviewUploads;
  readonly listProjects: ReviewApiPort['listProjects'];
  readonly listResponsibleEditors: ReviewApiPort['listResponsibleEditors'];
  readonly getProjectSummary: ReviewApiPort['getProjectSummary'];
  readonly getReviewItem: ReviewApiPort['getReviewItem'];
  readonly getVersion: ReviewApiPort['getVersion'];
  readonly getVersionIssues: ReviewApiPort['getVersionIssues'];
  readonly getIssueDetail: ReviewApiPort['getIssueDetail'];
  readonly getWorkspace: ReviewApiPort['getWorkspace'];
  readonly createProject: ReviewApiPort['createProject'];
  readonly updateProject: ReviewApiPort['updateProject'];
  readonly lockProjectFrameRate: ReviewApiPort['lockProjectFrameRate'];
  readonly createResponsibleEditor: ReviewApiPort['createResponsibleEditor'];
  readonly renameResponsibleEditor: ReviewApiPort['renameResponsibleEditor'];
  readonly archiveProject: ReviewApiPort['archiveProject'];
  readonly restoreProject: ReviewApiPort['restoreProject'];
  readonly deleteProject: ReviewApiPort['deleteProject'];
  readonly finalizeProject: ReviewApiPort['finalizeProject'];
  readonly revokeProjectFinalization: ReviewApiPort['revokeProjectFinalization'];
  readonly createReviewItemWithVersion: ReviewApiPort['createReviewItemWithVersion'];
  readonly updateReviewItem: ReviewApiPort['updateReviewItem'];
  readonly deleteReviewItem: ReviewApiPort['deleteReviewItem'];
  readonly deleteReviewItemsBatch: ReviewApiPort['deleteReviewItemsBatch'];
  readonly appendVersion: ReviewApiPort['appendVersion'];
  readonly startReview: ReviewApiPort['startReview'];
  readonly createIssue: ReviewApiPort['createIssue'];
  readonly replyToIssue: ReviewApiPort['replyToIssue'];
  readonly editIssue: ReviewApiPort['editIssue'];
  readonly resolveIssue: ReviewApiPort['resolveIssue'];
  readonly reopenIssue: ReviewApiPort['reopenIssue'];
  readonly deleteIssue: ReviewApiPort['deleteIssue'];
  readonly finalizeCurrentVersion: ReviewApiPort['finalizeCurrentVersion'];
  readonly revokeFinalization: ReviewApiPort['revokeFinalization'];
  readonly retryFinalizationCleanup: ReviewApiPort['retryFinalizationCleanup'];
  readonly downloadFinalizedOriginal: ReviewApiPort['downloadFinalizedOriginal'];
  readonly createProjectFinalizedPackage: ReviewApiPort['createProjectFinalizedPackage'];
  readonly downloadProjectFinalizedPackage: ReviewApiPort['downloadProjectFinalizedPackage'];

  constructor(
    public readonly mode: EntryMode,
    client: FinalCutReviewClient,
    public readonly entryPolicy: EntryPolicyPort,
    baseUrl: string,
    hostBridge: ReviewHostBridge,
  ) {
    const transport = new HttpReviewTransport(mode, client, baseUrl);
    const queries = new HttpReviewQueries(transport);
    const projects = new HttpReviewProjects(transport, queries);
    const uploads = new HttpReviewUploads(
      transport,
      (file, context, onProgress) => this.uploadFile(file, context, onProgress),
    );
    this.uploads = uploads;
    const issues = new HttpReviewIssues(transport, queries);
    const workflow = new HttpReviewWorkflow(transport);
    const downloads = new HttpReviewDownloads(transport, hostBridge);

    this.listProjects = projects.listProjects;
    this.listResponsibleEditors = projects.listResponsibleEditors;
    this.getProjectSummary = projects.getProjectSummary;
    this.getReviewItem = projects.getReviewItem;
    this.getVersion = projects.getVersion;
    this.getVersionIssues = projects.getVersionIssues;
    this.getIssueDetail = projects.getIssueDetail;
    this.getWorkspace = projects.getWorkspace;
    this.createProject = projects.createProject;
    this.updateProject = projects.updateProject;
    this.lockProjectFrameRate = projects.lockProjectFrameRate;
    this.createResponsibleEditor = projects.createResponsibleEditor;
    this.renameResponsibleEditor = projects.renameResponsibleEditor;
    this.archiveProject = projects.archiveProject;
    this.restoreProject = projects.restoreProject;
    this.deleteProject = projects.deleteProject;
    this.finalizeProject = projects.finalizeProject;
    this.revokeProjectFinalization = projects.revokeProjectFinalization;
    this.createReviewItemWithVersion = uploads.createReviewItemWithVersion;
    this.updateReviewItem = projects.updateReviewItem;
    this.deleteReviewItem = uploads.deleteReviewItem;
    this.deleteReviewItemsBatch = uploads.deleteReviewItemsBatch;
    this.appendVersion = uploads.appendVersion;
    this.startReview = workflow.startReview;
    this.createIssue = issues.createIssue;
    this.replyToIssue = issues.replyToIssue;
    this.editIssue = issues.editIssue;
    this.resolveIssue = issues.resolveIssue;
    this.reopenIssue = issues.reopenIssue;
    this.deleteIssue = issues.deleteIssue;
    this.finalizeCurrentVersion = workflow.finalizeCurrentVersion;
    this.revokeFinalization = workflow.revokeFinalization;
    this.retryFinalizationCleanup = workflow.retryFinalizationCleanup;
    this.downloadFinalizedOriginal = downloads.downloadFinalizedOriginal;
    this.createProjectFinalizedPackage = downloads.createProjectFinalizedPackage;
    this.downloadProjectFinalizedPackage = downloads.downloadProjectFinalizedPackage;
  }

  private uploadFile(
    file: File,
    context: ExecutionContext,
    onProgress?: (progress: UploadProgress) => void,
  ): Promise<UploadSessionDTO> {
    return this.uploads.uploadFile(file, context, onProgress);
  }
}
