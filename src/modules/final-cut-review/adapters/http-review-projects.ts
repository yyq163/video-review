import type { ProjectDTO, ReviewItemDTO, ReviewVersionDTO } from '../contracts-generated/backend-contract';
import type { ReviewApiPort, ReviewProjectSummary, ReviewProjectSummaryItem } from '../ports';
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
  | 'getProjectSummary'
  | 'getReviewItem'
  | 'getVersion'
  | 'getVersionIssues'
  | 'getIssueDetail'
  | 'getWorkspace'
  | 'createProject'
  | 'updateProject'
  | 'updateReviewItem'
  | 'archiveProject'
  | 'restoreProject'
  | 'deleteProject'
>;

interface ReviewProjectSummaryDTO {
  project: ProjectDTO;
  items: ReviewProjectSummaryItemDTO[];
}

interface ReviewProjectSummaryItemDTO {
  id: string;
  project_ref_id: string;
  item_code: string;
  episode_no?: number | null;
  title: string;
  workflow_state: ReviewProjectSummaryItem['status'];
  lock_version: number;
  current_version_id: string;
  current_version: {
    id: string;
    version_no: number;
    version_label: string;
    duration_ms: number;
    file_size: number;
    playback_status: 'pending' | 'ready' | 'failed';
    playback_url: string | null;
    thumbnail_status: 'pending' | 'ready' | 'failed';
    thumbnail_url: string | null;
  };
  unresolved_current_version_count: number;
  finalization: {
    id: string;
    status: 'active' | 'revoked';
    revoked_at?: string | null;
  } | null;
  revocation_cleanup_status: 'none' | 'pending' | 'failed' | 'complete';
  bulk_delete: {
    eligible: boolean;
    locked: boolean;
    reason: string | null;
  };
}

function editableItemFromDto(dto: ReviewItemDTO) {
  return Object.assign(itemFromDto(dto), { itemCode: dto.item_code });
}

function protectedMediaUrl(
  baseUrl: string,
  url: string | null,
  ready: boolean,
  expectedPath: string,
  allowThumbnailRevision = false,
): string | null {
  if (!ready || url === null) return null;
  const revisionPrefix = `${expectedPath}?asset=`;
  const validRevision = allowThumbnailRevision
    && url.startsWith(revisionPrefix)
    && /^[A-Za-z0-9_-]{1,64}$/.test(url.slice(revisionPrefix.length));
  if (url !== expectedPath && !validRevision) return null;
  return `${baseUrl.replace(/\/+$/, '')}${url}`;
}

function summaryItemFromDto(
  dto: ReviewProjectSummaryItemDTO,
  project: ReviewProjectSummary['project'],
  baseUrl: string,
): ReviewProjectSummaryItem {
  const finalization = dto.finalization
    ? {
        id: dto.finalization.id,
        status: dto.finalization.status,
        revokedAt: dto.finalization.revoked_at ?? null,
      }
    : null;
  const playbackPath =
    `/api/v1/final-cut-review/projects/${dto.project_ref_id}/items/${dto.id}/versions/${dto.current_version.id}/stream`;
  const thumbnailPath =
    `/api/v1/final-cut-review/projects/${dto.project_ref_id}/items/${dto.id}/versions/${dto.current_version.id}/thumbnail`;
  const playbackUrl = protectedMediaUrl(
    baseUrl,
    dto.current_version.playback_url,
    dto.current_version.playback_status === 'ready',
    playbackPath,
  );
  const thumbnailUrl = protectedMediaUrl(
    baseUrl,
    dto.current_version.thumbnail_url,
    dto.current_version.thumbnail_status === 'ready',
    thumbnailPath,
    true,
  );
  return {
    reviewItemId: dto.id,
    projectRefId: dto.project_ref_id,
    itemCode: dto.item_code,
    title: dto.title,
    episode: dto.episode_no?.toString() ?? dto.item_code,
    currentVersionId: dto.current_version_id,
    activeFinalizationId:
      finalization?.status === 'active'
        ? finalization.id
        : null,
    status: dto.workflow_state,
    createdAt: project.createdAt,
    updatedAt: project.updatedAt,
    lockVersion: dto.lock_version,
    currentVersion: {
      id: dto.current_version.id,
      versionNo: dto.current_version.version_no,
      versionLabel: dto.current_version.version_label,
      durationMs: dto.current_version.duration_ms,
      fileSize: dto.current_version.file_size,
      playbackStatus:
        dto.current_version.playback_status === 'ready' && !playbackUrl
          ? 'failed'
          : dto.current_version.playback_status,
      playbackUrl,
      thumbnailStatus:
        dto.current_version.thumbnail_status === 'ready' && !thumbnailUrl
          ? 'failed'
          : dto.current_version.thumbnail_status,
      thumbnailUrl,
    },
    unresolvedCurrentVersionCount: dto.unresolved_current_version_count,
    finalization,
    revocationCleanupStatus: dto.revocation_cleanup_status,
    bulkDelete: { ...dto.bulk_delete },
  };
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

  readonly getProjectSummary: ReviewApiPort['getProjectSummary'] = async (projectRefId, options) => {
    const dto = await this.transport.requestJson<ReviewProjectSummaryDTO>(
      `/api/v1/final-cut-review/projects/${projectRefId}/summary`,
      { signal: options?.signal },
    );
    const project = projectFromDto(dto.project);
    return {
      project,
      items: dto.items.map((item) => summaryItemFromDto(item, project, this.transport.baseUrl)),
    };
  };

  readonly getReviewItem: ReviewApiPort['getReviewItem'] = async (params, options) =>
    editableItemFromDto(
      await this.transport.requestJson<ReviewItemDTO>(
        `/api/v1/final-cut-review/projects/${params.projectRefId}/items/${params.reviewItemId}`,
        { signal: options?.signal },
      ),
    );

  readonly getVersion: ReviewApiPort['getVersion'] = async (params, options) =>
    versionFromDto(
      await this.transport.requestJson<ReviewVersionDTO>(
        `/api/v1/final-cut-review/projects/${params.projectRefId}/items/${params.reviewItemId}/versions/${params.versionId}`,
        { signal: options?.signal },
      ),
      this.transport.baseUrl,
    );

  readonly getVersionIssues: ReviewApiPort['getVersionIssues'] = async (params, options) =>
    this.queries.issuesForVersion(
      params.projectRefId,
      params.reviewItemId,
      params.versionId,
      options,
    );

  readonly getIssueDetail: ReviewApiPort['getIssueDetail'] = async (params, options) =>
    this.queries.issueWithMessages(
      params.projectRefId,
      params.reviewItemId,
      params.versionId,
      params.issueId,
      undefined,
      options,
    );

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
    const currentIssues = await this.queries.issuesForVersion(
      params.projectRefId,
      params.reviewItemId,
      versionId,
      options,
    );
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
      historicalIssues: [],
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
