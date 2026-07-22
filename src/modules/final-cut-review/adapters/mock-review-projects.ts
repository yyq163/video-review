import type { ReviewApiPort } from '../ports';
import type { MockReviewContext } from './mock-review-context';

type ProjectApi = Pick<
  ReviewApiPort,
  | 'listProjects'
  | 'getProjectDetail'
  | 'getWorkspace'
  | 'createProject'
  | 'updateProject'
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

  readonly getProjectDetail: ReviewApiPort['getProjectDetail'] = async (projectRefId, options) => {
    await this.context.ready();
    this.context.throwIfAborted(options?.signal);
    return this.context.repository.getProjectDetail(projectRefId);
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
