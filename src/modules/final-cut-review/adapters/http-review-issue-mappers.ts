import type {
  ReviewAnnotationSetDTO,
  ReviewAnnotationShape as BackendAnnotationShape,
  ReviewIssueDTO,
  ReviewIssueRevisionDTO,
  ThreadMessageDTO,
} from '../contracts-generated/backend-contract';
import type {
  ReviewAnnotationSet,
  ReviewAnnotationShape,
  ReviewIssue,
  ReviewThreadMessage,
} from '../contracts/types';

function annotationShapeFromBackend(shape: BackendAnnotationShape): ReviewAnnotationShape {
  let bounds: ReviewAnnotationShape['bounds'];
  if (shape.path_data) {
    try {
      const parsed = JSON.parse(shape.path_data) as { bounds?: ReviewAnnotationShape['bounds'] };
      bounds = parsed.bounds;
    } catch {
      bounds = undefined;
    }
  }
  return {
    shapeId: shape.id,
    tool: shape.tool_type,
    color: shape.color,
    lineWidth: shape.line_width,
    fontSize: shape.font_size,
    points: shape.anchor_points,
    bounds,
    text: shape.text_content,
  };
}

function annotationShapeToBackend(shape: ReviewAnnotationShape, index: number): BackendAnnotationShape {
  return {
    id: shape.shapeId,
    tool_type: shape.tool,
    anchor_points: shape.points,
    path_data: shape.bounds ? JSON.stringify({ bounds: shape.bounds }) : undefined,
    text_content: shape.text,
    color: shape.color,
    line_width: shape.lineWidth,
    font_size: shape.fontSize,
    z_index: index,
  };
}

function annotationFromDto(dto: ReviewAnnotationSetDTO, revisionId: string): ReviewAnnotationSet {
  return {
    annotationSetId: dto.id,
    projectRefId: dto.project_ref_id,
    reviewItemId: dto.review_item_id,
    versionId: dto.version_id,
    issueId: dto.issue_id,
    revisionId,
    timestampMs: dto.timestamp_ms,
    frameNumber: dto.frame_number,
    canvasWidth: dto.canvas_width,
    canvasHeight: dto.canvas_height,
    videoWidth: dto.video_width,
    videoHeight: dto.video_height,
    shapes: dto.shapes.map(annotationShapeFromBackend),
    createdAt: dto.created_at,
  };
}

function revisionFromDto(
  dto: ReviewIssueRevisionDTO,
  fallback: { timestampMs: number; frameNumber: number },
): ReviewIssue['currentRevision'] {
  return {
    revisionId: dto.id,
    projectRefId: dto.project_ref_id,
    reviewItemId: dto.review_item_id,
    versionId: dto.version_id,
    issueId: dto.issue_id,
    revisionNo: dto.revision_no,
    content: dto.content,
    annotationSetId: dto.annotation_set_id ?? undefined,
    timestampMs: fallback.timestampMs,
    frameNumber: fallback.frameNumber,
    createdAt: dto.created_at,
  };
}

function readOnlyRevisionsFromDtos(
  dtos: ReviewIssueRevisionDTO[],
  currentRevision: ReviewIssue['currentRevision'],
  fallback: { timestampMs: number; frameNumber: number },
): ReviewIssue['revisions'] {
  const revisionsById = new Map(
    dtos.map((dto) => {
      const revision = Object.freeze(revisionFromDto(dto, fallback));
      return [revision.revisionId, revision] as const;
    }),
  );
  revisionsById.set(currentRevision.revisionId, currentRevision);
  const revisions = [...revisionsById.values()].sort(
    (left, right) => left.revisionNo - right.revisionNo || left.revisionId.localeCompare(right.revisionId),
  );
  Object.freeze(revisions);
  return revisions;
}

function messageFromDto(dto: ThreadMessageDTO): ReviewThreadMessage {
  return {
    messageId: dto.id,
    projectRefId: dto.project_ref_id,
    reviewItemId: dto.review_item_id,
    versionId: dto.version_id,
    issueId: dto.issue_id,
    body: dto.content,
    createdAt: dto.created_at,
  };
}

export function issueFromDto(
  dto: ReviewIssueDTO,
  messages: ThreadMessageDTO[] = [],
  revisionDtos: ReviewIssueRevisionDTO[] = [dto.current_revision],
): ReviewIssue {
  const fallback = {
    timestampMs: dto.timestamp_ms,
    frameNumber: dto.frame_number,
  };
  const currentRevision = Object.freeze(revisionFromDto(dto.current_revision, fallback));
  const revisions = readOnlyRevisionsFromDtos(revisionDtos, currentRevision, fallback);
  const currentAnnotationSet = dto.current_annotation_set
    ? annotationFromDto(dto.current_annotation_set, currentRevision.revisionId)
    : null;
  return {
    issueId: dto.id,
    issueNo: dto.issue_no,
    projectRefId: dto.project_ref_id,
    reviewItemId: dto.review_item_id,
    versionId: dto.version_id,
    status: dto.status,
    severity: 'normal',
    currentRevisionId: dto.current_revision_id,
    timestampMs: dto.timestamp_ms,
    frameNumber: dto.frame_number,
    lockVersion: dto.lock_version,
    body: dto.current_revision.content,
    annotationSetId: dto.current_annotation_set?.id ?? undefined,
    currentRevision,
    currentAnnotationSet,
    revisions,
    replies: messages.map(messageFromDto),
    deletedAt: dto.deleted_at ?? null,
    createdAt: dto.created_at,
    updatedAt: dto.updated_at,
  };
}

export function annotationPayload(input: {
  canvasWidth: number;
  canvasHeight: number;
  videoWidth: number;
  videoHeight: number;
  shapes: ReviewAnnotationShape[];
}) {
  return {
    canvas_width: input.canvasWidth,
    canvas_height: input.canvasHeight,
    video_width: input.videoWidth,
    video_height: input.videoHeight,
    shapes: input.shapes.map(annotationShapeToBackend),
  };
}
