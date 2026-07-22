import type {
  FinalizationDTO,
  ReviewItemDTO,
  ReviewVersionDTO,
} from '../contracts-generated/backend-contract';
import type { ReviewApiPort } from '../ports';
import { finalizationFromDto, versionFromDto } from './http-review-project-mappers';
import type { HttpReviewTransport } from './http-review-transport';

type WorkflowApi = Pick<ReviewApiPort, 'startReview' | 'requestChanges' | 'finalizeCurrentVersion'>;

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

  readonly requestChanges: ReviewApiPort['requestChanges'] = async (input, context) => {
    this.transport.assertWriteContext(context, ['review']);
    const item = await this.transport.itemForLock(input.projectRefId, input.reviewItemId);
    const updated = await this.transport.command<
      ReviewItemDTO,
      { project_ref_id: string; review_item_id: string; version_id: string; summary: string }
    >(
      `/api/v1/final-cut-review/review/projects/${input.projectRefId}/items/${input.reviewItemId}/versions/${input.versionId}/request-changes`,
      'RequestChanges',
      {
        project_ref_id: input.projectRefId,
        review_item_id: input.reviewItemId,
        version_id: input.versionId,
        summary: 'Request changes from review UI',
      },
      context,
      item.lock_version,
      { idempotent: true },
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
}
