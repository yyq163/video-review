import { useCallback } from 'react';
import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from '@tanstack/react-query';
import type { EntryMode, IssueId, Project, ProjectRefId, ReviewIssue, ReviewItemId, ReviewWorkspace, VersionId } from '../contracts/types';
import { settleWithConcurrencyLimit } from '../core/bounded-concurrency';
import {
  clearRevokeFinalizationOperation,
  getPendingRevokeFinalizationOperation,
  hasPendingRevokeFinalizationOperation,
  hasPendingRevokeFinalizationOperationForProject,
  REVOKE_FINALIZATION_RECONCILIATION_INTERVAL_MS,
} from '../adapters/http-review-finalization-operation';
import type {
  ReviewItemWithMetadata,
  ReviewProjectSummary,
  ReviewProjectSummaryItem,
} from '../ports';
import { useReviewApi } from './runtime';

const MAX_CONCURRENT_REVIEW_ITEM_DELETES = 5;

export const reviewKeys = {
  projects: ['fj-review', 'projects'] as const,
  projectSummary: (projectRefId: ProjectRefId) =>
    ['fj-review', 'project-summary', projectRefId] as const,
  item: (projectRefId: ProjectRefId, reviewItemId: ReviewItemId) =>
    ['fj-review', 'item', projectRefId, reviewItemId] as const,
  revocationAuthority: (
    projectRefId: ProjectRefId,
    reviewItemId: ReviewItemId,
    commandId: string,
  ) =>
    ['fj-review', 'revocation-authority', projectRefId, reviewItemId, commandId] as const,
  revocationAuthorityItem: (projectRefId: ProjectRefId, reviewItemId: ReviewItemId) =>
    ['fj-review', 'revocation-authority', projectRefId, reviewItemId] as const,
  workspace: (projectRefId: ProjectRefId, reviewItemId: ReviewItemId, versionId?: VersionId) =>
    ['fj-review', 'workspace', projectRefId, reviewItemId, versionId ?? 'current'] as const,
  workspaceItem: (projectRefId: ProjectRefId, reviewItemId: ReviewItemId) =>
    ['fj-review', 'workspace', projectRefId, reviewItemId] as const,
  versionStatus: (projectRefId: ProjectRefId, reviewItemId: ReviewItemId, versionId: VersionId) =>
    ['fj-review', 'version-status', projectRefId, reviewItemId, versionId] as const,
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

function maskProtectedProject(project: Project): Project {
  return hasPendingRevokeFinalizationOperationForProject(project.projectRefId)
    ? { ...project, completionStatus: 'in_progress' }
    : project;
}

function maskProtectedSummary(summary: ReviewProjectSummary): ReviewProjectSummary {
  return {
    ...summary,
    project: maskProtectedProject(summary.project),
    items: summary.items.map((item) =>
      hasPendingRevokeFinalizationOperation(item.projectRefId, item.reviewItemId)
        ? {
            ...item,
            activeFinalizationId: null,
            bulkDelete: {
              eligible: false,
              locked: true,
              reason: 'finalization_history',
            },
            revocationCleanupStatus: 'pending',
          }
        : item,
    ),
  };
}

function maskProtectedWorkspace(workspace: ReviewWorkspace): ReviewWorkspace {
  return hasPendingRevokeFinalizationOperation(
    workspace.item.projectRefId,
    workspace.item.reviewItemId,
  )
    ? {
        ...workspace,
        project: maskProtectedProject(workspace.project),
        item: {
          ...workspace.item,
          activeFinalizationId: null,
        },
        activeFinalization: null,
      }
    : workspace;
}

function syncReviewItemAuthorityCaches(
  queryClient: QueryClient,
  item: ReviewItemWithMetadata,
): void {
  queryClient.setQueryData<ReviewProjectSummary>(
    reviewKeys.projectSummary(item.projectRefId),
    (summary) => {
      if (!summary) return summary;
      return {
        ...summary,
        items: summary.items.map((cached) =>
          cached.reviewItemId === item.reviewItemId
            ? {
                ...cached,
                ...item,
                finalization:
                  cached.finalization && !item.activeFinalizationId
                    ? {
                        ...cached.finalization,
                        status: 'revoked' as const,
                        revokedAt: item.updatedAt,
                      }
                    : cached.finalization,
                bulkDelete: !item.activeFinalizationId
                  ? {
                      eligible: false,
                      locked: true,
                      reason: 'finalization_history',
                    }
                  : cached.bulkDelete,
                revocationCleanupStatus: !item.activeFinalizationId
                  ? 'pending' as const
                  : cached.revocationCleanupStatus,
              }
            : cached,
        ),
      };
    },
  );
  queryClient.setQueriesData<ReviewWorkspace>(
    { queryKey: reviewKeys.workspaceItem(item.projectRefId, item.reviewItemId) },
    (workspace) => {
      if (!workspace) return workspace;
      return {
        ...workspace,
        item: { ...workspace.item, ...item },
        activeFinalization: !item.activeFinalizationId
          ? null
          : workspace.activeFinalization,
        currentVersion: {
          ...workspace.currentVersion,
          status: item.status,
        },
        versions: workspace.versions.map((version) =>
          version.versionId === item.currentVersionId
            ? { ...version, status: item.status }
            : version,
        ),
      };
    },
  );
  queryClient.setQueryData<Project[]>(reviewKeys.projects, (projects) =>
    projects?.map((project) =>
      project.projectRefId === item.projectRefId && !item.activeFinalizationId
        ? { ...project, completionStatus: 'in_progress' as const }
        : project,
    ),
  );
  void Promise.allSettled([
    queryClient.invalidateQueries({
      queryKey: reviewKeys.projectSummary(item.projectRefId),
    }),
    queryClient.invalidateQueries({
      queryKey: reviewKeys.workspaceItem(item.projectRefId, item.reviewItemId),
    }),
    queryClient.invalidateQueries({ queryKey: reviewKeys.projects }),
  ]);
}

export function useProjects(mode: EntryMode) {
  const api = useReviewApi(mode);
  const projects = useQuery({
    queryKey: reviewKeys.projects,
    queryFn: ({ signal }) => api.listProjects({ signal }),
    retry: (failureCount, error) => !isAbortError(error) && failureCount < 2,
    staleTime: 5_000,
  });
  return {
    ...projects,
    data: projects.data?.map(maskProtectedProject),
  };
}

export function useProjectSummary(mode: EntryMode, projectRefId: ProjectRefId) {
  const api = useReviewApi(mode);
  const summary = useQuery({
    queryKey: reviewKeys.projectSummary(projectRefId),
    queryFn: ({ signal }) => api.getProjectSummary(projectRefId, { signal }),
    retry: (failureCount, error) => !isAbortError(error) && failureCount < 2,
    staleTime: 5_000,
    refetchInterval: (query) =>
      query.state.data?.items.some(
        (item) =>
          item.revocationCleanupStatus === 'pending' ||
          item.revocationCleanupStatus === 'failed' ||
          item.currentVersion.playbackStatus === 'pending' ||
          item.currentVersion.thumbnailStatus === 'pending',
      )
        ? 5_000
        : false,
  });
  return {
    ...summary,
    data: summary.data ? maskProtectedSummary(summary.data) : summary.data,
  };
}

export function useWorkspace(mode: EntryMode, input: { projectRefId: ProjectRefId; reviewItemId: ReviewItemId; versionId?: VersionId }) {
  const api = useReviewApi(mode);
  const queryClient = useQueryClient();
  const pendingRevocationOperation = getPendingRevokeFinalizationOperation(
    input.projectRefId,
    input.reviewItemId,
  );
  const revocationProtectionPending = hasPendingRevokeFinalizationOperation(
    input.projectRefId,
    input.reviewItemId,
  );
  const workspace = useQuery({
    queryKey: reviewKeys.workspace(input.projectRefId, input.reviewItemId, input.versionId),
    queryFn: async ({ signal }) => {
      const expectedOperation = getPendingRevokeFinalizationOperation(
        input.projectRefId,
        input.reviewItemId,
      );
      const protectedWithoutIdentity =
        !expectedOperation &&
        hasPendingRevokeFinalizationOperation(input.projectRefId, input.reviewItemId);
      const result = await api.getWorkspace(input, { signal });
      const currentOperation = getPendingRevokeFinalizationOperation(
        input.projectRefId,
        input.reviewItemId,
      );
      const currentlyProtected = hasPendingRevokeFinalizationOperation(
        input.projectRefId,
        input.reviewItemId,
      );
      const protectionIdentityMatches = expectedOperation
        ? currentOperation?.commandId === expectedOperation.commandId
        : protectedWithoutIdentity
          ? currentOperation === null && currentlyProtected
          : !currentlyProtected;
      if (!protectionIdentityMatches) {
        throw new DOMException(
          'Ignored workspace response from a superseded revocation identity',
          'AbortError',
        );
      }
      if (
        (expectedOperation || protectedWithoutIdentity) &&
        (result.item.status !== 'finalized' || !result.item.activeFinalizationId)
      ) {
        clearRevokeFinalizationOperation(
          input.projectRefId,
          input.reviewItemId,
          expectedOperation?.commandId ?? null,
        );
      }
      return result;
    },
    retry: (failureCount, error) => !isAbortError(error) && failureCount < 2,
    staleTime: 5_000,
  });
  const revocationAuthority = useQuery({
    queryKey: reviewKeys.revocationAuthority(
      input.projectRefId,
      input.reviewItemId,
      pendingRevocationOperation?.commandId ?? 'storage-unavailable',
    ),
    queryFn: async ({ signal }) => {
      const item = await api.getReviewItem(input, { signal });
      if (item.status !== 'finalized' || !item.activeFinalizationId) {
        const cleared = clearRevokeFinalizationOperation(
          input.projectRefId,
          input.reviewItemId,
          pendingRevocationOperation?.commandId ?? null,
        );
        if (cleared) syncReviewItemAuthorityCaches(queryClient, item);
      }
      return item;
    },
    enabled: revocationProtectionPending,
    retry: (failureCount, error) => !isAbortError(error) && failureCount < 2,
    staleTime: 0,
    refetchInterval: revocationProtectionPending
      ? REVOKE_FINALIZATION_RECONCILIATION_INTERVAL_MS
      : false,
  });
  const versionId = input.versionId ?? workspace.data?.item.currentVersionId;
  const versionStatus = useQuery({
    queryKey: reviewKeys.versionStatus(
      input.projectRefId,
      input.reviewItemId,
      versionId ?? ('not-loaded' as VersionId),
    ),
    queryFn: ({ signal }) => {
      if (!versionId) throw new Error('版本 ID 缺失');
      return api.getVersion({ ...input, versionId }, { signal });
    },
    enabled:
      Boolean(versionId) &&
      (workspace.data?.currentVersion.playbackStatus === 'pending' ||
        workspace.data?.currentVersion.thumbnailStatus === 'pending'),
    retry: (failureCount, error) => !isAbortError(error) && failureCount < 2,
    staleTime: 5_000,
    refetchInterval: (query) =>
      query.state.data?.playbackStatus === 'pending' ||
      query.state.data?.thumbnailStatus === 'pending'
        ? 5_000
        : false,
  });
  const refreshedVersion = versionStatus.data;
  const mergedWorkspace =
    workspace.data &&
    refreshedVersion?.versionId === workspace.data.currentVersion.versionId
      ? {
          ...workspace.data,
          currentVersion: {
            ...workspace.data.currentVersion,
            playbackStatus: refreshedVersion.playbackStatus,
            playbackAssetId: refreshedVersion.playbackAssetId,
            playbackUrl: refreshedVersion.playbackUrl,
            thumbnailStatus: refreshedVersion.thumbnailStatus,
            thumbnailAssetId: refreshedVersion.thumbnailAssetId,
            thumbnailUrl: refreshedVersion.thumbnailUrl,
          },
          versions: workspace.data.versions.map((version) =>
            version.versionId === refreshedVersion.versionId
              ? {
                  ...version,
                  playbackStatus: refreshedVersion.playbackStatus,
                  playbackAssetId: refreshedVersion.playbackAssetId,
                  playbackUrl: refreshedVersion.playbackUrl,
                  thumbnailStatus: refreshedVersion.thumbnailStatus,
                  thumbnailAssetId: refreshedVersion.thumbnailAssetId,
                  thumbnailUrl: refreshedVersion.thumbnailUrl,
                }
              : version,
          ),
        }
      : workspace.data;
  const authoritativeItem = revocationAuthority.data;
  const authorityMergedWorkspace =
    mergedWorkspace &&
    revocationProtectionPending &&
    authoritativeItem &&
    (authoritativeItem.status !== 'finalized' ||
      !authoritativeItem.activeFinalizationId)
      ? {
          ...mergedWorkspace,
          item: { ...mergedWorkspace.item, ...authoritativeItem },
          activeFinalization: null,
          currentVersion: {
            ...mergedWorkspace.currentVersion,
            status: authoritativeItem.status,
          },
          versions: mergedWorkspace.versions.map((version) =>
            version.versionId === authoritativeItem.currentVersionId
              ? { ...version, status: authoritativeItem.status }
              : version,
          ),
        }
      : mergedWorkspace;
  return {
    ...workspace,
    data: authorityMergedWorkspace
      ? maskProtectedWorkspace(authorityMergedWorkspace)
      : authorityMergedWorkspace,
  };
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
  const getReviewItemAuthority = useCallback(
    (
      input: Parameters<typeof api.getReviewItem>[0],
      options?: Parameters<typeof api.getReviewItem>[1],
    ) => api.getReviewItem(input, options),
    [api],
  );
  const syncReviewItemAuthority = useCallback(
    (item: ReviewItemWithMetadata) => {
      syncReviewItemAuthorityCaches(queryClient, item);
    },
    [queryClient],
  );
  const lockReviewItemRevocationUncertain = useCallback(
    (item: ReviewProjectSummaryItem) => {
      queryClient.setQueryData<ReviewProjectSummary>(
        reviewKeys.projectSummary(item.projectRefId),
        (summary) => {
          if (!summary) return summary;
          return {
            ...summary,
            items: summary.items.map((cached) =>
              cached.reviewItemId === item.reviewItemId
                ? {
                    ...cached,
                    activeFinalizationId: null,
                    bulkDelete: {
                      eligible: false,
                      locked: true,
                      reason: 'finalization_history',
                    },
                    revocationCleanupStatus: 'pending' as const,
                  }
                : cached,
            ),
          };
        },
      );
      queryClient.setQueriesData<ReviewWorkspace>(
        { queryKey: reviewKeys.workspaceItem(item.projectRefId, item.reviewItemId) },
        (workspace) => {
          if (!workspace) return workspace;
          return {
            ...workspace,
            item: {
              ...workspace.item,
              activeFinalizationId: null,
            },
            activeFinalization: null,
          };
        },
      );
      queryClient.setQueryData<Project[]>(reviewKeys.projects, (projects) =>
        projects?.map((project) =>
          project.projectRefId === item.projectRefId
            ? { ...project, completionStatus: 'in_progress' as const }
            : project,
        ),
      );
    },
    [queryClient],
  );
  const context = () => api.entryPolicy.createContext(mode);
  const invalidateProject = (projectRefId: ProjectRefId) =>
    queryClient.invalidateQueries({ queryKey: reviewKeys.projectSummary(projectRefId) });
  const invalidateWorkspace = (projectRefId: ProjectRefId, reviewItemId: ReviewItemId, versionId?: VersionId) =>
    queryClient.invalidateQueries({ queryKey: reviewKeys.workspace(projectRefId, reviewItemId, versionId) });
  const invalidateCurrentWorkspace = (projectRefId: ProjectRefId, reviewItemId: ReviewItemId) =>
    queryClient.invalidateQueries({ queryKey: reviewKeys.workspaceItem(projectRefId, reviewItemId) });
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
    getReviewItemAuthority,
    syncReviewItemAuthority,
    lockReviewItemRevocationUncertain,
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
        queryClient.removeQueries({ queryKey: reviewKeys.workspaceItem(item.projectRefId, item.reviewItemId) });
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
          queryClient.removeQueries({ queryKey: reviewKeys.workspaceItem(item.projectRefId, item.reviewItemId) });
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
        queryClient.removeQueries({
          queryKey: reviewKeys.revocationAuthorityItem(
            finalization.projectRefId,
            finalization.reviewItemId,
          ),
        });
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
