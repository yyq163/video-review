import type {
  FinalizationDTO,
  PackageSnapshotDTO,
  ProjectDTO,
  ReviewItemDTO,
  ReviewVersionDTO,
} from '../contracts-generated/backend-contract';
import type {
  FinalizationRecord,
  PackageResult,
  Project,
  ReviewItem,
  ReviewVersion,
} from '../contracts/types';

export function projectFromDto(dto: ProjectDTO): Project {
  return {
    projectRefId: dto.project_ref_id,
    code: dto.project_code,
    name: dto.project_name,
    description: dto.description,
    status: dto.lifecycle_status,
    deletedAt: dto.deleted_at ?? null,
    createdAt: dto.created_at,
    updatedAt: dto.updated_at,
  };
}

export function originalMediaFromDto(dto: ReviewVersionDTO | FinalizationDTO) {
  return {
    originalFileId: dto.original_media.original_file_id,
    originalFilename: dto.original_media.original_filename,
    mimeType: dto.original_media.mime_type,
    fileSize: dto.original_media.file_size,
    sha256: dto.original_media.sha256,
    durationMs: dto.original_media.duration_ms,
    width: dto.original_media.width,
    height: dto.original_media.height,
    fpsNum: dto.original_media.fps_num,
    fpsDen: dto.original_media.fps_den,
    mediaProbeVersion: dto.original_media.media_probe_version,
  };
}

export function itemFromDto(dto: ReviewItemDTO): ReviewItem {
  return {
    reviewItemId: dto.id,
    projectRefId: dto.project_ref_id,
    title: dto.title,
    episode:
      /^\d+$/.test(dto.item_code) && dto.episode_no === Number(dto.item_code)
        ? dto.item_code
        : dto.episode_no?.toString() ?? dto.item_code,
    currentVersionId: dto.current_version_id,
    activeFinalizationId: dto.active_finalization_id ?? null,
    status: dto.workflow_status,
    createdAt: dto.created_at,
    updatedAt: dto.updated_at,
  };
}

function versionStatus(dto: ReviewVersionDTO, item?: ReviewItemDTO): ReviewVersion['status'] {
  if (item && dto.id === item.current_version_id) {
    return item.workflow_status;
  }
  return dto.is_current ? 'in_review' : 'changes_requested';
}

export function versionFromDto(dto: ReviewVersionDTO, baseUrl: string, item?: ReviewItemDTO): ReviewVersion {
  const media = originalMediaFromDto(dto);
  return {
    versionId: dto.id,
    projectRefId: dto.project_ref_id,
    reviewItemId: dto.review_item_id,
    versionNo: dto.version_no,
    label: dto.version_label,
    originalFileId: media.originalFileId,
    originalMedia: media,
    sha256: media.sha256,
    fileName: media.originalFilename,
    mimeType: media.mimeType,
    size: media.fileSize,
    durationMs: media.durationMs,
    width: media.width,
    height: media.height,
    fpsNum: media.fpsNum,
    fpsDen: media.fpsDen,
    playbackAssetId: dto.playback_asset_id ?? media.originalFileId,
    playbackUrl: `${baseUrl}/api/v1/final-cut-review/projects/${dto.project_ref_id}/items/${dto.review_item_id}/versions/${dto.id}/stream`,
    status: versionStatus(dto, item),
    versionNote: dto.version_note ?? null,
    changeSummary: dto.change_summary ?? null,
    uploadedAt: dto.created_at,
    requestedChangesAt: null,
  };
}

export function finalizationFromDto(dto: FinalizationDTO): FinalizationRecord {
  const media = originalMediaFromDto(dto);
  return {
    finalizationId: dto.id,
    projectRefId: dto.project_ref_id,
    reviewItemId: dto.review_item_id,
    versionId: dto.version_id,
    originalFileId: media.originalFileId,
    sha256: media.sha256,
    fileName: media.originalFilename,
    originalMedia: media,
    frozenAt: dto.finalized_at,
  };
}

export function packageFromDto(dto: PackageSnapshotDTO): PackageResult {
  if (dto.status !== 'ready') {
    throw new Error('项目包准备失败，请重试。');
  }
  return {
    packageId: dto.id,
    projectRefId: dto.project_ref_id,
    packageFilename: dto.package_filename,
    createdAt: dto.created_at,
    entries: dto.items.map((item) => ({
      projectRefId: dto.project_ref_id,
      reviewItemId: item.review_item_id,
      versionId: item.version_id,
      finalizationId: item.finalization_id,
      originalFileId: item.original_file_id,
      sha256: item.sha256,
      fileName: item.original_filename,
    })),
    fileName: dto.package_filename,
  };
}
