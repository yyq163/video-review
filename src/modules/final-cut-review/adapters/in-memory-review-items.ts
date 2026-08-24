import type {
  ProjectRefId,
  ReviewItem,
  ReviewItemId,
  ReviewVersion,
  ResponsibleEditor,
  StoredOriginalFile,
  VersionId,
} from '../contracts/types';
import { ReviewDomainError, invariant } from '../core/errors';
import { createUuid } from '../core/uuid';
import type { ReviewItemWithMetadata } from '../ports';
import type { MediaMetadata } from '../ports';
import { cloneVersion, nowIso, originalMediaFromFile } from './in-memory-review-clones';
import type { InMemoryReviewStore } from './in-memory-review-store';

function validateReviewItemTitle(titleValue: string): void {
  const title = titleValue.trim();
  invariant(title.length >= 1 && title.length <= 512, '成片标题长度必须在 1 到 512 之间', 'VALIDATION_ERROR');
}

function validateEpisode(episodeValue: string): void {
  const episode = episodeValue.trim();
  invariant(/^\d+$/.test(episode) && Number(episode) >= 1, '集数必须是大于等于 1 的整数', 'VALIDATION_ERROR');
}

export class InMemoryReviewItems {
  constructor(private readonly store: InMemoryReviewStore) {}

  readonly requireResponsibleEditor = (responsibleEditorId?: string): ResponsibleEditor => {
    const normalizedId = responsibleEditorId?.trim() ?? '';
    invariant(normalizedId.length > 0, '责任剪辑师必填', 'VALIDATION_ERROR');
    return this.store.getResponsibleEditor(normalizedId);
  };

  readonly createReviewItemWithVersion = async (input: {
    projectRefId: ProjectRefId;
    title: string;
    episode: string;
    file: StoredOriginalFile;
    responsibleEditorId?: string;
    responsibleEditorNameSnapshot?: string;
    localProbe?: MediaMetadata;
  }): Promise<{ item: ReviewItemWithMetadata; version: ReviewVersion }> => {
    const project = this.store.assertProjectWritable(input.projectRefId);
    const editor = this.requireResponsibleEditor(input.responsibleEditorId);
    if (input.localProbe) {
      invariant(project.frameRate, '上传 V1 前必须先锁定项目帧率', 'RESOURCE_STATE_CONFLICT');
      invariant(
        project.frameRate.fpsNum === input.localProbe.fpsNum
          && project.frameRate.fpsDen === input.localProbe.fpsDen,
        '视频帧率与项目帧率不匹配',
        'VALIDATION_ERROR',
      );
    }
    validateReviewItemTitle(input.title);
    const timestamp = nowIso();
    const reviewItemId = `item_${createUuid()}`;
    const item: ReviewItemWithMetadata = {
      reviewItemId,
      projectRefId: input.projectRefId,
      itemCode: input.episode.trim() || input.title.trim(),
      title: input.title.trim(),
      episode: input.episode.trim(),
      currentVersionId: '',
      activeFinalizationId: null,
      status: 'pending_review',
      createdAt: timestamp,
      updatedAt: timestamp,
    };
    const version = this.buildVersion({
      projectRefId: input.projectRefId,
      reviewItemId,
      versionNo: 1,
      file: input.file,
      timestamp,
      status: 'pending_review',
      responsibleEditorId: editor.responsibleEditorId,
      responsibleEditorNameSnapshot: editor.displayName,
    });
    item.currentVersionId = version.versionId;
    this.store.items.set(item.reviewItemId, item);
    this.store.versions.set(version.versionId, version);
    this.store.emitChange();
    return { item: { ...item }, version: cloneVersion(version) };
  };

  readonly updateReviewItem = async (input: {
    projectRefId: ProjectRefId;
    reviewItemId: ReviewItemId;
    title: string;
    episode: string;
  }): Promise<ReviewItemWithMetadata> => {
    this.store.assertProjectWritable(input.projectRefId);
    const item = this.store.getItem(input.projectRefId, input.reviewItemId);
    invariant(item.status !== 'finalized', '已定稿成片只读', 'FINALIZED_READONLY');
    validateReviewItemTitle(input.title);
    const episode = input.episode.trim();
    const preservesNonnumericItemCode = !/^\d+$/.test(episode) && episode === item.itemCode;
    if (!preservesNonnumericItemCode) {
      validateEpisode(episode);
    }
    const next: ReviewItemWithMetadata = {
      ...item,
      title: input.title.trim(),
      episode: preservesNonnumericItemCode ? item.episode : episode,
      updatedAt: nowIso(),
    };
    this.store.items.set(next.reviewItemId, next);
    this.store.emitChange();
    return { ...next };
  };

