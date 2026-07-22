import type {
  ProjectRefId,
  ReviewItem,
  ReviewItemId,
  ReviewVersion,
  StoredOriginalFile,
  VersionId,
} from '../contracts/types';
import { invariant } from '../core/errors';
import { createUuid } from '../core/uuid';
import type { ReviewItemWithMetadata } from '../ports';
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

  readonly createReviewItemWithVersion = async (input: {
    projectRefId: ProjectRefId;
    title: string;
    episode: string;
    file: StoredOriginalFile;
  }): Promise<{ item: ReviewItemWithMetadata; version: ReviewVersion }> => {
    this.store.assertProjectWritable(input.projectRefId);
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
    invariant(item.status === 'pending_review', '审核已开始，不能删除分集', 'REVIEW_ITEM_DELETE_LOCKED');
    invariant(versions.length === 1, '已有多版本记录，不能删除分集', 'REVIEW_ITEM_DELETE_LOCKED');
    invariant(issues.length === 0, '已有审核意见，不能删除分集', 'REVIEW_ITEM_DELETE_LOCKED');
    invariant(
      !item.activeFinalizationId && finalizations.length === 0,
      '已有定稿记录，不能删除分集',
      'REVIEW_ITEM_DELETE_LOCKED',
    );

    this.store.versions.delete(versions[0].versionId);
    this.store.items.delete(item.reviewItemId);
    this.store.emitChange();
    return { ...item };
  };

  readonly appendVersion = async (input: {
    projectRefId: ProjectRefId;
    reviewItemId: ReviewItemId;
    file: StoredOriginalFile;
    versionNote?: string;
    changeSummary?: string;
    supersedeReason?: string;
  }): Promise<ReviewVersion> => {
    const item = this.ensureAppendVersionWritable(input);
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
    supersedeReason?: string;
  }): ReviewItemWithMetadata => {
    this.store.assertProjectWritable(input.projectRefId);
    const item = this.store.getItem(input.projectRefId, input.reviewItemId);
    invariant(item.status !== 'finalized', '已定稿后不能追加版本', 'FINALIZED_READONLY');
    invariant(
      item.status !== 'in_review',
      '审阅中不能追加版本，请先要求修改或完成当前审阅',
      'IN_REVIEW_UPLOAD_FORBIDDEN',
    );
    if (item.status === 'pending_review') {
      invariant(Boolean(input.supersedeReason?.trim()), '待审状态主动补版必须填写原因', 'SUPERSEDE_REASON_REQUIRED');
    }
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
      playbackAssetId: `playback_${versionId}`,
      playbackUrl: input.file.playbackUrl,
      status: input.status,
      versionNote: input.versionNote?.trim() || null,
      changeSummary: input.changeSummary?.trim() || null,
      uploadedAt: input.timestamp,
      requestedChangesAt: null,
    };
  }
}
