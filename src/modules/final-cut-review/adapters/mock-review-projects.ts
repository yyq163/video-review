import type { ReviewApiPort } from '../ports';
import type { MockReviewContext } from './mock-review-context';

type ProjectApi = Pick<
  ReviewApiPort,
  | 'listProjects'
  | 'listResponsibleEditors'
  | 'getProjectSummary'
  | 'getTechnicalQcProgress'
  | 'rerunProjectTechnicalQc'
  | 'getReviewItem'
  | 'getVersion'
  | 'getVersionIssues'
  | 'getIssueDetail'
  | 'getWorkspace'
  | 'createProject'
  | 'updateProject'
  | 'lockProjectFrameRate'
  | 'createResponsibleEditor'
  | 'renameResponsibleEditor'
  | 'archiveProject'
  | 'restoreProject'
  | 'deleteProject'
>;

export class MockReviewProjects implements ProjectApi {
  constructor(private readonly context: MockReviewContext) {}

  readonly listProjects: ReviewApiPort['listProjects'] = async (options) => {
    await this.context.ready();
    this.context.throwIfAborted(options?.signal);
    return this.context.repository.listProjects();
  };

  readonly listResponsibleEditors: ReviewApiPort['listResponsibleEditors'] = async (options) => {
    await this.context.ready();
    this.context.throwIfAborted(options?.signal);
    return this.context.repository.listResponsibleEditors();
  };

  readonly getProjectSummary: ReviewApiPort['getProjectSummary'] = async (projectRefId, options) => {
    await this.context.ready();
    this.context.throwIfAborted(options?.signal);
    return this.context.repository.getProjectSummary(projectRefId);
  };

  readonly getTechnicalQcProgress: ReviewApiPort['getTechnicalQcProgress'] = async (projectRefId, options) => {
    await this.context.ready();
    this.context.throwIfAborted(options?.signal);
    return this.context.repository.getTechnicalQcProgress(projectRefId);
  };

  readonly rerunProjectTechnicalQc: ReviewApiPort['rerunProjectTechnicalQc'] = async (input, executionContext) => {
    await this.context.ready();
    this.context.assertContext(executionContext, 'review.technical_qc.rerun');
    const summary = await this.context.repository.getProjectSummary(input.projectRefId);
    return {
      projectRefId: input.projectRefId,
      status: 'queued',
      queuedItemCount: summary.items.length,
    };
  };

  readonly getReviewItem: ReviewApiPort['getReviewItem'] = async (input, options) => {
    await this.context.ready();
    this.context.throwIfAborted(options?.signal);
    return this.context.repository.getReviewItem(input);
  };

  readonly getVersion: ReviewApiPort['getVersion'] = async (input, options) => {
    await this.context.ready();
    this.context.throwIfAborted(options?.signal);
    return this.context.repository.getVersion(input);
  };

  readonly getVersionIssues: ReviewApiPort['getVersionIssues'] = async (input, options) => {
    await this.context.ready();
    this.context.throwIfAborted(options?.signal);
    return this.context.repository.getVersionIssues(input);
  };

  readonly getIssueDetail: ReviewApiPort['getIssueDetail'] = async (input, options) => {
    await this.context.ready();
    this.context.throwIfAborted(options?.signal);
    return this.context.repository.getIssueDetail(input);
  };

  readonly getWorkspace: ReviewApiPort['getWorkspace'] = async (input, options) => {
    await this.context.ready();
    this.context.throwIfAborted(options?.signal);
    return this.context.repository.getWorkspace(input);
  };

  readonly createProject: ReviewApiPort['createProject'] = async (input, executionContext) => {
    await this.context.ready();
    this.context.assertContext(executionContext, 'review.project.create');
    return this.context.repository.createProject(input);
  };

  readonly updateProject: ReviewApiPort['updateProject'] = async (input, executionContext) => {
    await this.context.ready();
    this.context.assertContext(executionContext, 'review.project.update');
    return this.context.repository.updateProject(input);
  };

  readonly lockProjectFrameRate: ReviewApiPort['lockProjectFrameRate'] = async (input, executionContext) => {
    await this.context.ready();
    this.context.assertContext(executionContext, 'review.project.update');
    return this.context.repository.lockProjectFrameRate(input);
  };

  readonly createResponsibleEditor: ReviewApiPort['createResponsibleEditor'] = async (input, executionContext) => {
    await this.context.ready();
    this.context.assertContext(executionContext, 'review.project.update');
    return this.context.repository.createResponsibleEditor(input);
  };

  readonly renameResponsibleEditor: ReviewApiPort['renameResponsibleEditor'] = async (input, executionContext) => {
    await this.context.ready();
    this.context.assertContext(executionContext, 'review.project.update');
    return this.context.repository.renameResponsibleEditor(input);
  };

  readonly archiveProject: ReviewApiPort['archiveProject'] = async (input, executionContext) => {
    await this.context.ready();
    this.context.assertContext(executionContext, 'review.project.archive');
    return this.context.repository.archiveProject(input);
  };

  readonly restoreProject: ReviewApiPort['restoreProject'] = async (input, executionContext) => {
    await this.context.ready();
    this.context.assertContext(executionContext, 'review.project.restore');
    return this.context.repository.restoreProject(input);
  };

  readonly deleteProject: ReviewApiPort['deleteProject'] = async (input, executionContext) => {
    await this.context.ready();
    this.context.assertContext(executionContext, 'review.project.delete');
    return this.context.repository.deleteProject(input);
  };
}
