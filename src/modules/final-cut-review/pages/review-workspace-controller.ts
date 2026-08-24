import { useCallback, useEffect, useMemo, useState, type RefObject } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import type { EntryMode, IssueStatus, ReviewAnnotationShape, ReviewIssue, ReviewWorkspace, UploadProgress } from '../contracts/types';
import { useIssueDetail, useProjectSummary, useResponsibleEditors, useReviewMutations } from '../entry/use-review-queries';
import { useMediaMetadata } from '../entry/runtime';
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
import { useFinalizationRevocationCoordinator } from './use-finalization-revocation-coordinator';

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
  const projectSummary = useProjectSummary(props.entryMode, props.projectRefId);
  const responsibleEditors = useResponsibleEditors(props.entryMode);
  const mediaMetadata = useMediaMetadata();
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
  const authoritativeCurrentVersion = useMemo(
    () => {
      const matchingVersions = data.versions.filter(
        (version) => version.versionId === data.item.currentVersionId,
      );
      if (matchingVersions.length !== 1) {
        throw new Error('工作台缺少唯一的真实当前版本');
      }
      return matchingVersions[0];
    },
    [data.item.currentVersionId, data.versions],
  );
  const selectedIssueSummary = [...data.currentIssues, ...data.historicalIssues].find(
    (issue) => issue.issueId === selectedIssueId,
  );
  const selectedIssueDetail = useIssueDetail(
    props.entryMode,
    {
      projectRefId: props.projectRefId,
      reviewItemId: props.reviewItemId,
      versionId: selectedIssueSummary?.versionId ?? data.currentVersion.versionId,
      issueId: selectedIssueId,
    },
    Boolean(selectedIssueSummary),
  );

  const pending = useMemo(
    () =>
      mutations.appendVersion.isPending ||
      mutations.createIssue.isPending ||
      mutations.resolveIssue.isPending ||
      mutations.reopenIssue.isPending ||
      mutations.finalizeCurrentVersion.isPending ||
      mutations.revokeFinalization.isPending ||
      mutations.downloadFinalizedOriginal.isPending ||
      mutations.editIssue.isPending ||
      mutations.replyToIssue.isPending ||
      mutations.deleteIssue.isPending,
    [mutations],
  );
  const readonlyReason =
    data.project.status === 'archived'
      ? '归档项目只读，恢复后才能执行写操作。'
      : data.item.status === 'finalized' && !data.item.activeFinalizationId
        ? '撤回结果确认中，正在查询权威状态。'
        : undefined;
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
  const issuePanelReadonlyReason =
    writeReadonlyReason === '历史版本只读'
      ? undefined
      : writeReadonlyReason === '当前版本已定稿冻结'
        ? '定稿冻结后意见区只读。'
        : writeReadonlyReason;
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
      optimisticIssue.versionId !== data.item.currentVersionId
    ) {
      return sortedIssuesForPlayback(
        data.currentIssues
          .map((issue) => {
            const loadedIssue =
              selectedIssueDetail.data?.issueId === issue.issueId
                ? selectedIssueDetail.data
                : issue;
            return {
              ...loadedIssue,
              status: optimisticIssueStatuses[issue.issueId] ?? loadedIssue.status,
            };
          })
          .filter((issue) => !issue.deletedAt),
      );
    }
    const currentCopy = data.currentIssues.filter(
      (issue) => !issue.deletedAt && issue.issueId !== optimisticIssue.issueId,
    );
    return sortedIssuesForPlayback(
      [...currentCopy, optimisticIssue].map((issue) => ({
        ...issue,
        status: optimisticIssueStatuses[issue.issueId] ?? issue.status,
      })),
    );
  }, [
    data.currentIssues,
    data.item.currentVersionId,
    optimisticIssue,
    optimisticIssueStatuses,
    props.projectRefId,
    props.reviewItemId,
    selectedIssueDetail.data,
  ]);
  const historicalIssues = useMemo(
    () =>
      data.historicalIssues
        .map((issue) =>
          selectedIssueDetail.data?.issueId === issue.issueId
            ? selectedIssueDetail.data
            : issue,
        )
        .filter((issue) => !issue.deletedAt && issue.versionId !== data.item.currentVersionId),
    [data.historicalIssues, data.item.currentVersionId, selectedIssueDetail.data],
  );
  const selectedVersionIssues = useMemo(
    () =>
      data.currentVersion.versionId === data.item.currentVersionId
        ? currentIssues
        : historicalIssues.filter((issue) => issue.versionId === data.currentVersion.versionId),
    [
      currentIssues,
      data.currentVersion.versionId,
      data.item.currentVersionId,
      historicalIssues,
    ],
  );
  const otherVersionIssues = useMemo(
    () =>
      [...currentIssues, ...historicalIssues].filter(
        (issue) => issue.versionId !== data.currentVersion.versionId,
      ),
    [currentIssues, data.currentVersion.versionId, historicalIssues],
  );
  const currentInput = useMemo(
    () => ({ projectRefId: props.projectRefId, reviewItemId: props.reviewItemId, versionId: data.currentVersion.versionId }),
    [data.currentVersion.versionId, props.projectRefId, props.reviewItemId],
  );
  const selectedIssue =
    [...currentIssues, ...historicalIssues].find((issue) => issue.issueId === selectedIssueId) ?? null;
  const selectedAnnotationSet = selectedIssue?.currentAnnotationSet ?? null;
  const episodeItems = useMemo(() => {
    const projectItems = projectSummary.data?.items ?? [];
    const itemsWithFreshWorkspaceItem = [
      ...projectItems.filter((item) => item.reviewItemId !== data.item.reviewItemId),
      data.item,
    ];
    return dedupeReviewItemsByEpisode(
      itemsWithFreshWorkspaceItem,
      {
        currentItemId: data.item.reviewItemId,
      },
    );
  }, [data.item, projectSummary.data?.items]);
  const episodeUnresolvedCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const item of episodeItems) {
      if (item.reviewItemId === data.item.reviewItemId) {
        counts[item.reviewItemId] = currentIssues.filter(
          (issue) => issue.status === 'unresolved' && !issue.deletedAt,
        ).length;
      } else {
        counts[item.reviewItemId] =
          projectSummary.data?.items.find(
            (candidate) => candidate.reviewItemId === item.reviewItemId,
          )?.unresolvedCurrentVersionCount ?? 0;
      }
    }
    return counts;
  }, [
    currentIssues,
    data.item.reviewItemId,
    episodeItems,
    projectSummary.data?.items,
  ]);
  const episodeCurrentVersionMetadata = useMemo(() => {
    const labels: Record<string, string> = {};
    const fileNames: Record<string, string> = {};
    const durationsMs: Record<string, number> = {};
    for (const item of episodeItems) {
      const versions =
        item.reviewItemId === data.item.reviewItemId
          ? data.versions
          : [];
      const currentVersion = versions.find((version) => version.versionId === item.currentVersionId);
      const summaryItem = projectSummary.data?.items.find(
        (candidate) => candidate.reviewItemId === item.reviewItemId,
      );
      labels[item.reviewItemId] =
        currentVersion?.label ||
        summaryItem?.currentVersion.versionLabel ||
        '-';
      fileNames[item.reviewItemId] =
        currentVersion?.originalMedia.originalFilename ||
        currentVersion?.fileName ||
        summaryItem?.currentVersion.versionLabel ||
        '-';
      durationsMs[item.reviewItemId] =
        currentVersion?.durationMs
        ?? summaryItem?.currentVersion.durationMs
        ?? 0;
    }
    return { labels, fileNames, durationsMs };
  }, [data.item.reviewItemId, data.versions, episodeItems, projectSummary.data?.items]);
  const showToast = useCallback((message: string) => setToast(message), []);
  const showActionError = useCallback((caught: unknown) => showToast(actionError(caught)), [showToast]);
  const reportRevocationError = useCallback(
    (message: string | null) => setToast(message),
    [],
  );
  const refetchProjectSummary = projectSummary.refetch;
  const refreshProjectSummary = useCallback(
    async () => {
      await refetchProjectSummary({ throwOnError: true, cancelRefetch: false });
    },
    [refetchProjectSummary],
  );
  const revocation = useFinalizationRevocationCoordinator({
    projectRefId: props.projectRefId,
    items: projectSummary.data?.items ?? [],
    mutations,
    refreshProjectSummary,
    reportError: reportRevocationError,
    reportMessage: showToast,
  });
  const revokeFinalization = revocation.revokeFinalization;
  const revocationUncertainIds = useMemo(() => {
    const ids = new Set(revocation.revocationUncertainIds);
    if (data.item.status === 'finalized' && !data.item.activeFinalizationId) {
      ids.add(data.item.reviewItemId);
    }
    return ids;
  }, [
    data.item.activeFinalizationId,
    data.item.reviewItemId,
    data.item.status,
    revocation.revocationUncertainIds,
  ]);
  const revokeEpisodeFinalization = useCallback(
    (item: { reviewItemId: string }) => {
      const authoritativeItem = projectSummary.data?.items.find(
        (candidate) => candidate.reviewItemId === item.reviewItemId,
      );
      if (!authoritativeItem) {
        return Promise.reject(new Error('项目剧集权威状态尚未载入，暂时不能撤销定稿。'));
      }
      return revokeFinalization(authoritativeItem);
    },
    [projectSummary.data?.items, revokeFinalization],
  );
  const playback = useReviewWorkspacePlayback({
    projectRefId: props.projectRefId,
    reviewItemId: props.reviewItemId,
    currentVersionId: data.currentVersion.versionId,
    currentItemVersionId: data.item.currentVersionId,
    currentIssues: selectedVersionIssues,
    historicalIssues: otherVersionIssues,
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
      optimisticIssue.versionId !== data.item.currentVersionId ||
      data.currentIssues.some(
        (issue) => issue.issueId === optimisticIssue.issueId && issue.currentRevisionId === optimisticIssue.currentRevisionId,
      )
    ) {
      const frame = window.requestAnimationFrame(() => setOptimisticIssue(null));
      return () => window.cancelAnimationFrame(frame);
    }
  }, [data.currentIssues, data.item.currentVersionId, optimisticIssue]);
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
    authoritativeCurrentVersion,
    currentIssues,
    historicalIssues,
    selectedVersionIssues,
    selectedIssueId,
    selectedAnnotationSet,
    episodeItems,
    episodeUnresolvedCounts,
    episodeCurrentVersionLabels: episodeCurrentVersionMetadata.labels,
    episodeCurrentFileNames: episodeCurrentVersionMetadata.fileNames,
    episodeCurrentDurationsMs: episodeCurrentVersionMetadata.durationsMs,
    responsibleEditors: responsibleEditors.data ?? [],
    mediaMetadata,
    revocationUncertainIds,
    revokeEpisodeFinalization,
    canRevokeEpisodeFinalization: Boolean(projectSummary.data),
    playback,
    actions,
    selectVersion: (versionId: string) => selectVersionParams(versionId, data.item.currentVersionId),
  };
}

export type ReviewWorkspaceController = ReturnType<typeof useReviewWorkspaceController>;
