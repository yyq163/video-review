import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type { EntryMode, IssueId, ProjectRefId, ReviewIssue, ReviewItemId, VersionId } from '../contracts/types';
import { settleWithConcurrencyLimit } from '../core/bounded-concurrency';
import { useReviewApi } from './runtime';

const MAX_CONCURRENT_REVIEW_ITEM_DELETES = 5;

export const reviewKeys = {
  projects: ['fj-review', 'projects'] as const,
  projectSummary: (projectRefId: ProjectRefId) =>
    ['fj-review', 'project-summary', projectRefId] as const,
  item: (projectRefId: ProjectRefId, reviewItemId: ReviewItemId) =>
    ['fj-review', 'item', projectRefId, reviewItemId] as const,
  workspace: (projectRefId: ProjectRefId, reviewItemId: ReviewItemId, versionId?: VersionId) =>
    ['fj-review', 'workspace', projectRefId, reviewItemId, versionId ?? 'current'] as const,
  issues: (projectRefId: ProjectRefId, reviewItemId: ReviewItemId, versionId: VersionId) =>
    ['fj-review', 'issues', projectRefId, reviewItemId, versionId] as const,
  issueDetail: (
    projectRefId: ProjectRefId,
    reviewItemId: ReviewItemId,
    versionId: VersionId,
    issueId: IssueId,
  ) => ['fj-review', 'issue-detail', projectRefId, reviewItemId, versionId, issueId] as const,
};

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}

export function useProjects(mode: EntryMode) {
  const api = useReviewApi(mode);
  return useQuery({
    queryKey: reviewKeys.projects,
    queryFn: ({ signal }) => api.listProjects({ signal }),
    retry: (failureCount, error) => !isAbortError(error) && failureCount < 2,
  });
}

export function useProjectSummary(mode: EntryMode, projectRefId: ProjectRefId) {
  const api = useReviewApi(mode);
  return useQuery({
    queryKey: reviewKeys.projectSummary(projectRefId),
    queryFn: ({ signal }) => api.getProjectSummary(projectRefId, { signal }),
    retry: (failureCount, error) => !isAbortError(error) && failureCount < 2,
    refetchInterval: (query) =>
      query.state.data?.items.some(
        (item) => item.revocationCleanupStatus === 'pending',
      )
        ? 3_000
        : false,
  });
}

export function useWorkspace(mode: EntryMode, input: { projectRefId: ProjectRefId; reviewItemId: ReviewItemId; versionId?: VersionId }) {
  const api = useReviewApi(mode);
  return useQuery({
    queryKey: reviewKeys.workspace(input.projectRefId, input.reviewItemId, input.versionId),
    queryFn: ({ signal }) => api.getWorkspace(input, { signal }),
    retry: (failureCount, error) => !isAbortError(error) && failureCount < 2,
    refetchInterval: (query) =>
      query.state.data?.currentVersion.playbackStatus === 'pending'
        ? 5_000
        : false,
  });
}

export function useVersionIssues(
  mode: EntryMode,
  input: {
    projectRefId: ProjectRefId;
    reviewItemId: ReviewItemId;
    versionId: VersionId;
    initialData?: ReviewIssue[];
  },
) {
  const api = useReviewApi(mode);
  return useQuery({
    queryKey: reviewKeys.issues(input.projectRefId, input.reviewItemId, input.versionId),
    queryFn: ({ signal }) => api.getVersionIssues(input, { signal }),
    initialData: input.initialData,
    staleTime: input.initialData ? 5_000 : 0,
    retry: (failureCount, error) => !isAbortError(error) && failureCount < 2,
  });
}

export function useIssueDetail(
  mode: EntryMode,
  input: {
    projectRefId: ProjectRefId;
    reviewItemId: ReviewItemId;
    versionId: VersionId;
    issueId?: IssueId;
  },
  enabled: boolean,
) {
  const api = useReviewApi(mode);
  const issueId = input.issueId ?? 'not-selected';
  return useQuery({
    queryKey: reviewKeys.issueDetail(
      input.projectRefId,
      input.reviewItemId,
      input.versionId,
      issueId,
    ),
    queryFn: ({ signal }) => {
      if (!input.issueId) throw new Error('意见 ID 缺失');
      return api.getIssueDetail({ ...input, issueId: input.issueId }, { signal });
    },
    enabled: enabled && Boolean(input.issueId),
    retry: (failureCount, error) => !isAbortError(error) && failureCount < 2,
  });
}

