import { useCallback, useEffect, useMemo, useState, type RefObject } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import type { EntryMode, ReviewAnnotationShape, ReviewIssue, ReviewWorkspace, UploadProgress } from '../contracts/types';
import { useProjectDetail, useReviewMutations } from '../entry/use-review-queries';
import { actionError } from '../components/shared';
import type { ReviewPlayerHandle } from '../components/ReviewPlayer';
import { dedupeReviewItemsByEpisode } from '../core/episode-dedupe';
import { sortedIssuesForPlayback } from '../core/playback';
import {
  getAppendVersionProtectionState,
  type AppendVersionProtectionState,
} from '../adapters/http-review-uploads';
import { useReviewWorkspaceActions, type AppendVersionInput } from './review-workspace-actions';
import { useReviewWorkspacePlayback } from './review-workspace-playback';

export interface ReviewWorkspaceLoadedProps {
  entryMode: EntryMode;
  projectRefId: string;
  reviewItemId: string;
  data: ReviewWorkspace;
  refetchWorkspace(): Promise<{ data?: ReviewWorkspace }>;
}

export function useReviewWorkspaceController(
  props: ReviewWorkspaceLoadedProps,
  playerRef: RefObject<ReviewPlayerHandle | null>,
) {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const selectedIssueId = searchParams.get('issue') ?? undefined;
  const mutations = useReviewMutations(props.entryMode);
  const projectDetail = useProjectDetail(props.entryMode, props.projectRefId);
  const [annotationToolbarHost, setAnnotationToolbarHost] = useState<HTMLDivElement | null>(null);
  const [timeMs, setTimeMs] = useState(0);
  const [draftShapes, setDraftShapes] = useState<ReviewAnnotationShape[]>([]);
  const [optimisticIssue, setOptimisticIssue] = useState<ReviewIssue | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [uploadProgress, setUploadProgress] = useState<UploadProgress | undefined>(undefined);
  const [appendVersionRetry, setAppendVersionRetry] = useState<AppendVersionInput | null>(null);
  const [appendVersionProtectionState, setAppendVersionProtectionState] = useState<AppendVersionProtectionState>(
    () => props.entryMode === 'edit'
      ? getAppendVersionProtectionState(props.projectRefId, props.reviewItemId)
      : 'clear',
  );
  const [appendVersionConfirmationPending, setAppendVersionConfirmationPending] = useState(false);
  const data = props.data;

  const pending = useMemo(
    () =>
      mutations.appendVersion.isPending ||
      mutations.startReview.isPending ||
      mutations.createIssue.isPending ||
      mutations.resolveIssue.isPending ||
      mutations.reopenIssue.isPending ||
      mutations.requestChanges.isPending ||
      mutations.finalizeCurrentVersion.isPending ||
      mutations.downloadFinalizedOriginal.isPending ||
      mutations.createProjectFinalizedPackage.isPending ||
      mutations.downloadProjectFinalizedPackage.isPending ||
      mutations.editIssue.isPending ||
      mutations.replyToIssue.isPending ||
      mutations.deleteIssue.isPending,
    [mutations],
  );
  const readonlyReason = data.project.status === 'archived' ? '归档项目只读，恢复后才能执行写操作。' : undefined;
  const isSelectedCurrent = data.currentVersion.versionId === data.item.currentVersionId;
  const writeReadonlyReason =
    readonlyReason ??
    (props.entryMode !== 'review'
      ? '剪辑入口仅可查看意见，不能创建或处理意见。'
      : !isSelectedCurrent
        ? '历史版本只读'
        : data.item.status === 'changes_requested'
          ? '已要求修改，当前版本只读'
          : data.currentVersion.status === 'finalized'
            ? '当前版本已定稿冻结'
            : undefined);
  const issuePanelReadonlyReason = writeReadonlyReason === '当前版本已定稿冻结' ? '定稿冻结后意见区只读。' : writeReadonlyReason;
  const canAppendVersion =
    props.entryMode === 'edit' &&
    !readonlyReason &&
    isSelectedCurrent &&
    data.item.status !== 'finalized' &&
    data.item.status !== 'in_review';
  const nextLabel = `V${data.versions.length + 1}`;
  const currentIssues = useMemo(() => {
    if (
      !optimisticIssue ||
      optimisticIssue.projectRefId !== props.projectRefId ||
      optimisticIssue.reviewItemId !== props.reviewItemId ||
      optimisticIssue.versionId !== data.currentVersion.versionId
    ) {
      return data.currentIssues;
    }
    const currentCopy = data.currentIssues.filter((issue) => issue.issueId !== optimisticIssue.issueId);
    return sortedIssuesForPlayback([...currentCopy, optimisticIssue]);
  }, [data.currentIssues, data.currentVersion.versionId, optimisticIssue, props.projectRefId, props.reviewItemId]);
  const currentInput = useMemo(
    () => ({ projectRefId: props.projectRefId, reviewItemId: props.reviewItemId, versionId: data.currentVersion.versionId }),
    [data.currentVersion.versionId, props.projectRefId, props.reviewItemId],
  );
  const selectedIssue = currentIssues.find((issue) => issue.issueId === selectedIssueId) ?? null;
  const selectedAnnotationSet = selectedIssue?.currentAnnotationSet ?? null;
  const compareIssues = useMemo(() => {
    const byId = new Map<string, ReviewIssue>();
    for (const issue of [...data.historicalIssues, ...currentIssues]) byId.set(issue.issueId, issue);
    return [...byId.values()];
  }, [currentIssues, data.historicalIssues]);
  const episodeItems = useMemo(
    () =>
      dedupeReviewItemsByEpisode(projectDetail.data?.items?.length ? projectDetail.data.items : [data.item], {
        currentItemId: data.item.reviewItemId,
        versionsByItem: projectDetail.data?.versionsByItem,
      }),
    [data.item, projectDetail.data],
  );
  const episodeVersionCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const item of episodeItems) {
      counts[item.reviewItemId] =
        projectDetail.data?.versionsByItem[item.reviewItemId]?.length ??
        (item.reviewItemId === data.item.reviewItemId ? data.versions.length : 0);
    }
    return counts;
  }, [data.item.reviewItemId, data.versions.length, episodeItems, projectDetail.data]);
  const episodeCurrentLabels = useMemo(() => {
    const labels: Record<string, string> = {};
    for (const item of episodeItems) {
      const versions =
        projectDetail.data?.versionsByItem[item.reviewItemId] ??
        (item.reviewItemId === data.item.reviewItemId ? data.versions : []);
      labels[item.reviewItemId] = versions.find((version) => version.versionId === item.currentVersionId)?.label ?? '-';
    }
    return labels;
  }, [data.item.reviewItemId, data.versions, episodeItems, projectDetail.data]);
  const showToast = useCallback((message: string) => setToast(message), []);
  const showActionError = useCallback((caught: unknown) => showToast(actionError(caught)), [showToast]);
  const playback = useReviewWorkspacePlayback({
    projectRefId: props.projectRefId,
    reviewItemId: props.reviewItemId,
    currentVersionId: data.currentVersion.versionId,
    currentItemVersionId: data.item.currentVersionId,
    currentIssues,
    historicalIssues: data.historicalIssues,
    selectedIssueId,
    searchParams,
    setSearchParams,
    playerRef,
    setOptimisticIssue,
  });
  const { setPlaybackError } = playback;
  const selectVersionParams = useCallback(
    (versionId: string, currentVersionId: string) => {
      const next = new URLSearchParams();
      if (versionId !== currentVersionId) next.set('version', versionId);
      setSearchParams(next, { replace: false });
      playerRef.current?.clearDraft();
      setDraftShapes([]);
      setPlaybackError(null);
    },
    [playerRef, setPlaybackError, setSearchParams],
  );
  const actions = useReviewWorkspaceActions({
    projectRefId: props.projectRefId,
    reviewItemId: props.reviewItemId,
    data,
    mutations,
    currentInput,
    playerRef,
    timeMs,
    draftShapes,
    setDraftShapes,
    setOptimisticIssue,
    setUploadProgress,
    appendVersionProtectionState,
    setAppendVersionProtectionState,
    appendVersionRetry,
    setAppendVersionRetry,
    setAppendVersionConfirmationPending,
    refetchWorkspace: props.refetchWorkspace,
    showToast,
    showActionError,
    selectIssue: playback.selectIssue,
    clearSelectedIssueParam: playback.clearSelectedIssueParam,
    selectVersionParams,
  });

  useEffect(() => {
    if (!toast) return;
    const timeout = window.setTimeout(() => setToast(null), 2400);
    return () => window.clearTimeout(timeout);
  }, [toast]);
  useEffect(() => {
    if (!optimisticIssue) return;
    if (
      optimisticIssue.versionId !== data.currentVersion.versionId ||
      data.currentIssues.some(
        (issue) => issue.issueId === optimisticIssue.issueId && issue.currentRevisionId === optimisticIssue.currentRevisionId,
      )
    ) {
      const frame = window.requestAnimationFrame(() => setOptimisticIssue(null));
      return () => window.cancelAnimationFrame(frame);
    }
  }, [data.currentIssues, data.currentVersion.versionId, optimisticIssue]);

  return {
    props,
    data,
    navigate,
    mutations,
    annotationToolbarHost,
    setAnnotationToolbarHost,
    timeMs,
    setTimeMs,
    setDraftShapes,
    toast,
    setToast,
    uploadProgress,
    appendVersionProtectionState,
    appendVersionConfirmationRequired: appendVersionProtectionState !== 'clear',
    appendVersionConfirmationPending,
    appendVersionRetryAvailable: appendVersionRetry !== null,
    pending,
    readonlyReason,
    isSelectedCurrent,
    writeReadonlyReason,
    issuePanelReadonlyReason,
    canAppendVersion,
    nextLabel,
    currentIssues,
    selectedIssueId,
    selectedAnnotationSet,
    compareIssues,
    episodeItems,
    episodeVersionCounts,
    episodeCurrentLabels,
    playback,
    actions,
    selectVersion: (versionId: string) => selectVersionParams(versionId, data.item.currentVersionId),
  };
}

export type ReviewWorkspaceController = ReturnType<typeof useReviewWorkspaceController>;
