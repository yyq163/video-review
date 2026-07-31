import type {
  FinalizationDTO,
  FinalizationRevocationDTO,
  ReviewItemDTO,
  ReviewVersionDTO,
} from '../contracts-generated/backend-contract';
import { FinalCutReviewHttpError } from '../contracts-generated/backend-contract';
import type { ReviewApiPort } from '../ports';
import { finalizationFromDto, itemFromDto, versionFromDto } from './http-review-project-mappers';
import {
  beginRevokeFinalizationOperation,
  clearRevokeFinalizationOperation,
  RevokeFinalizationResultUncertainError,
} from './http-review-finalization-operation';
import type { HttpReviewTransport } from './http-review-transport';

type WorkflowApi = Pick<ReviewApiPort, 'startReview' | 'finalizeCurrentVersion' | 'revokeFinalization'>;

export class HttpReviewWorkflow implements WorkflowApi {
  constructor(private readonly transport: HttpReviewTransport) {}

  readonly startReview: ReviewApiPort['startReview'] = async (input, context) => {
    this.transport.assertWriteContext(context, ['review']);
    const item = await this.transport.itemForLock(input.projectRefId, input.reviewItemId);
    const updated = await this.transport.command<
      ReviewItemDTO,
      { project_ref_id: string; review_item_id: string }
    >(
      `/api/v1/final-cut-review/review/projects/${input.projectRefId}/items/${input.reviewItemId}/start`,
      'StartReview',
      { project_ref_id: input.projectRefId, review_item_id: input.reviewItemId },
      context,
      item.lock_version,
    );
    const version = await this.transport.requestJson<ReviewVersionDTO>(
      `/api/v1/final-cut-review/projects/${input.projectRefId}/items/${input.reviewItemId}/versions/${input.versionId}`,
    );
    return versionFromDto(version, this.transport.baseUrl, updated);
  };

  readonly finalizeCurrentVersion: ReviewApiPort['finalizeCurrentVersion'] = async (input, context) => {
    this.transport.assertWriteContext(context, ['review']);
    const item = await this.transport.itemForLock(input.projectRefId, input.reviewItemId);
    const finalization = await this.transport.command<
      FinalizationDTO,
      { project_ref_id: string; review_item_id: string; version_id: string; confirmed: true }
    >(
      `/api/v1/final-cut-review/review/projects/${input.projectRefId}/items/${input.reviewItemId}/versions/${input.versionId}/finalize`,
      'FinalizeVersion',
      {
        project_ref_id: input.projectRefId,
        review_item_id: input.reviewItemId,
        version_id: input.versionId,
        confirmed: input.confirmed,
      },
      context,
      item.lock_version,
      { idempotent: true },
    );
    return finalizationFromDto(finalization);
  };

  readonly revokeFinalization: ReviewApiPort['revokeFinalization'] = async (input, context) => {
    this.transport.assertWriteContext(context, ['review']);
    const item = await this.transport.itemForLock(input.projectRefId, input.reviewItemId);
    const operation = beginRevokeFinalizationOperation(
      input.projectRefId,
      input.reviewItemId,
      item.lock_version,
    );
    try {
      const result = await this.transport.command<
        FinalizationRevocationDTO,
        { project_ref_id: string; review_item_id: string; confirmed: true }
      >(
        `/api/v1/final-cut-review/review/projects/${input.projectRefId}/items/${input.reviewItemId}/finalization/revoke`,
        'RevokeFinalization',
        {
          project_ref_id: input.projectRefId,
          review_item_id: input.reviewItemId,
          confirmed: input.confirmed,
        },
        context,
        operation.lockVersion,
        {
          idempotent: true,
          commandId: operation.commandId,
        },
      );
      clearRevokeFinalizationOperation(input.projectRefId, input.reviewItemId);
      return {
        finalization: finalizationFromDto(result.finalization),
        reviewItem: itemFromDto(result.review_item),
        cleanupStatus: result.cleanup_status,
        invalidatedPackageIds: [...result.invalidated_package_ids],
      };
    } catch (error) {
      if (
        error instanceof FinalCutReviewHttpError &&
        error.httpStatus < 500 &&
        error.code !== 'IDEMPOTENCY_CONFLICT'
      ) {
        clearRevokeFinalizationOperation(input.projectRefId, input.reviewItemId);
        throw error;
      }
      throw new RevokeFinalizationResultUncertainError(error);
    }
  };
}
