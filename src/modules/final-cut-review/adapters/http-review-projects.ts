import {
  FinalCutReviewHttpError,
  type ProjectDTO,
  type ProjectDeletionResultDTO,
  type ProjectSummaryDTO,
  type ReviewItemDTO,
  type ReviewVersionDTO,
  type ResponsibleEditorDTO,
} from '../contracts-generated/backend-contract';
import type { ReviewApiPort, ReviewProjectSummary, ReviewProjectSummaryItem } from '../ports';
import { settleWithConcurrencyLimit } from '../core/bounded-concurrency';
import {
  itemFromDto,
  projectFromDto,
  responsibleEditorFromDto,
  versionFromDto,
} from './http-review-project-mappers';
import type { HttpReviewQueries } from './http-review-queries';
import { randomId, type HttpReviewTransport } from './http-review-transport';

const MAX_CONCURRENT_VERSION_ISSUE_QUERIES = 4;
const DELETE_PROJECT_OPERATION_KEY_PREFIX = 'fj-final-cut-review:delete-project-operation:';

interface PendingDeleteProjectOperation {
  commandId: string;
  lockVersion: number;
}

export class DeleteProjectResultUncertainError extends Error {
  readonly cause: unknown;

  constructor(cause: unknown) {
    super('项目删除结果不确定，必须先刷新项目列表核对，不能直接重试。');
    this.name = 'DeleteProjectResultUncertainError';
    this.cause = cause;
  }
}

export class DeleteProjectProtectionUnavailableError extends Error {
  constructor() {
    super('当前浏览器无法持久保存项目删除幂等身份，已阻止删除。');
    this.name = 'DeleteProjectProtectionUnavailableError';
  }
}

function projectDeleteStorage(): Storage | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

function projectDeleteKey(projectRefId: string): string {
  return `${DELETE_PROJECT_OPERATION_KEY_PREFIX}${encodeURIComponent(projectRefId)}`;
}

function readProjectDeleteOperation(
  projectRefId: string,
): { state: 'available'; operation?: PendingDeleteProjectOperation } | { state: 'unavailable' } {
  const storage = projectDeleteStorage();
  if (!storage) return { state: 'unavailable' };
  try {
    const raw = storage.getItem(projectDeleteKey(projectRefId));
    if (!raw) return { state: 'available' };
    const parsed = JSON.parse(raw) as Partial<PendingDeleteProjectOperation>;
    if (
      typeof parsed.commandId !== 'string'
      || !parsed.commandId.startsWith('DeleteProject_')
      || typeof parsed.lockVersion !== 'number'
      || !Number.isSafeInteger(parsed.lockVersion)
      || parsed.lockVersion < 1
    ) {
      return { state: 'unavailable' };
    }
    return {
      state: 'available',
      operation: { commandId: parsed.commandId, lockVersion: parsed.lockVersion },
    };
  } catch {
    return { state: 'unavailable' };
  }
}

function writeProjectDeleteOperation(
  projectRefId: string,
  operation: PendingDeleteProjectOperation,
): boolean {
  const storage = projectDeleteStorage();
  if (!storage) return false;
  try {
    const key = projectDeleteKey(projectRefId);
    const value = JSON.stringify(operation);
    storage.setItem(key, value);
    return storage.getItem(key) === value;
  } catch {
    return false;
  }
}

function clearProjectDeleteOperation(projectRefId: string): void {
  try {
    projectDeleteStorage()?.removeItem(projectDeleteKey(projectRefId));
  } catch {
    // A completed operation must not be replaced with a storage cleanup error.
  }
}

type ProjectApi = Pick<
  ReviewApiPort,
  | 'listProjects'
  | 'listResponsibleEditors'
  | 'getProjectSummary'
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
  | 'updateReviewItem'
  | 'archiveProject'
  | 'restoreProject'
  | 'deleteProject'
  | 'finalizeProject'
  | 'revokeProjectFinalization'
>;

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
  dto: ProjectSummaryDTO['items'][number],
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
    episode: dto.episode_no?.toString() ?? '',
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
      width: dto.current_version.width,
      height: dto.current_version.height,
      fpsNum: dto.current_version.fps_num,
      fpsDen: dto.current_version.fps_den,
      responsibleEditorId: dto.current_version.responsible_editor_id,
      responsibleEditorNameSnapshot: dto.current_version.responsible_editor_name_snapshot,
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

