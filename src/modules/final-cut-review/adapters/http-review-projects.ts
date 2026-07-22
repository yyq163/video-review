import type { ProjectDTO, ReviewItemDTO, ReviewVersionDTO } from '../contracts-generated/backend-contract';
import type { FinalizationRecord, ProjectDetail } from '../contracts/types';
import type { ReviewApiPort } from '../ports';
import {
  itemFromDto,
  projectFromDto,
  versionFromDto,
} from './http-review-project-mappers';
import type { HttpReviewQueries } from './http-review-queries';
import type { HttpReviewTransport } from './http-review-transport';

type ProjectApi = Pick<
  ReviewApiPort,
  | 'listProjects'
  | 'getProjectDetail'
  | 'getWorkspace'
  | 'createProject'
  | 'updateProject'
  | 'updateReviewItem'
  | 'archiveProject'
  | 'restoreProject'
  | 'deleteProject'
>;

function editableItemFromDto(dto: ReviewItemDTO) {
  return Object.assign(itemFromDto(dto), { itemCode: dto.item_code });
}

export class HttpReviewProjects implements ProjectApi {
  constructor(
    private readonly transport: HttpReviewTransport,
    private readonly queries: HttpReviewQueries,
  ) {}

  readonly listProjects: ReviewApiPort['listProjects'] = async (options) => {
    const projects = await this.transport.requestList<ProjectDTO[]>(
      '/api/v1/final-cut-review/projects',
      options,
    );
    return projects.map(projectFromDto);
  };

  readonly getProjectDetail: ReviewApiPort['getProjectDetail'] = async (projectRefId, options) => {
    const project = await this.transport.client.getProject(projectRefId, this.transport.queryInit(options));
    const items = await this.transport.requestList<ReviewItemDTO[]>(
      `/api/v1/final-cut-review/projects/${projectRefId}/items`,
      options,
    );
    const versionsByItem: ProjectDetail['versionsByItem'] = {};
    const issuesByVersion: ProjectDetail['issuesByVersion'] = {};
    const finalizations: FinalizationRecord[] = [];

    for (const item of items) {
      const versions = await this.transport.requestList<ReviewVersionDTO[]>(
        `/api/v1/final-cut-review/projects/${projectRefId}/items/${item.id}/versions`,
        options,
      );
      versionsByItem[item.id] = versions.map((version) =>
        versionFromDto(version, this.transport.baseUrl, item),
      );
      for (const version of versions) {
        issuesByVersion[version.id] = await this.queries.issuesForVersion(
          projectRefId,
          item.id,
          version.id,
          options,
        );
      }
      const finalization = await this.queries.optionalFinalization(projectRefId, item.id, options);
      if (finalization) {
        finalizations.push(finalization);
      }
    }

    return {
      project: projectFromDto(project),
      items: items.map(editableItemFromDto),
      versionsByItem,
      issuesByVersion,
      finalizations,
    };
  };

  readonly getWorkspace: ReviewApiPort['getWorkspace'] = async (params, options) => {
    const [projectDto, itemDto, versionDtos] = await Promise.all([
      this.transport.client.getProject(params.projectRefId, this.transport.queryInit(options)),
      this.transport.requestJson<ReviewItemDTO>(
        `/api/v1/final-cut-review/projects/${params.projectRefId}/items/${params.reviewItemId}`,
        { signal: options?.signal },
      ),
      this.transport.requestList<ReviewVersionDTO[]>(
        `/api/v1/final-cut-review/projects/${params.projectRefId}/items/${params.reviewItemId}/versions`,
        options,
      ),
    ]);
    const versionId = params.versionId ?? itemDto.current_version_id;
    const currentVersionDto =
      versionDtos.find((version) => version.id === versionId) ??
      (await this.transport.requestJson<ReviewVersionDTO>(
        `/api/v1/final-cut-review/projects/${params.projectRefId}/items/${params.reviewItemId}/versions/${versionId}`,
        { signal: options?.signal },
      ));
    const issuesByVersion = await Promise.all(
      versionDtos.map(async (version) => ({
        version,
        issues: await this.queries.issuesForVersion(
          params.projectRefId,
          params.reviewItemId,
          version.id,
          options,
        ),
      })),
    );
    const currentIssues = issuesByVersion.find((entry) => entry.version.id === versionId)?.issues ?? [];
    const historicalIssues = issuesByVersion
      .filter((entry) => entry.version.id !== versionId)
      .flatMap((entry) => entry.issues);
    const activeFinalization = await this.queries.optionalFinalization(
      params.projectRefId,
      params.reviewItemId,
      options,
    );

    return {
      project: projectFromDto(projectDto),
      item: editableItemFromDto(itemDto),
      versions: versionDtos.map((version) => versionFromDto(version, this.transport.baseUrl, itemDto)),
      currentVersion: versionFromDto(currentVersionDto, this.transport.baseUrl, itemDto),
      currentIssues,
      historicalIssues,
      activeFinalization,
    };
  };

