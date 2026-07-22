import { useCallback, useEffect, useRef, useState, type RefObject } from 'react';
import type { ReviewIssue, ReviewPlaybackTarget } from '../contracts/types';
import type { ReviewPlayerHandle } from '../components/ReviewPlayer';
import { playbackTargetFromIssue } from '../core/playback';

function targetParams(target: ReviewPlaybackTarget, currentVersionId: string): URLSearchParams {
  const params = new URLSearchParams();
  if (target.versionId !== currentVersionId) params.set('version', target.versionId);
  params.set('issue', target.issueId);
  return params;
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}

export function useReviewWorkspacePlayback(input: {
  projectRefId: string;
  reviewItemId: string;
  currentVersionId: string;
  currentItemVersionId: string;
  currentIssues: ReviewIssue[];
  historicalIssues: ReviewIssue[];
  selectedIssueId: string | undefined;
  searchParams: URLSearchParams;
  setSearchParams(next: URLSearchParams, options: { replace: boolean }): void;
  playerRef: RefObject<ReviewPlayerHandle | null>;
  setOptimisticIssue(issue: ReviewIssue | null): void;
}) {
  const {
    projectRefId,
    reviewItemId,
    currentVersionId,
    currentItemVersionId,
    currentIssues,
    historicalIssues,
    selectedIssueId,
    searchParams,
    setSearchParams,
    playerRef,
    setOptimisticIssue,
  } = input;
  const playbackSequenceRef = useRef(0);
  const restoredIssueRef = useRef<string | null>(null);
  const [pendingTarget, setPendingTarget] = useState<ReviewPlaybackTarget | null>(null);
  const [playbackPending, setPlaybackPending] = useState(false);
  const [playbackError, setPlaybackError] = useState<string | null>(null);

  const selectIssue = useCallback(
    (issue: ReviewIssue) => {
      const target = playbackTargetFromIssue(issue);
      if (
        issue.projectRefId === projectRefId &&
        issue.reviewItemId === reviewItemId &&
        issue.versionId === currentVersionId
      ) {
        setOptimisticIssue(issue);
      }
      playbackSequenceRef.current += 1;
      setPendingTarget(target);
      setPlaybackPending(true);
      setPlaybackError(null);
      setSearchParams(targetParams(target, currentItemVersionId), { replace: false });
    },
    [currentItemVersionId, currentVersionId, projectRefId, reviewItemId, setOptimisticIssue, setSearchParams],
  );

  const clearSelectedIssueParam = useCallback(
    (issueId: string) => {
      if (selectedIssueId !== issueId) return;
      const next = new URLSearchParams(searchParams);
      next.delete('issue');
      setSearchParams(next, { replace: true });
      setPendingTarget(null);
      setPlaybackPending(false);
      setPlaybackError(null);
    },
    [searchParams, selectedIssueId, setSearchParams],
  );

  useEffect(() => {
    const key = `${currentVersionId}:${selectedIssueId ?? ''}`;
    if (!selectedIssueId || restoredIssueRef.current === key || pendingTarget) return;
    const issue =
      currentIssues.find((candidate) => candidate.issueId === selectedIssueId) ??
      historicalIssues.find((candidate) => candidate.issueId === selectedIssueId);
    restoredIssueRef.current = key;
    if (!issue) {
      const cleaned = new URLSearchParams(searchParams);
      cleaned.delete('issue');
      queueMicrotask(() => {
        setPlaybackError('URL 中的意见不存在，已清理参数');
        setSearchParams(cleaned, { replace: true });
      });
      return;
    }
    queueMicrotask(() => setPendingTarget(playbackTargetFromIssue(issue)));
  }, [currentIssues, currentVersionId, historicalIssues, pendingTarget, searchParams, selectedIssueId, setSearchParams]);

  useEffect(() => {
    if (!pendingTarget || pendingTarget.versionId !== currentVersionId) return;
    const issue = currentIssues.find((candidate) => candidate.issueId === pendingTarget.issueId);
    if (!issue) {
      queueMicrotask(() => {
        setPlaybackPending(false);
        setPlaybackError('目标意见不属于当前版本');
      });
      return;
    }
    const sequence = playbackSequenceRef.current;
    queueMicrotask(() => setPlaybackPending(true));
    playerRef.current
      ?.playbackToTarget(pendingTarget)
      .then(() => {
        if (sequence !== playbackSequenceRef.current) return;
        setPlaybackPending(false);
        setPlaybackError(null);
        setPendingTarget(null);
      })
      .catch((error: unknown) => {
        if (sequence !== playbackSequenceRef.current || isAbortError(error)) return;
        setPlaybackPending(false);
        setPlaybackError(error instanceof Error ? error.message : '回放定位失败');
      });
  }, [currentIssues, currentVersionId, pendingTarget, playerRef]);

  return {
    pendingTarget,
    playbackPending,
    playbackError,
    setPlaybackError,
    selectIssue,
    clearSelectedIssueParam,
  };
}
