import { afterEach, describe, expect, it, vi } from 'vitest';
import { FinalCutReviewClient, type ProjectDTO, type ResponsibleEditorDTO } from '../contracts-generated/backend-contract';
import type { ExecutionContext, Project, ReviewFrameRate } from '../contracts/types';
import type { ReviewProjectSummary } from '../ports';
import { HttpReviewProjects } from './http-review-projects';
import type { HttpReviewQueries } from './http-review-queries';
import { HttpReviewTransport } from './http-review-transport';

const projectDto: ProjectDTO = {
  project_ref_id: 'project-task5',
  project_code: 'TASK5',
  project_name: 'Task 5 project',
  description: '',
  source: 'local',
  lifecycle_status: 'active',
  completion_status: 'in_progress',
  fps_num: null,
  fps_den: null,
  lock_version: 7,
  created_at: '2026-08-25T00:00:00.000Z',
  updated_at: '2026-08-25T00:00:00.000Z',
};

const editorDto: ResponsibleEditorDTO = {
  id: 'editor-task5',
  display_name: '剪辑师甲',
  lock_version: 3,
  created_at: projectDto.created_at,
  updated_at: projectDto.updated_at,
};

type Task5ProjectApi = {
  listResponsibleEditors(): Promise<Array<{ responsibleEditorId: string; displayName: string; lockVersion: number }>>;
  createResponsibleEditor(input: { displayName: string }, context: ExecutionContext): Promise<{ responsibleEditorId: string; displayName: string }>;
  renameResponsibleEditor(input: { responsibleEditorId: string; displayName: string; lockVersion: number }, context: ExecutionContext): Promise<{ responsibleEditorId: string; displayName: string }>;
  lockProjectFrameRate(input: { projectRefId: string; frameRate: ReviewFrameRate }, context: ExecutionContext): Promise<Project>;
  finalizeProject(input: { projectRefId: string; confirmed: true }, context: ExecutionContext): Promise<ReviewProjectSummary>;
  revokeProjectFinalization(input: { projectRefId: string; confirmed: true }, context: ExecutionContext): Promise<ReviewProjectSummary>;
};

describe('HTTP Task 1-4 frontend contract wiring', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('maps the authoritative non-paginated editor envelope without starting a pagination snapshot', async () => {
    const baseUrl = 'https://review.example';
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      expect(String(input)).toBe(`${baseUrl}/api/v1/final-cut-review/responsible-editors`);
      return new Response(JSON.stringify({
        data: [editorDto],
        meta: { request_id: 'req-editor-list', contract_version: '1.0' },
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
    vi.stubGlobal('fetch', fetchMock);
    const transport = new HttpReviewTransport(
      'edit',
      new FinalCutReviewClient(baseUrl),
      baseUrl,
    );
    const api = new HttpReviewProjects(transport, {} as HttpReviewQueries);

    await expect(api.listResponsibleEditors()).resolves.toEqual([
      expect.objectContaining({
        responsibleEditorId: 'editor-task5',
        displayName: '剪辑师甲',
        lockVersion: 3,
      }),
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('maps editor/fps data and sends one atomic command for each project-level action', async () => {
    const summaryDto = { project: { ...projectDto, fps_num: 25, fps_den: 1, lock_version: 8 }, items: [] };
    const command = vi.fn(async (_path: string, commandType: string) => {
      if (commandType === 'CreateResponsibleEditor') return editorDto;
      if (commandType === 'RenameResponsibleEditor') return { ...editorDto, display_name: '剪辑师乙', lock_version: 4 };
      if (commandType === 'LockProjectFrameRate') return { ...projectDto, fps_num: 25, fps_den: 1, lock_version: 8 };
      if (commandType === 'FinalizeProject' || commandType === 'RevokeProjectFinalization') return summaryDto;
      throw new Error(`unexpected command ${commandType}`);
    });
    const transport = {
      assertWriteContext: vi.fn(),
      requestJson: vi.fn().mockResolvedValue([editorDto]),
      projectForWrite: vi.fn().mockResolvedValue(projectDto),
      command,
      baseUrl: 'https://review.example',
    } as unknown as HttpReviewTransport;
    const api = new HttpReviewProjects(transport, {} as HttpReviewQueries) as unknown as Task5ProjectApi;
    const editContext = { entryMode: 'edit' } as ExecutionContext;
    const reviewContext = { entryMode: 'review' } as ExecutionContext;

    expect(await api.listResponsibleEditors()).toEqual([
      expect.objectContaining({ responsibleEditorId: editorDto.id, displayName: '剪辑师甲', lockVersion: 3 }),
    ]);
    expect(await api.createResponsibleEditor({ displayName: '剪辑师甲' }, editContext)).toMatchObject({
      responsibleEditorId: editorDto.id,
      displayName: '剪辑师甲',
    });
    expect(await api.renameResponsibleEditor({ responsibleEditorId: editorDto.id, displayName: '剪辑师乙', lockVersion: 3 }, editContext)).toMatchObject({
      responsibleEditorId: editorDto.id,
      displayName: '剪辑师乙',
    });
    expect(await api.lockProjectFrameRate({ projectRefId: projectDto.project_ref_id, frameRate: { fpsNum: 25, fpsDen: 1 } }, editContext)).toMatchObject({
      frameRate: { fpsNum: 25, fpsDen: 1 },
    });
    await api.finalizeProject({ projectRefId: projectDto.project_ref_id, confirmed: true }, reviewContext);
    await api.revokeProjectFinalization({ projectRefId: projectDto.project_ref_id, confirmed: true }, reviewContext);

    expect(command.mock.calls.map(([, commandType]) => commandType)).toEqual([
      'CreateResponsibleEditor',
      'RenameResponsibleEditor',
      'LockProjectFrameRate',
      'FinalizeProject',
      'RevokeProjectFinalization',
    ]);
    expect(command).toHaveBeenCalledTimes(5);
  });
});