  readonly createProject: ReviewApiPort['createProject'] = async (input, context) => {
    this.transport.assertWriteContext(context, ['edit']);
    const project = await this.transport.command<
      ProjectDTO,
      { project_code: string; project_name: string; description?: string }
    >(
      '/api/v1/final-cut-review/edit/projects',
      'CreateProject',
      {
        project_code: input.code,
        project_name: input.name,
        description: input.description || undefined,
      },
      context,
      undefined,
      { idempotent: true },
    );
    return projectFromDto(project);
  };

  readonly updateProject: ReviewApiPort['updateProject'] = async (input, context) => {
    this.transport.assertWriteContext(context, ['edit']);
    const current = await this.transport.projectForWrite(input.projectRefId);
    const project = await this.transport.command<
      ProjectDTO,
      { project_ref_id: string; project_name: string; description: string }
    >(
      `/api/v1/final-cut-review/edit/projects/${input.projectRefId}`,
      'UpdateProject',
      {
        project_ref_id: input.projectRefId,
        project_name: input.name,
        description: input.description,
      },
      context,
      current.lock_version,
      { method: 'PATCH' },
    );
    return projectFromDto(project);
  };

  readonly updateReviewItem: ReviewApiPort['updateReviewItem'] = async (input, context) => {
    this.transport.assertWriteContext(context, ['edit']);
    const current = await this.transport.requestJson<ReviewItemDTO>(
      `/api/v1/final-cut-review/projects/${input.projectRefId}/items/${input.reviewItemId}`,
    );
    const item = await this.transport.command<
      ReviewItemDTO,
      { project_ref_id: string; review_item_id: string; title: string; episode_no?: number }
    >(
      `/api/v1/final-cut-review/edit/projects/${input.projectRefId}/items/${input.reviewItemId}`,
      'UpdateReviewItem',
      {
        project_ref_id: input.projectRefId,
        review_item_id: input.reviewItemId,
        title: input.title,
        ...(/^\d+$/.test(input.episode.trim()) ? { episode_no: Number(input.episode) } : {}),
      },
      context,
      current.lock_version,
      { method: 'PATCH' },
    );
    return editableItemFromDto(item);
  };

  readonly archiveProject: ReviewApiPort['archiveProject'] = async (input, context) => {
    this.transport.assertWriteContext(context, ['review']);
    const current = await this.transport.client.getProject(input.projectRefId, this.transport.queryInit());
    const project = await this.transport.command<ProjectDTO, { project_ref_id: string }>(
      `/api/v1/final-cut-review/review/projects/${input.projectRefId}/archive`,
      'ArchiveProject',
      { project_ref_id: input.projectRefId },
      context,
      current.lock_version,
    );
    return projectFromDto(project);
  };

  readonly restoreProject: ReviewApiPort['restoreProject'] = async (input, context) => {
    this.transport.assertWriteContext(context, ['review']);
    const current = await this.transport.client.getProject(input.projectRefId, this.transport.queryInit());
    const project = await this.transport.command<ProjectDTO, { project_ref_id: string }>(
      `/api/v1/final-cut-review/review/projects/${input.projectRefId}/restore`,
      'RestoreProject',
      { project_ref_id: input.projectRefId },
      context,
      current.lock_version,
    );
    return projectFromDto(project);
  };

  readonly deleteProject: ReviewApiPort['deleteProject'] = async (input, context) => {
    this.transport.assertWriteContext(context, ['review']);
    const current = await this.transport.client.getProject(input.projectRefId, this.transport.queryInit());
    const project = await this.transport.command<ProjectDTO, { project_ref_id: string; confirmed: true }>(
      `/api/v1/final-cut-review/review/projects/${input.projectRefId}/soft-delete`,
      'SoftDeleteProject',
      { project_ref_id: input.projectRefId, confirmed: input.confirmed },
      context,
      current.lock_version,
    );
    return projectFromDto(project);
  };
}