  readonly deleteReviewItem = async (input: {
    projectRefId: ProjectRefId;
    reviewItemId: ReviewItemId;
    confirmed: true;
  }): Promise<ReviewItem> => {
    invariant(input.confirmed === true, '删除分集必须二次确认', 'RESOURCE_STATE_CONFLICT');
    this.store.assertProjectWritable(input.projectRefId);
    const item = this.store.getItem(input.projectRefId, input.reviewItemId);
    const versions = this.store.getVersionsForItem(input.projectRefId, input.reviewItemId);
    const issues = this.store.getIssuesForItem(input.projectRefId, input.reviewItemId, {
      includeDeleted: true,
    });
    const finalizations = [...this.store.finalizations.values()].filter(
      (finalization) => finalization.reviewItemId === input.reviewItemId,
    );
    const explicitlyRevoked = finalizations.length > 0
      && finalizations.every((finalization) => finalization.status === 'revoked');
    invariant(
      item.status === 'pending_review' || (item.status === 'in_review' && explicitlyRevoked),
      '审核已开始，不能删除分集',
      'REVIEW_ITEM_DELETE_LOCKED',
    );
    invariant(versions.length === 1, '已有多版本记录，不能删除分集', 'REVIEW_ITEM_DELETE_LOCKED');
    invariant(issues.length === 0, '已有审核意见，不能删除分集', 'REVIEW_ITEM_DELETE_LOCKED');
    invariant(!item.activeFinalizationId, '当前分集仍处于定稿状态', 'REVIEW_ITEM_DELETE_LOCKED');
    invariant(
      finalizations.length === 0 || explicitlyRevoked,
      '已有未撤销的定稿记录',
      'REVIEW_ITEM_DELETE_LOCKED',
    );

    this.store.versions.delete(versions[0].versionId);
    for (const finalization of finalizations) {
      this.store.finalizations.delete(finalization.finalizationId);
    }
    this.store.items.delete(item.reviewItemId);
    this.store.emitChange();
    return { ...item };
  };

  readonly deleteReviewItemsBatch = async (input: {
    projectRefId: ProjectRefId;
    reviewItemIds: ReviewItemId[];
    confirmed: true;
  }): Promise<{
    projectRefId: ProjectRefId;
    results: Array<{
      projectRefId: ProjectRefId;
      reviewItemId: ReviewItemId;
      outcome: 'deleted' | 'failed';
      errorCode: string | null;
    }>;
  }> => {
    invariant(input.confirmed === true, '批量删除分集必须二次确认', 'RESOURCE_STATE_CONFLICT');
    this.store.assertProjectWritable(input.projectRefId);
    const results = [];
    for (const reviewItemId of input.reviewItemIds) {
      try {
        await this.deleteReviewItem({
          projectRefId: input.projectRefId,
          reviewItemId,
          confirmed: true,
        });
        results.push({
          projectRefId: input.projectRefId,
          reviewItemId,
          outcome: 'deleted' as const,
          errorCode: null,
        });
      } catch (error) {
        results.push({
          projectRefId: input.projectRefId,
          reviewItemId,
          outcome: 'failed' as const,
          errorCode: error instanceof ReviewDomainError ? error.code : 'INTERNAL_SERVER_ERROR',
        });
      }
    }
    return { projectRefId: input.projectRefId, results };
  };

