import { afterEach, describe, expect, it, vi } from 'vitest';
import type {
  FinalizationRevocationDTO,
  ReviewItemDTO,
} from '../contracts-generated/backend-contract';
import { FinalCutReviewHttpError } from '../contracts-generated/backend-contract';
import type { ExecutionContext } from '../contracts/types';
import type { HttpReviewTransport } from './http-review-transport';
import {
  getRevokeFinalizationProtectionState,
  RevokeFinalizationResultUncertainError,
} from './http-review-finalization-operation';
import { HttpReviewWorkflow } from './http-review-workflow';

const PROJECT_ID = 'project-revoke';
const ITEM_ID = 'item-revoke';

function context(): ExecutionContext {
  return {
    entryMode: 'review',
    requestId: 'request-revoke',
    createdAt: '2026-07-31T00:00:00.000Z',
  };
}

function reviewItem(lockVersion: number): ReviewItemDTO {
  return {
    id: ITEM_ID,
    project_ref_id: PROJECT_ID,
    item_code: '13',
    episode_no: 13,
    title: '第13集',
    workflow_status: 'finalized',
    current_version_id: 'version-revoke',
    current_version_no: 2,
    ui_status: 'finalized',
    active_finalization_id: 'finalization-revoke',
    unresolved_current_version_count: 0,
    resolved_current_version_count: 1,
    historical_version_count: 1,
    is_finalized: true,
    lock_version: lockVersion,
    created_at: '2026-07-31T00:00:00.000Z',
    updated_at: '2026-07-31T00:00:00.000Z',
  };
}

function revocationResult(): FinalizationRevocationDTO {
  const item = {
    ...reviewItem(8),
    workflow_status: 'in_review' as const,
    active_finalization_id: null,
    is_finalized: false,
  };
  return {
    finalization: {
      id: 'finalization-revoke',
      project_ref_id: PROJECT_ID,
      review_item_id: ITEM_ID,
      version_id: 'version-revoke',
      version_no: 2,
      original_media: {
        original_file_id: 'file-revoke',
        original_filename: 'episode-13.mov',
        mime_type: 'video/quicktime',
        file_size: 1024,
        sha256: 'a'.repeat(64),
        duration_ms: 1000,
        width: 1920,
        height: 1080,
        fps_num: 25,
        fps_den: 1,
        media_probe_version: 'ffprobe',
      },
      status: 'revoked',
      finalized_at: '2026-07-31T00:00:00.000Z',
      revoked_at: '2026-07-31T00:01:00.000Z',
    },
    review_item: item,
    cleanup_status: 'pending',
    invalidated_package_ids: ['package-1'],
  };
}

afterEach(() => {
  window.sessionStorage.clear();
  vi.restoreAllMocks();
});

describe('HTTP finalization revocation idempotency', () => {
  it('persists commandId and original If-Match across an uncertain response and clears only after success', async () => {
    const command = vi
      .fn()
      .mockRejectedValueOnce(new TypeError('response lost'))
      .mockResolvedValueOnce(revocationResult());
    const itemForLock = vi
      .fn()
      .mockResolvedValueOnce(reviewItem(7))
      .mockResolvedValueOnce(reviewItem(99));
    const transport = {
      assertWriteContext: vi.fn(),
      itemForLock,
      command,
      baseUrl: 'https://review.example',
    } as unknown as HttpReviewTransport;
    const workflow = new HttpReviewWorkflow(transport);
    const input = {
      projectRefId: PROJECT_ID,
      reviewItemId: ITEM_ID,
      confirmed: true as const,
    };

    await expect(workflow.revokeFinalization(input, context()))
      .rejects.toBeInstanceOf(RevokeFinalizationResultUncertainError);
    expect(getRevokeFinalizationProtectionState(PROJECT_ID, ITEM_ID)).toBe('required');
    const firstOptions = command.mock.calls[0]?.[5] as { commandId: string };

    await expect(workflow.revokeFinalization(input, context())).resolves.toMatchObject({
      cleanupStatus: 'pending',
      invalidatedPackageIds: ['package-1'],
    });
    const secondOptions = command.mock.calls[1]?.[5] as { commandId: string };
    expect(secondOptions.commandId).toBe(firstOptions.commandId);
    expect(command.mock.calls[0]?.[4]).toBe(7);
    expect(command.mock.calls[1]?.[4]).toBe(7);
    expect(getRevokeFinalizationProtectionState(PROJECT_ID, ITEM_ID)).toBe('clear');
  });

  it('clears the stored operation only after the same command is definitively rejected before execution', async () => {
    const definitiveRejection = new FinalCutReviewHttpError(
      {
        code: 'OPTIMISTIC_LOCK_CONFLICT',
        message: '撤回请求未执行，锁版本已变化',
        details: {},
        request_id: 'revoke-definitive-rejection',
        timestamp: '2026-07-31T00:02:00.000Z',
        contract_version: '1.0',
        http_status: 409,
      },
      409,
    );
    const command = vi
      .fn()
      .mockRejectedValueOnce(new TypeError('request failed before response'))
      .mockRejectedValueOnce(definitiveRejection);
    const transport = {
      assertWriteContext: vi.fn(),
      itemForLock: vi.fn().mockResolvedValue(reviewItem(7)),
      command,
      baseUrl: 'https://review.example',
    } as unknown as HttpReviewTransport;
    const workflow = new HttpReviewWorkflow(transport);
    const input = {
      projectRefId: PROJECT_ID,
      reviewItemId: ITEM_ID,
      confirmed: true as const,
    };

    await expect(workflow.revokeFinalization(input, context()))
      .rejects.toBeInstanceOf(RevokeFinalizationResultUncertainError);
    const firstOptions = command.mock.calls[0]?.[5] as { commandId: string };
    expect(getRevokeFinalizationProtectionState(PROJECT_ID, ITEM_ID)).toBe('required');

    await expect(workflow.revokeFinalization(input, context()))
      .rejects.toBe(definitiveRejection);
    const retryOptions = command.mock.calls[1]?.[5] as { commandId: string };
    expect(retryOptions.commandId).toBe(firstOptions.commandId);
    expect(getRevokeFinalizationProtectionState(PROJECT_ID, ITEM_ID)).toBe('clear');
  });
});
