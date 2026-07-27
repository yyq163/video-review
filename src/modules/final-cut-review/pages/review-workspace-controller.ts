import { useCallback, useEffect, useMemo, useState, type RefObject } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import type { EntryMode, IssueStatus, ReviewAnnotationShape, ReviewIssue, ReviewWorkspace, UploadProgress } from '../contracts/types';
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
  const [optimisticIssueStatuses, setOptimisticIssueStatuses] = useState<Record<string, IssueStatus>>({});
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
      mutations.createIssue.isPending ||
      mutations.resolveIssue.isPending ||
      mutations.reopenIssue.isPending ||
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
    (!isSelectedCurrent
      ? '历史版本只读'
      : props.entryMode !== 'review'
        ? '剪辑入口仅可查看意见正文并标记“已修改”。'
        : data.currentVersion.status === 'finalized'
          ? '当前版本已定稿冻结'
          : undefined);
  const issuePanelReadonlyReason = writeReadonlyReason === '当前版本已定稿冻结' ? '定稿冻结后意见区只读。' : writeReadonlyReason;
  const issueStatusReadonlyReason =
    readonlyReason ??
    (!isSelectedCurrent
      ? '历史版本只读'
      : data.currentVersion.status === 'finalized'
        ? '定稿冻结后意见区只读。'
        : undefined);
  const canAppendVersion =
    props.entryMode === 'edit' &&
    !readonlyReason &&
    isSelectedCurrent &&
    data.item.status !== 'finalized' &&
    data.currentIssues.length > 0;
  const nextLabel = `V${data.versions.length + 1}`;
  const currentIssues = useMemo(() => {
    if (
      !optimisticIssue ||
      optimisticIssue.projectRefId !== props.projectRefId ||
      optimisticIssue.reviewItemId !== props.reviewItemId ||
      optimisticIssue.versionId !== data.currentVersion.versionId
    ) {
      return sortedIssuesForPlayback(
        data.currentIssues.map((issue) => ({
          ...issue,
          status: optimisticIssueStatuses[issue.issueId] ?? issue.status,
        })),
      );
    }
    const currentCopy = data.currentIssues.filter((issue) => issue.issueId !== optimisticIssue.issueId);
    return sortedIssuesForPlayback(
      [...currentCopy, optimisticIssue].map((issue) => ({
        ...issue,
        status: optimisticIssueStatuses[issue.issueId] ?? issue.status,
      })),
    );
  }, [
    data.currentIssues,
    data.currentVersion.versionId,
    optimisticIssue,
    optimisticIssueStatuses,
    props.projectRefId,
    props.reviewItemId,
  ]);
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
  const episodeItems = useMemo(() => {
    const projectItems = projectDetail.data?.items ?? [];
    const itemsWithFreshWorkspaceItem = [
      ...projectItems.filter((item) => item.reviewItemId !== data.item.reviewItemId),
      data.item,
    ];
    return dedupeReviewItemsByEpisode(
      itemsWithFreshWorkspaceItem,
      {
        currentItemId: data.item.reviewItemId,
        versionsByItem: projectDetail.data?.versionsByItem,
      },
    );
  }, [data.item, projectDetail.data]);
  const episodeUnresolvedCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const item of episodeItems) {
      const currentVersionIssues =
        item.reviewItemId === data.item.reviewItemId
          ? data.currentVersion.versionId === item.currentVersionId
            ? currentIssues
            : [...data.currentIssues, ...data.historicalIssues].filter(
                (issue) => issue.versionId === item.currentVersionId,
              )
          : projectDetail.data?.issuesByVersion[item.currentVersionId] ?? [];
      counts[item.reviewItemId] = currentVersionIssues.filter(
        (issue) => issue.status === 'unresolved' && !issue.deletedAt,
      ).length;
    }
    return counts;
  }, [
    currentIssues,
    data.currentIssues,
    data.currentVersion.versionId,
    data.historicalIssues,
    data.item.reviewItemId,
    episodeItems,
    projectDetail.data,
  ]);
  const episodeCurrentVersionMetadata = useMemo(() => {
    const labels: Record<string, string> = {};
    const fileNames: Record<string, string> = {};
    for (const item of episodeItems) {
      const versions =
        item.reviewItemId === data.item.reviewItemId
          ? data.versions
          : projectDetail.data?.versionsByItem[item.reviewItemId] ?? [];
      const currentVersion = versions.find((version) => version.versionId === item.currentVersionId);
      labels[item.reviewItemId] = currentVersion?.label || '-';
      fileNames[item.reviewItemId] =
        currentVersion?.originalMedia.originalFilename || currentVersion?.fileName || '-';
    }
    return { labels, fileNames };
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
    setOptimisticIssueStatuses,
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
  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      setOptimisticIssueStatuses((current) => {
        const next = Object.fromEntries(
          Object.entries(current).filter(([issueId, status]) => {
            const issue = data.currentIssues.find((candidate) => candidate.issueId === issueId);
            return issue !== undefined && issue.status !== status;
          }),
        );
        return Object.keys(next).length === Object.keys(current).length ? current : next;
      });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [data.currentIssues]);

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
    issueStatusReadonlyReason,
    canAppendVersion,
    nextLabel,
    currentIssues,
    selectedIssueId,
    selectedAnnotationSet,
    compareIssues,
    episodeItems,
    episodeUnresolvedCounts,
    episodeCurrentVersionLabels: episodeCurrentVersionMetadata.labels,
    episodeCurrentFileNames: episodeCurrentVersionMetadata.fileNames,
    playback,
    actions,
    selectVersion: (versionId: string) => selectVersionParams(versionId, data.item.currentVersionId),
  };
}

export type ReviewWorkspaceController = ReturnType<typeof useReviewWorkspaceController>;