  readonly appendVersion = async (input: {
    projectRefId: ProjectRefId;
    reviewItemId: ReviewItemId;
    file: StoredOriginalFile;
    versionNote?: string;
    changeSummary?: string;
    supersedeReason?: string;
    responsibleEditorId?: string;
    responsibleEditorNameSnapshot?: string;
    localProbe?: MediaMetadata;
  }): Promise<ReviewVersion> => {
    const item = this.ensureAppendVersionWritable(input);
    const project = this.store.getProject(input.projectRefId);
    const editor = this.requireResponsibleEditor(input.responsibleEditorId);
    if (input.localProbe) {
      invariant(project.frameRate, '追加版本前必须先锁定项目帧率', 'RESOURCE_STATE_CONFLICT');
      invariant(
        project.frameRate.fpsNum === input.localProbe.fpsNum
          && project.frameRate.fpsDen === input.localProbe.fpsDen,
        '视频帧率与项目帧率不匹配',
        'VALIDATION_ERROR',
      );
    }
    const previousVersions = this.store.getVersionsForItem(input.projectRefId, input.reviewItemId);
    const timestamp = nowIso();
    const version = this.buildVersion({
      projectRefId: input.projectRefId,
      reviewItemId: input.reviewItemId,
      versionNo: previousVersions.length + 1,
      file: input.file,
      timestamp,
      status: 'pending_review',
      versionNote: input.versionNote,
      changeSummary: input.changeSummary || input.supersedeReason,
      responsibleEditorId: editor.responsibleEditorId,
      responsibleEditorNameSnapshot: editor.displayName,
    });
    this.store.versions.set(version.versionId, version);
    this.store.items.set(item.reviewItemId, {
      ...item,
      currentVersionId: version.versionId,
      activeFinalizationId: null,
      status: 'pending_review',
      updatedAt: timestamp,
    });
    this.store.emitChange();
    return cloneVersion(version);
  };

  readonly startReview = async (input: {
    projectRefId: ProjectRefId;
    reviewItemId: ReviewItemId;
    versionId: VersionId;
  }): Promise<ReviewVersion> => {
    this.store.assertProjectWritable(input.projectRefId);
    const item = this.store.getItem(input.projectRefId, input.reviewItemId);
    invariant(item.currentVersionId === input.versionId, '只能开始当前版本审阅', 'NOT_CURRENT_VERSION');
    invariant(item.status === 'pending_review', '只有待审版本可以开始审阅', 'INVALID_STATUS_TRANSITION');
    const version = this.store.getVersion(input.projectRefId, input.reviewItemId, input.versionId);
    const timestamp = nowIso();
    const nextVersion: ReviewVersion = { ...version, status: 'in_review' };
    this.store.versions.set(version.versionId, nextVersion);
    this.store.items.set(item.reviewItemId, { ...item, status: 'in_review', updatedAt: timestamp });
    this.store.emitChange();
    return cloneVersion(nextVersion);
  };

  readonly ensureAppendVersionWritable = (input: {
    projectRefId: ProjectRefId;
    reviewItemId: ReviewItemId;
  }): ReviewItemWithMetadata => {
    this.store.assertProjectWritable(input.projectRefId);
    const item = this.store.getItem(input.projectRefId, input.reviewItemId);
    invariant(item.status !== 'finalized', '已定稿后不能追加版本', 'FINALIZED_READONLY');
    const currentIssues = this.store.getIssuesForVersion(input.projectRefId, input.reviewItemId, item.currentVersionId);
    invariant(currentIssues.length > 0, '当前版本至少需要一条意见才能上传下一版本', 'NEXT_VERSION_REQUIRES_ISSUE');
    return item;
  };

  private buildVersion(input: {
    projectRefId: ProjectRefId;
    reviewItemId: ReviewItemId;
    versionNo: number;
    file: StoredOriginalFile;
    timestamp: string;
    status: ReviewVersion['status'];
    versionNote?: string | null;
    changeSummary?: string | null;
    responsibleEditorId?: string | null;
    responsibleEditorNameSnapshot?: string | null;
  }): ReviewVersion {
    const versionId = `ver_${createUuid()}`;
    const media = originalMediaFromFile(input.file);
    return {
      versionId,
      projectRefId: input.projectRefId,
      reviewItemId: input.reviewItemId,
      versionNo: input.versionNo,
      label: `V${input.versionNo}`,
      originalFileId: input.file.originalFileId,
      originalMedia: media,
      sha256: input.file.sha256,
      fileName: input.file.fileName,
      mimeType: input.file.mimeType,
      size: input.file.size,
      durationMs: media.durationMs,
      width: media.width,
      height: media.height,
      fpsNum: media.fpsNum,
      fpsDen: media.fpsDen,
      responsibleEditorId: input.responsibleEditorId ?? null,
      responsibleEditorNameSnapshot: input.responsibleEditorNameSnapshot ?? null,
      playbackStatus: 'ready',
      playbackAssetId: `playback_${versionId}`,
      playbackUrl: input.file.playbackUrl,
      thumbnailStatus: 'pending',
      thumbnailAssetId: `thumbnail_${versionId}`,
      thumbnailUrl: null,
      status: input.status,
      versionNote: input.versionNote?.trim() || null,
      changeSummary: input.changeSummary?.trim() || null,
      uploadedAt: input.timestamp,
      requestedChangesAt: null,
    };
  }
}