function projectSummaryFromDto(dto: ProjectSummaryDTO, baseUrl: string): ReviewProjectSummary {
  const project = projectFromDto(dto.project);
  return {
    project,
    items: dto.items.map((item) => summaryItemFromDto(item, project, baseUrl)),
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

  readonly listResponsibleEditors: ReviewApiPort['listResponsibleEditors'] = async (options) =>
    (await this.transport.requestList<ResponsibleEditorDTO[]>(
      '/api/v1/final-cut-review/responsible-editors',
      options,
    )).map(responsibleEditorFromDto);

  readonly getProjectSummary: ReviewApiPort['getProjectSummary'] = async (projectRefId, options) => {
    const dto = await this.transport.requestJson<ProjectSummaryDTO>(
      `/api/v1/final-cut-review/projects/${projectRefId}/summary`,
      { signal: options?.signal },
    );
    return projectSummaryFromDto(dto, this.transport.baseUrl);
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
    const selectedVersionId = params.versionId ?? itemDto.current_version_id;
    const versionsById = new Map(versionDtos.map((version) => [version.id, version]));
    const missingVersionIds = [...new Set([selectedVersionId, itemDto.current_version_id])].filter(
      (versionId) => !versionsById.has(versionId),
    );
    const missingVersions = await Promise.all(
      missingVersionIds.map((versionId) =>
        this.transport.requestJson<ReviewVersionDTO>(
          `/api/v1/final-cut-review/projects/${params.projectRefId}/items/${params.reviewItemId}/versions/${versionId}`,
          { signal: options?.signal },
        ),
      ),
    );
    for (const version of missingVersions) versionsById.set(version.id, version);
    const allVersionDtos = [...versionsById.values()].sort(
      (left, right) => left.version_no - right.version_no || left.id.localeCompare(right.id),
    );
    const selectedVersionDto = versionsById.get(selectedVersionId);
    const authoritativeCurrentVersionDto = versionsById.get(itemDto.current_version_id);
    if (!selectedVersionDto || !authoritativeCurrentVersionDto) {
      throw new Error('工作台版本列表缺少当前或所选版本');
    }

    const issueResults = await settleWithConcurrencyLimit(
      allVersionDtos,
      MAX_CONCURRENT_VERSION_ISSUE_QUERIES,
      (version) =>
        this.queries.issuesForVersion(
          params.projectRefId,
          params.reviewItemId,
          version.id,
          options,
        ),
    );
    const issuesByVersion = new Map<string, Awaited<ReturnType<HttpReviewQueries['issuesForVersion']>>>();
    issueResults.forEach((result, index) => {
      if (result.status === 'rejected') throw result.reason;
      const versionId = allVersionDtos[index].id;
      if (
        result.value.some(
          (issue) =>
            issue.projectRefId !== params.projectRefId ||
            issue.reviewItemId !== params.reviewItemId ||
            issue.versionId !== versionId,
        )
      ) {
        throw new Error(`版本 ${versionId} 的意见列表包含越界数据`);
      }
      issuesByVersion.set(
        versionId,
        result.value.filter((issue) => !issue.deletedAt),
      );
    });
    const currentIssues = issuesByVersion.get(itemDto.current_version_id);
    if (!currentIssues) throw new Error('工作台意见列表缺少真实当前版本');
    const historicalIssues = allVersionDtos
      .filter((version) => version.id !== itemDto.current_version_id)
      .flatMap((version) => issuesByVersion.get(version.id) ?? []);
    const activeFinalization = await this.queries.optionalFinalization(
      params.projectRefId,
      params.reviewItemId,
      options,
    );

    return {
      project: projectFromDto(projectDto),
      item: editableItemFromDto(itemDto),
      versions: allVersionDtos.map((version) => versionFromDto(version, this.transport.baseUrl, itemDto)),
      currentVersion: versionFromDto(selectedVersionDto, this.transport.baseUrl, itemDto),
      currentIssues,
      historicalIssues,
      activeFinalization,
    };
  };

  readonly createProject: ReviewApiPort['createProject'] = async (input, context) => {
    this.transport.assertWriteContext(context, ['edit']);
    const frameRate = input.frameRate ?? { fpsNum: 25, fpsDen: 1 };
    const project = await this.transport.command<
      ProjectDTO,
      { project_code: string; project_name: string; description?: string; fps_num: number; fps_den: number }
    >(
      '/api/v1/final-cut-review/edit/projects',
      'CreateProject',
      {
        project_code: input.code,
        project_name: input.name,
        description: input.description || undefined,
        fps_num: frameRate.fpsNum,
        fps_den: frameRate.fpsDen,
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

  readonly lockProjectFrameRate: ReviewApiPort['lockProjectFrameRate'] = async (input, context) => {
    this.transport.assertWriteContext(context, ['edit']);
    const current = await this.transport.projectForWrite(input.projectRefId);
    const project = await this.transport.command<
      ProjectDTO,
      { project_ref_id: string; fps_num: number; fps_den: number }
    >(
      `/api/v1/final-cut-review/edit/projects/${input.projectRefId}/frame-rate/lock`,
      'LockProjectFrameRate',
      {
        project_ref_id: input.projectRefId,
        fps_num: input.frameRate.fpsNum,
        fps_den: input.frameRate.fpsDen,
      },
      context,
      current.lock_version,
      { idempotent: true },
    );
    return projectFromDto(project);
  };

  readonly createResponsibleEditor: ReviewApiPort['createResponsibleEditor'] = async (input, context) => {
    this.transport.assertWriteContext(context, ['edit']);
    const editor = await this.transport.command<ResponsibleEditorDTO, { display_name: string }>(
      '/api/v1/final-cut-review/edit/responsible-editors',
      'CreateResponsibleEditor',
      { display_name: input.displayName },
      context,
      undefined,
      { idempotent: true },
    );
    return responsibleEditorFromDto(editor);
  };

  readonly renameResponsibleEditor: ReviewApiPort['renameResponsibleEditor'] = async (input, context) => {
    this.transport.assertWriteContext(context, ['edit']);
    const editor = await this.transport.command<
      ResponsibleEditorDTO,
      { responsible_editor_id: string; display_name: string }
    >(
      `/api/v1/final-cut-review/edit/responsible-editors/${input.responsibleEditorId}`,
      'RenameResponsibleEditor',
      {
        responsible_editor_id: input.responsibleEditorId,
        display_name: input.displayName,
      },
      context,
      input.lockVersion,
      { method: 'PATCH' },
    );
    return responsibleEditorFromDto(editor);
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

  readonly finalizeProject: ReviewApiPort['finalizeProject'] = async (input, context) => {
    this.transport.assertWriteContext(context, ['review']);
    const current = await this.transport.projectForWrite(input.projectRefId);
    const summary = await this.transport.command<
      ProjectSummaryDTO,
      { project_ref_id: string; confirmed: true }
    >(
      `/api/v1/final-cut-review/review/projects/${input.projectRefId}/finalize`,
      'FinalizeProject',
      { project_ref_id: input.projectRefId, confirmed: input.confirmed },
      context,
      current.lock_version,
      { idempotent: true },
    );
    return projectSummaryFromDto(summary, this.transport.baseUrl);
  };

  readonly revokeProjectFinalization: ReviewApiPort['revokeProjectFinalization'] = async (input, context) => {
    this.transport.assertWriteContext(context, ['review']);
    const current = await this.transport.projectForWrite(input.projectRefId);
    const summary = await this.transport.command<
      ProjectSummaryDTO,
      { project_ref_id: string; confirmed: true }
    >(
      `/api/v1/final-cut-review/review/projects/${input.projectRefId}/finalization/revoke`,
      'RevokeProjectFinalization',
      { project_ref_id: input.projectRefId, confirmed: input.confirmed },
      context,
      current.lock_version,
      { idempotent: true },
    );
    return projectSummaryFromDto(summary, this.transport.baseUrl);
  };

  readonly deleteProject: ReviewApiPort['deleteProject'] = async (input, context) => {
    this.transport.assertWriteContext(context, ['review']);
    const current = await this.transport.client.getProject(input.projectRefId, this.transport.queryInit());
    const stored = readProjectDeleteOperation(input.projectRefId);
    if (stored.state === 'unavailable') {
      throw new DeleteProjectProtectionUnavailableError();
    }
    const operation = stored.operation ?? {
      commandId: randomId('DeleteProject'),
      lockVersion: current.lock_version,
    };
    if (!writeProjectDeleteOperation(input.projectRefId, operation)) {
      throw new DeleteProjectProtectionUnavailableError();
    }
    try {
      const result = await this.transport.command<
        ProjectDeletionResultDTO,
        { project_ref_id: string; confirmed: true }
      >(
        `/api/v1/final-cut-review/review/projects/${input.projectRefId}/delete`,
        'DeleteProject',
        { project_ref_id: input.projectRefId, confirmed: input.confirmed },
        context,
        operation.lockVersion,
        { idempotent: true, commandId: operation.commandId },
      );
      if (result.project_ref_id !== input.projectRefId || result.outcome !== 'deleted') {
        throw new DeleteProjectResultUncertainError(
          new Error('项目删除命令返回了无法核对的对象结果'),
        );
      }
      clearProjectDeleteOperation(input.projectRefId);
      return projectFromDto(current);
    } catch (error) {
      if (
        error instanceof FinalCutReviewHttpError
        && error.httpStatus < 500
        && error.code !== 'IDEMPOTENCY_CONFLICT'
      ) {
        clearProjectDeleteOperation(input.projectRefId);
        throw error;
      }
      try {
        await this.transport.client.getProject(input.projectRefId, this.transport.queryInit());
      } catch (reconciliationError) {
        if (
          reconciliationError instanceof FinalCutReviewHttpError
          && reconciliationError.httpStatus === 404
        ) {
          clearProjectDeleteOperation(input.projectRefId);
          return projectFromDto(current);
        }
        throw new DeleteProjectResultUncertainError(error);
      }
      throw new DeleteProjectResultUncertainError(error);
    }
  };
}
