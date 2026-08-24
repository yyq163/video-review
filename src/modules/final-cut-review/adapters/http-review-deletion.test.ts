import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  FinalCutReviewHttpError,
  type ProjectDTO,
  type ReviewItemDTO,
} from '../contracts-generated/backend-contract';
import type { ExecutionContext } from '../contracts/types';
import { ReviewDomainError } from '../core/errors';
import { DeleteProjectResultUncertainError, HttpReviewProjects } from './http-review-projects';
import { HttpReviewTransport } from './http-review-transport';
import {
  DeleteReviewItemProtectionUnavailableError,
  DeleteReviewItemResultUncertainError,
  HttpReviewUploads,
} from './http-review-uploads';

afterEach(() => {
  window.sessionStorage.clear();
  vi.restoreAllMocks();
});

describe('durable HTTP deletion contracts', () => {
  it('sends DeleteProject once with a reload-stable command identity and current lock', async () => {
    const project = projectDto('prj_delete');
    const command = vi.fn().mockResolvedValue({
      project_ref_id: project.project_ref_id,
      outcome: 'deleted',
      error_code: null,
    });
    const transport = {
      assertWriteContext: vi.fn(),
      client: { getProject: vi.fn().mockResolvedValue(project) },
      queryInit: vi.fn().mockReturnValue({ credentials: 'include' }),
      command,
    } as unknown as HttpReviewTransport;
    const projects = new HttpReviewProjects(transport, {} as never);

    await expect(projects.deleteProject(
      { projectRefId: project.project_ref_id, confirmed: true },
      context('delete-project'),
    )).resolves.toMatchObject({ projectRefId: project.project_ref_id });

    expect(command).toHaveBeenCalledWith(
      `/api/v1/final-cut-review/review/projects/${project.project_ref_id}/delete`,
      'DeleteProject',
      { project_ref_id: project.project_ref_id, confirmed: true },
      expect.objectContaining({ requestId: 'delete-project' }),
      project.lock_version,
      expect.objectContaining({ idempotent: true, commandId: expect.stringContaining('DeleteProject_') }),
    );
  });

  it('reconciles a lost DeleteProject response only when the canonical project read is absent', async () => {
    const project = projectDto('prj_delete_lost');
    const notFound = new FinalCutReviewHttpError({
      code: 'RESOURCE_NOT_FOUND',
      message: '不存在',
      details: {},
      request_id: 'lost-project-response',
      timestamp: '2026-08-24T00:00:00Z',
      contract_version: '1.0',
      http_status: 404,
    }, 404);
    const getProject = vi.fn()
      .mockResolvedValueOnce(project)
      .mockRejectedValueOnce(notFound);
    const transport = {
      assertWriteContext: vi.fn(),
      client: { getProject },
      queryInit: vi.fn().mockReturnValue({ credentials: 'include' }),
      command: vi.fn().mockRejectedValue(new TypeError('response lost')),
    } as unknown as HttpReviewTransport;

    await expect(new HttpReviewProjects(transport, {} as never).deleteProject(
      { projectRefId: project.project_ref_id, confirmed: true },
      context('delete-project-lost'),
    )).resolves.toMatchObject({ projectRefId: project.project_ref_id });
    expect(getProject).toHaveBeenCalledTimes(2);
  });

  it('clears a determinate project failure and permits a corrected operation with a new command id', async () => {
    const project = projectDto('prj_delete_failed');
    const command = vi.fn()
      .mockResolvedValueOnce({
        project_ref_id: project.project_ref_id,
        outcome: 'failed',
        error_code: 'RESOURCE_STATE_CONFLICT',
      })
      .mockResolvedValueOnce({
        project_ref_id: project.project_ref_id,
        outcome: 'deleted',
        error_code: null,
      });
    const transport = {
      assertWriteContext: vi.fn(),
      client: { getProject: vi.fn().mockResolvedValue(project) },
      queryInit: vi.fn(),
      command,
    } as unknown as HttpReviewTransport;
    const input = { projectRefId: project.project_ref_id, confirmed: true as const };

    await expect(new HttpReviewProjects(transport, {} as never).deleteProject(
      input,
      context('project-failed'),
    )).rejects.toEqual(expect.objectContaining<Partial<ReviewDomainError>>({
      code: 'RESOURCE_STATE_CONFLICT',
    }));
    await expect(new HttpReviewProjects(transport, {} as never).deleteProject(
      input,
      context('project-corrected'),
    )).resolves.toMatchObject({ projectRefId: project.project_ref_id });

    expect(command.mock.calls[1]?.[5]?.commandId).not.toBe(command.mock.calls[0]?.[5]?.commandId);
  });

  it('retains a project unknown identity across adapter reloads', async () => {
    const project = projectDto('prj_delete_unknown');
    const command = vi.fn().mockResolvedValue({
      project_ref_id: project.project_ref_id,
      outcome: 'outcome_unknown',
      error_code: 'COMMIT_OUTCOME_UNKNOWN',
    });
    const transport = {
      assertWriteContext: vi.fn(),
      client: { getProject: vi.fn().mockResolvedValue(project) },
      queryInit: vi.fn(),
      command,
    } as unknown as HttpReviewTransport;
    const input = { projectRefId: project.project_ref_id, confirmed: true as const };

    await expect(new HttpReviewProjects(transport, {} as never).deleteProject(
      input,
      context('project-unknown'),
    )).rejects.toBeInstanceOf(DeleteProjectResultUncertainError);
    await expect(new HttpReviewProjects(transport, {} as never).deleteProject(
      input,
      context('project-unknown-reload'),
    )).rejects.toBeInstanceOf(DeleteProjectResultUncertainError);

    expect(command.mock.calls[1]?.[5]?.commandId).toBe(command.mock.calls[0]?.[5]?.commandId);
  });

  it('clears a determinate item failure and keeps an item unknown identity across reloads', async () => {
    const item = reviewItemDto('item_outcome_split');
    const input = {
      projectRefId: item.project_ref_id,
      reviewItemId: item.id,
      confirmed: true as const,
    };
    const failedThenDeleted = vi.fn()
      .mockResolvedValueOnce({
        project_ref_id: item.project_ref_id,
        review_item_id: item.id,
        outcome: 'failed',
        error_code: 'REVIEW_ITEM_DELETE_LOCKED',
      })
      .mockResolvedValueOnce({
        project_ref_id: item.project_ref_id,
        review_item_id: item.id,
        outcome: 'deleted',
        error_code: null,
      });
    const failedTransport = {
      assertWriteContext: vi.fn(),
      projectForWrite: vi.fn().mockResolvedValue(projectDto(item.project_ref_id)),
      itemForLock: vi.fn().mockResolvedValue(item),
      command: failedThenDeleted,
      baseUrl: 'https://review.example',
    } as unknown as HttpReviewTransport;

    await expect(new HttpReviewUploads(failedTransport).deleteReviewItem(
      input,
      context('item-failed', 'edit'),
    )).rejects.toEqual(expect.objectContaining<Partial<ReviewDomainError>>({
      code: 'REVIEW_ITEM_DELETE_LOCKED',
    }));
    await expect(new HttpReviewUploads(failedTransport).deleteReviewItem(
      input,
      context('item-corrected', 'edit'),
    )).resolves.toMatchObject({ reviewItemId: item.id });
    expect(failedThenDeleted.mock.calls[1]?.[5]?.commandId).not.toBe(
      failedThenDeleted.mock.calls[0]?.[5]?.commandId,
    );

    const unknownCommand = vi.fn().mockResolvedValue({
      project_ref_id: item.project_ref_id,
      review_item_id: item.id,
      outcome: 'outcome_unknown',
      error_code: 'COMMIT_OUTCOME_UNKNOWN',
    });
    const unknownTransport = {
      ...failedTransport,
      command: unknownCommand,
    } as unknown as HttpReviewTransport;
    await expect(new HttpReviewUploads(unknownTransport).deleteReviewItem(
      input,
      context('item-unknown', 'edit'),
    )).rejects.toBeInstanceOf(DeleteReviewItemResultUncertainError);
    await expect(new HttpReviewUploads(unknownTransport).deleteReviewItem(
      input,
      context('item-unknown-reload', 'edit'),
    )).rejects.toBeInstanceOf(DeleteReviewItemResultUncertainError);
    expect(unknownCommand.mock.calls[1]?.[5]?.commandId).toBe(unknownCommand.mock.calls[0]?.[5]?.commandId);
  });

  it('uses one backend batch command and preserves per-object outcomes', async () => {
    const first = reviewItemDto('item_delete_first');
    const second = reviewItemDto('item_delete_second');
    const command = vi.fn().mockResolvedValue({
      project_ref_id: first.project_ref_id,
      results: [
        {
          project_ref_id: first.project_ref_id,
          review_item_id: first.id,
          outcome: 'deleted',
          error_code: null,
        },
        {
          project_ref_id: first.project_ref_id,
          review_item_id: second.id,
          outcome: 'failed',
          error_code: 'RESOURCE_STATE_CONFLICT',
        },
      ],
    });
    const transport = {
      assertWriteContext: vi.fn(),
      projectForWrite: vi.fn().mockResolvedValue(projectDto(first.project_ref_id)),
      command,
      requestList: vi.fn(),
    } as unknown as HttpReviewTransport;

    const result = await new HttpReviewUploads(transport).deleteReviewItemsBatch(
      {
        projectRefId: first.project_ref_id,
        reviewItemIds: [first.id, second.id],
        confirmed: true,
      },
      context('delete-batch', 'edit'),
    );

    expect(command).toHaveBeenCalledOnce();
    expect(command).toHaveBeenCalledWith(
      `/api/v1/final-cut-review/edit/projects/${first.project_ref_id}/items/delete-batch`,
      'DeleteReviewItemsBatch',
      {
        project_ref_id: first.project_ref_id,
        review_item_ids: [first.id, second.id],
        confirmed: true,
      },
      expect.anything(),
      undefined,
      expect.objectContaining({ idempotent: true, commandId: expect.stringContaining('DeleteReviewItemsBatch_') }),
    );
    expect(result.results).toEqual([
      expect.objectContaining({ reviewItemId: first.id, outcome: 'deleted' }),
      expect.objectContaining({ reviewItemId: second.id, outcome: 'failed', errorCode: 'RESOURCE_STATE_CONFLICT' }),
    ]);
    expect(transport.requestList).not.toHaveBeenCalled();
  });

  it('reconciles a lost batch response per item without retrying with a new command identity', async () => {
    const first = reviewItemDto('item_batch_absent');
    const second = reviewItemDto('item_batch_visible');
    const command = vi.fn().mockRejectedValue(new TypeError('batch response lost'));
    const transport = {
      assertWriteContext: vi.fn(),
      projectForWrite: vi.fn().mockResolvedValue(projectDto(first.project_ref_id)),
      command,
      requestList: vi.fn().mockResolvedValue([second]),
    } as unknown as HttpReviewTransport;
    const input = {
      projectRefId: first.project_ref_id,
      reviewItemIds: [first.id, second.id],
      confirmed: true as const,
    };

    const initial = await new HttpReviewUploads(transport).deleteReviewItemsBatch(
      input,
      context('delete-batch-lost', 'edit'),
    );
    const replay = await new HttpReviewUploads(transport).deleteReviewItemsBatch(
      input,
      context('delete-batch-replay', 'edit'),
    );

    expect(initial.results.map((entry) => entry.outcome)).toEqual(['deleted', 'outcome_unknown']);
    expect(replay.results.map((entry) => entry.outcome)).toEqual(['deleted', 'outcome_unknown']);
    expect(command.mock.calls[1]?.[5]?.commandId).toBe(command.mock.calls[0]?.[5]?.commandId);
  });

  it('retains an explicit batch unknown identity across reload and blocks a different signature', async () => {
    const first = reviewItemDto('item_batch_explicit_deleted');
    const second = reviewItemDto('item_batch_explicit_unknown');
    const command = vi.fn().mockResolvedValue({
      project_ref_id: first.project_ref_id,
      results: [
        {
          project_ref_id: first.project_ref_id,
          review_item_id: first.id,
          outcome: 'deleted',
          error_code: null,
        },
        {
          project_ref_id: first.project_ref_id,
          review_item_id: second.id,
          outcome: 'outcome_unknown',
          error_code: 'COMMIT_OUTCOME_UNKNOWN',
        },
      ],
    });
    const transport = {
      assertWriteContext: vi.fn(),
      projectForWrite: vi.fn().mockResolvedValue(projectDto(first.project_ref_id)),
      command,
      requestList: vi.fn(),
    } as unknown as HttpReviewTransport;
    const input = {
      projectRefId: first.project_ref_id,
      reviewItemIds: [first.id, second.id],
      confirmed: true as const,
    };

    await new HttpReviewUploads(transport).deleteReviewItemsBatch(input, context('explicit-unknown', 'edit'));
    await new HttpReviewUploads(transport).deleteReviewItemsBatch(input, context('explicit-unknown-reload', 'edit'));
    expect(command.mock.calls[1]?.[5]?.commandId).toBe(command.mock.calls[0]?.[5]?.commandId);

    await expect(new HttpReviewUploads(transport).deleteReviewItemsBatch(
      { ...input, reviewItemIds: [second.id] },
      context('explicit-unknown-different', 'edit'),
    )).rejects.toBeInstanceOf(DeleteReviewItemProtectionUnavailableError);
  });
});