export function useReviewMutations(mode: EntryMode) {
  const api = useReviewApi(mode);
  const queryClient = useQueryClient();
  const context = () => api.entryPolicy.createContext(mode);
  const invalidateProject = (projectRefId: ProjectRefId) =>
    queryClient.invalidateQueries({ queryKey: reviewKeys.projectSummary(projectRefId) });
  const invalidateWorkspace = (projectRefId: ProjectRefId, reviewItemId: ReviewItemId, versionId?: VersionId) =>
    queryClient.invalidateQueries({ queryKey: reviewKeys.workspace(projectRefId, reviewItemId, versionId) });
  const invalidateCurrentWorkspace = (projectRefId: ProjectRefId, reviewItemId: ReviewItemId) =>
    queryClient.invalidateQueries({ queryKey: reviewKeys.workspace(projectRefId, reviewItemId) });
  const invalidateIssueQueries = (issue: {
    projectRefId: ProjectRefId;
    reviewItemId: ReviewItemId;
    versionId: VersionId;
    issueId: IssueId;
  }) =>
    Promise.all([
      queryClient.invalidateQueries({
        queryKey: reviewKeys.issues(issue.projectRefId, issue.reviewItemId, issue.versionId),
      }),
      queryClient.invalidateQueries({
        queryKey: reviewKeys.issueDetail(
          issue.projectRefId,
          issue.reviewItemId,
          issue.versionId,
          issue.issueId,
        ),
      }),
    ]);
  const refreshInBackground = (...refreshes: Promise<unknown>[]) => {
    void Promise.allSettled(refreshes);
  };

  return {
    createProject: useMutation({
      mutationFn: (input: Parameters<typeof api.createProject>[0]) => api.createProject(input, context()),
      onSuccess: () => queryClient.invalidateQueries({ queryKey: reviewKeys.projects }),
    }),
    updateProject: useMutation({
      mutationFn: (input: Parameters<typeof api.updateProject>[0]) => api.updateProject(input, context()),
      onSuccess: async (project) => {
        await Promise.all([
          invalidateProject(project.projectRefId),
          queryClient.invalidateQueries({ queryKey: reviewKeys.projects }),
        ]);
      },
    }),
    archiveProject: useMutation({
      mutationFn: (input: Parameters<typeof api.archiveProject>[0]) => api.archiveProject(input, context()),
      onSuccess: (project) => {
        invalidateProject(project.projectRefId);
        queryClient.invalidateQueries({ queryKey: reviewKeys.projects });
      },
    }),
    restoreProject: useMutation({
      mutationFn: (input: Parameters<typeof api.restoreProject>[0]) => api.restoreProject(input, context()),
      onSuccess: (project) => {
        invalidateProject(project.projectRefId);
        queryClient.invalidateQueries({ queryKey: reviewKeys.projects });
      },
    }),
    deleteProject: useMutation({
      mutationFn: (input: Parameters<typeof api.deleteProject>[0]) => api.deleteProject(input, context()),
      onSuccess: (project) => {
        invalidateProject(project.projectRefId);
        queryClient.invalidateQueries({ queryKey: reviewKeys.projects });
      },
    }),
    createReviewItemWithVersion: useMutation({
      mutationFn: (input: Parameters<typeof api.createReviewItemWithVersion>[0]) =>
        api.createReviewItemWithVersion(input, context()),
      onSuccess: ({ item }) => {
        refreshInBackground(
          invalidateProject(item.projectRefId),
          invalidateCurrentWorkspace(item.projectRefId, item.reviewItemId),
        );
      },
    }),
    updateReviewItem: useMutation({
      mutationFn: (input: Parameters<typeof api.updateReviewItem>[0]) => api.updateReviewItem(input, context()),
      onSuccess: async (item) => {
        await Promise.all([
          invalidateProject(item.projectRefId),
          invalidateCurrentWorkspace(item.projectRefId, item.reviewItemId),
        ]);
      },
    }),
    deleteReviewItem: useMutation({
      mutationFn: (input: Parameters<typeof api.deleteReviewItem>[0]) => api.deleteReviewItem(input, context()),
      onSuccess: (item) => {
        invalidateProject(item.projectRefId);
        queryClient.removeQueries({ queryKey: reviewKeys.item(item.projectRefId, item.reviewItemId) });
        queryClient.removeQueries({ queryKey: reviewKeys.workspace(item.projectRefId, item.reviewItemId) });
      },
    }),
    bulkDeleteReviewItems: useMutation({
      mutationFn: async (inputs: Array<Parameters<typeof api.deleteReviewItem>[0]>) => {
        const projectRefId = inputs[0]?.projectRefId ?? null;
        if (projectRefId && inputs.some((input) => input.projectRefId !== projectRefId)) {
          throw new Error('批量删除只能处理同一个项目内的分集。');
        }
        const results = await settleWithConcurrencyLimit(
          inputs,
          MAX_CONCURRENT_REVIEW_ITEM_DELETES,
          (input) => api.deleteReviewItem(input, context()),
        );
        return { inputs, projectRefId, results };
      },
      onSuccess: ({ projectRefId, results }) => {
        for (const result of results) {
          if (result.status !== 'fulfilled') continue;
          const item = result.value;
          queryClient.removeQueries({ queryKey: reviewKeys.item(item.projectRefId, item.reviewItemId) });
          queryClient.removeQueries({ queryKey: reviewKeys.workspace(item.projectRefId, item.reviewItemId) });
        }
        if (projectRefId) refreshInBackground(invalidateProject(projectRefId));
      },
    }),
    appendVersion: useMutation({
      mutationFn: (input: Parameters<typeof api.appendVersion>[0]) => api.appendVersion(input, context()),
      onSuccess: (version) => {
        refreshInBackground(
          invalidateProject(version.projectRefId),
          invalidateCurrentWorkspace(version.projectRefId, version.reviewItemId),
          invalidateWorkspace(version.projectRefId, version.reviewItemId, version.versionId),
        );
      },
    }),
    createIssue: useMutation({
      mutationFn: (input: Parameters<typeof api.createIssue>[0]) => api.createIssue(input, context()),
      onSuccess: (issue) => {
        invalidateProject(issue.projectRefId);
        invalidateCurrentWorkspace(issue.projectRefId, issue.reviewItemId);
        invalidateWorkspace(issue.projectRefId, issue.reviewItemId, issue.versionId);
        invalidateIssueQueries(issue);
      },
    }),
    replyToIssue: useMutation({
      mutationFn: (input: Parameters<typeof api.replyToIssue>[0]) => api.replyToIssue(input, context()),
      onSuccess: (issue) => {
        invalidateCurrentWorkspace(issue.projectRefId, issue.reviewItemId);
        invalidateWorkspace(issue.projectRefId, issue.reviewItemId, issue.versionId);
        invalidateIssueQueries(issue);
      },
    }),
    editIssue: useMutation({
      mutationFn: (input: Parameters<typeof api.editIssue>[0]) => api.editIssue(input, context()),
      onSuccess: (issue) => {
        invalidateProject(issue.projectRefId);
        invalidateCurrentWorkspace(issue.projectRefId, issue.reviewItemId);
        invalidateWorkspace(issue.projectRefId, issue.reviewItemId, issue.versionId);
        invalidateIssueQueries(issue);
      },
    }),
    resolveIssue: useMutation({
      mutationFn: (input: Parameters<typeof api.resolveIssue>[0]) => api.resolveIssue(input, context()),
      onSuccess: (issue) => {
        invalidateProject(issue.projectRefId);
        invalidateCurrentWorkspace(issue.projectRefId, issue.reviewItemId);
        invalidateWorkspace(issue.projectRefId, issue.reviewItemId, issue.versionId);
        invalidateIssueQueries(issue);
      },
    }),
    reopenIssue: useMutation({
      mutationFn: (input: Parameters<typeof api.reopenIssue>[0]) => api.reopenIssue(input, context()),
      onSuccess: (issue) => {
        invalidateProject(issue.projectRefId);
        invalidateCurrentWorkspace(issue.projectRefId, issue.reviewItemId);
        invalidateWorkspace(issue.projectRefId, issue.reviewItemId, issue.versionId);
        invalidateIssueQueries(issue);
      },
    }),
    deleteIssue: useMutation({
      mutationFn: (input: Parameters<typeof api.deleteIssue>[0]) => api.deleteIssue(input, context()),
      onSuccess: (issue) => {
        invalidateProject(issue.projectRefId);
        invalidateCurrentWorkspace(issue.projectRefId, issue.reviewItemId);
        invalidateWorkspace(issue.projectRefId, issue.reviewItemId, issue.versionId);
        invalidateIssueQueries(issue);
      },
    }),
    finalizeCurrentVersion: useMutation({
      mutationFn: (input: Parameters<typeof api.finalizeCurrentVersion>[0]) => api.finalizeCurrentVersion(input, context()),
      onSuccess: (finalization) => {
        invalidateProject(finalization.projectRefId);
        invalidateCurrentWorkspace(finalization.projectRefId, finalization.reviewItemId);
        invalidateWorkspace(finalization.projectRefId, finalization.reviewItemId, finalization.versionId);
      },
    }),
    revokeFinalization: useMutation({
      mutationFn: (input: Parameters<typeof api.revokeFinalization>[0]) =>
        api.revokeFinalization(input, context()),
      onSuccess: (revocation) => {
        refreshInBackground(
          invalidateProject(revocation.reviewItem.projectRefId),
          invalidateCurrentWorkspace(
            revocation.reviewItem.projectRefId,
            revocation.reviewItem.reviewItemId,
          ),
          queryClient.invalidateQueries({ queryKey: reviewKeys.projects }),
        );
      },
    }),
    downloadFinalizedOriginal: useMutation({
      mutationFn: (input: Parameters<typeof api.downloadFinalizedOriginal>[0]) =>
        api.downloadFinalizedOriginal(input, context()),
    }),
    createProjectFinalizedPackage: useMutation({
      mutationFn: (projectRefId: ProjectRefId) => api.createProjectFinalizedPackage(projectRefId, context()),
    }),
    downloadProjectFinalizedPackage: useMutation({
      mutationFn: (result: Parameters<typeof api.downloadProjectFinalizedPackage>[0]) =>
        api.downloadProjectFinalizedPackage(result, context()),
    }),
  };
}
