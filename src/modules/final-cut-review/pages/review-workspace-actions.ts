import type { Dispatch, RefObject, SetStateAction } from 'react';
import type { ReviewAnnotationShape, ReviewIssue, ReviewVersion, ReviewWorkspace, UploadProgress } from '../contracts/types';
import type { ReviewPlayerHandle } from '../components/ReviewPlayer';
import type { useReviewMutations } from '../entry/use-review-queries';
import {
  clearAppendVersionConfirmationRequired,
  getAppendVersionProtectionState,
  type AppendVersionProtectionState,
} from '../adapters/http-review-uploads';

type ReviewMutations = ReturnType<typeof useReviewMutations>;
type CurrentInput = { projectRefId: string; reviewItemId: string; versionId: string };
export type AppendVersionInput = { file: File; versionNote: string; changeSummary: string };

function isSameAppendVersionInput(current: AppendVersionInput, pending: AppendVersionInput): boolean {
  return current.file === pending.file &&
    current.versionNote === pending.versionNote &&
    current.changeSummary === pending.changeSummary;
}

export function useReviewWorkspaceActions(input: {
  projectRefId: string;
  reviewItemId: string;
  data: ReviewWorkspace;
  mutations: ReviewMutations;
  currentInput: CurrentInput;
  playerRef: RefObject<ReviewPlayerHandle | null>;
  timeMs: number;
  draftShapes: ReviewAnnotationShape[];
  setDraftShapes: Dispatch<SetStateAction<ReviewAnnotationShape[]>>;
  setOptimisticIssue: Dispatch<SetStateAction<ReviewIssue | null>>;
  setUploadProgress: Dispatch<SetStateAction<UploadProgress | undefined>>;
  appendVersionProtectionState: AppendVersionProtectionState;
  setAppendVersionProtectionState: Dispatch<SetStateAction<AppendVersionProtectionState>>;
  appendVersionRetry: AppendVersionInput | null;
  setAppendVersionRetry: Dispatch<SetStateAction<AppendVersionInput | null>>;
  setAppendVersionConfirmationPending: Dispatch<SetStateAction<boolean>>;
  refetchWorkspace(): Promise<{ data?: ReviewWorkspace }>;
  showToast(message: string): void;
  showActionError(error: unknown): void;
  selectIssue(issue: ReviewIssue): void;
  clearSelectedIssueParam(issueId: string): void;
  selectVersionParams(versionId: string, currentVersionId: string): void;
}) {
  const createIssue = async (body: string) => {
    const snapshot = input.playerRef.current?.snapshot();
    try {
      const issue = await input.mutations.createIssue.mutateAsync({
        ...input.currentInput,
        timestampMs: snapshot?.timestampMs ?? input.timeMs,
        frameNumber: snapshot?.frameNumber ?? 0,
        body,
        severity: 'normal',
        shapes: input.draftShapes,
        canvasWidth: snapshot?.canvasWidth ?? 1280,
        canvasHeight: snapshot?.canvasHeight ?? 720,
        videoWidth: snapshot?.videoWidth ?? input.data.currentVersion.width,
        videoHeight: snapshot?.videoHeight ?? input.data.currentVersion.height,
      });
      input.showToast('意见已提交到当前版本。');
      input.playerRef.current?.clearDraft();
      input.setDraftShapes([]);
      input.selectIssue(issue);
    } catch (error) {
      input.showActionError(error);
      throw error;
    }
  };

  const finalize = () => {
    const confirmed = window.confirm('确认将当前版本定稿？定稿后当前版本将冻结，且不可撤销。');
    if (!confirmed) return;
    input.mutations.finalizeCurrentVersion.mutate(
      { ...input.currentInput, confirmed: true },
      { onError: input.showActionError, onSuccess: () => input.showToast('当前版本已精确定稿冻结。') },
    );
  };

  const download = () =>
    input.mutations.downloadFinalizedOriginal.mutate(
      { projectRefId: input.projectRefId, reviewItemId: input.reviewItemId },
      { onError: input.showActionError, onSuccess: () => input.showToast('已触发单片定稿原片下载。') },
    );

  const packageProject = () => {
    const preparePackage = () =>
      input.mutations.createProjectFinalizedPackage.mutate(input.projectRefId, {
        onError: input.showActionError,
        onSuccess: (result) => input.showToast(`项目包下载就绪，包含 ${result.entries.length} 个定稿原片。`),
      });
    if (input.mutations.downloadProjectFinalizedPackage.isError) {
      input.mutations.downloadProjectFinalizedPackage.reset();
      input.mutations.createProjectFinalizedPackage.reset();
      preparePackage();
      return;
    }
    const preparedPackage = input.mutations.createProjectFinalizedPackage.data;
    if (preparedPackage) {
      input.mutations.downloadProjectFinalizedPackage.mutate(preparedPackage, {
        onError: (error) => {
          input.mutations.createProjectFinalizedPackage.reset();
          input.showActionError(error);
        },
        onSuccess: () => {
          input.mutations.createProjectFinalizedPackage.reset();
          input.showToast('已触发项目定稿包下载。');
        },
      });
      return;
    }
    preparePackage();
  };

  const appendVersion = async (versionInput: AppendVersionInput) => {
    const retryAllowed =
      input.appendVersionProtectionState === 'required' &&
      input.appendVersionRetry !== null &&
      isSameAppendVersionInput(versionInput, input.appendVersionRetry);
    if (input.appendVersionProtectionState !== 'clear' && !retryAllowed) {
      const error = new Error('上一笔版本追加结果尚未确认。请刷新并核对版本列表后再提交新版本。');
      input.showActionError(error);
      throw error;
    }
    input.setUploadProgress({ stage: 'validating', percent: 0, totalBytes: versionInput.file.size });
    let version: ReviewVersion;
    try {
      version = await input.mutations.appendVersion.mutateAsync({
        ...versionInput,
        projectRefId: input.projectRefId,
        reviewItemId: input.reviewItemId,
        onProgress: input.setUploadProgress,
      });
    } catch (error) {
      input.setUploadProgress(undefined);
      const nextProtectionState = getAppendVersionProtectionState(input.projectRefId, input.reviewItemId);
      input.setAppendVersionProtectionState(nextProtectionState);
      input.setAppendVersionRetry(nextProtectionState === 'required' ? versionInput : null);
      input.showActionError(error);
      throw error;
    }

    clearAppendVersionConfirmationRequired(input.projectRefId, input.reviewItemId);
    input.setAppendVersionProtectionState(
      getAppendVersionProtectionState(input.projectRefId, input.reviewItemId),
    );
    input.setAppendVersionRetry(null);
    input.setUploadProgress({
      stage: 'completed',
      percent: 100,
      bytesSent: versionInput.file.size,
      totalBytes: versionInput.file.size,
    });
    try {
      const refreshed = await input.refetchWorkspace();
      if (!refreshed.data?.versions.some((candidate) => candidate.versionId === version.versionId)) {
        input.showToast('文件已上传成功，待审列表暂时刷新失败，请刷新页面查看。');
      } else {
        input.showToast(`${version.label} 已追加，旧版本意见不会继承。`);
        input.selectVersionParams(version.versionId, version.versionId);
      }
    } catch {
      input.showToast('文件已上传成功，待审列表暂时刷新失败，请刷新页面查看。');
    } finally {
      input.setUploadProgress(undefined);
    }
  };

  const confirmAppendVersionList = async () => {
    input.setAppendVersionConfirmationPending(true);
    try {
      const refreshed = await input.refetchWorkspace();
      if (!refreshed.data) throw new Error('版本列表刷新未返回数据。');
      clearAppendVersionConfirmationRequired(input.projectRefId, input.reviewItemId);
      const nextProtectionState = getAppendVersionProtectionState(input.projectRefId, input.reviewItemId);
      input.setAppendVersionProtectionState(nextProtectionState);
      if (nextProtectionState !== 'clear') {
        throw new Error('浏览器会话存储不可用，无法解除版本追加保护。');
      }
      input.setAppendVersionRetry(null);
      input.showToast('已刷新并确认当前版本列表，可以继续追加版本。');
    } catch (error) {
      input.setAppendVersionProtectionState(
        getAppendVersionProtectionState(input.projectRefId, input.reviewItemId),
      );
      input.showActionError(error);
    } finally {
      input.setAppendVersionConfirmationPending(false);
    }
  };

  const editIssue = async (issue: ReviewIssue, body: string) => {
    const snapshot = input.playerRef.current?.snapshot();
    const shapes = input.draftShapes.length ? input.draftShapes : (issue.currentAnnotationSet?.shapes ?? []);
    try {
      const updatedIssue = await input.mutations.editIssue.mutateAsync({
        ...input.currentInput,
        issueId: issue.issueId,
        timestampMs: snapshot?.timestampMs ?? issue.timestampMs,
        frameNumber: snapshot?.frameNumber ?? issue.frameNumber,
        body,
        shapes,
        canvasWidth: snapshot?.canvasWidth ?? issue.currentAnnotationSet?.canvasWidth ?? 1280,
        canvasHeight: snapshot?.canvasHeight ?? issue.currentAnnotationSet?.canvasHeight ?? 720,
        videoWidth: snapshot?.videoWidth ?? issue.currentAnnotationSet?.videoWidth ?? input.data.currentVersion.width,
        videoHeight: snapshot?.videoHeight ?? issue.currentAnnotationSet?.videoHeight ?? input.data.currentVersion.height,
      });
      input.showToast('意见已保存为新修订。');
      input.playerRef.current?.clearDraft();
      input.setDraftShapes([]);
      input.selectIssue(updatedIssue);
    } catch (error) {
      input.showActionError(error);
      throw error;
    }
  };

  const replyIssue = async (issue: ReviewIssue, body: string) => {
    try {
      await input.mutations.replyToIssue.mutateAsync({ ...input.currentInput, issueId: issue.issueId, body });
      input.showToast('回复已提交。');
    } catch (error) {
      input.showActionError(error);
      throw error;
    }
  };
  const resolveIssue = (issue: ReviewIssue) =>
    input.mutations.resolveIssue.mutate({ ...input.currentInput, issueId: issue.issueId }, { onError: input.showActionError });
  const reopenIssue = (issue: ReviewIssue) =>
    input.mutations.reopenIssue.mutate({ ...input.currentInput, issueId: issue.issueId }, { onError: input.showActionError });
  const deleteIssue = (issue: ReviewIssue) =>
    input.mutations.deleteIssue.mutate(
      { ...input.currentInput, issueId: issue.issueId },
      {
        onError: input.showActionError,
        onSuccess: (deletedIssue) => {
          input.clearSelectedIssueParam(deletedIssue.issueId);
          input.setOptimisticIssue(null);
          input.showToast('意见已删除。');
        },
      },
    );

  return {
    createIssue,
    finalize,
    download,
    packageProject,
    appendVersion,
    confirmAppendVersionList,
    editIssue,
    replyIssue,
    resolveIssue,
    reopenIssue,
    deleteIssue,
  };
}