function context(requestId: string, entryMode: 'edit' | 'review' = 'review'): ExecutionContext {
  return { entryMode, requestId, createdAt: '2026-08-24T00:00:00Z' };
}

function projectDto(projectRefId: string): ProjectDTO {
  return {
    project_ref_id: projectRefId,
    project_code: 'TASK4',
    project_name: 'Task 4',
    description: '',
    source: 'local',
    external_project_id: null,
    lifecycle_status: 'active',
    completion_status: 'in_progress',
    deleted_at: null,
    fps_num: 25,
    fps_den: 1,
    lock_version: 7,
    created_at: '2026-08-24T00:00:00Z',
    updated_at: '2026-08-24T00:00:00Z',
  };
}

function reviewItemDto(id: string): ReviewItemDTO {
  return {
    id,
    project_ref_id: 'prj_batch_delete',
    item_code: id,
    episode_no: 1,
    title: id,
    workflow_status: 'pending_review',
    current_version_id: `ver_${id}`,
    current_version_no: 1,
    ui_status: 'pending_review',
    active_finalization_id: null,
    unresolved_current_version_count: 0,
    resolved_current_version_count: 0,
    historical_version_count: 0,
    is_finalized: false,
    lock_version: 1,
    created_at: '2026-08-24T00:00:00Z',
    updated_at: '2026-08-24T00:00:00Z',
  };
}
