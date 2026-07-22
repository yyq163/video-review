import { useMemo, useState } from 'react';
import { useQueries } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import type { EntryMode, ProjectDetail } from '../contracts/types';
import { entryLinksFor } from '../entry/entry-links';
import { reviewKeys, useProjects, useReviewMutations } from '../entry/use-review-queries';
import { useReviewApi } from '../entry/runtime';
import { AppShell, CapabilityGate, IconText, actionError } from '../components/shared';
import type { ProjectFormValues } from '../components/ProjectForms';
import { ProjectListContent } from './project-list-content';
import type { CompletionFilter, LifecycleFilter, UpdatedSort } from './project-list-controls';

function projectCompletion(detail: ProjectDetail | undefined): Exclude<CompletionFilter, 'all'> | 'unknown' {
  if (!detail) return 'unknown';
  if (detail.items.length === 0) return 'empty';
  return detail.items.every((item) => item.status === 'finalized') ? 'completed' : 'unfinished';
}

export function ProjectListPage(props: { entryMode: EntryMode }) {
  const navigate = useNavigate();
  const api = useReviewApi(props.entryMode);
  const projects = useProjects(props.entryMode);
  const mutations = useReviewMutations(props.entryMode);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [projectActionError, setProjectActionError] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [lifecycleFilter, setLifecycleFilter] = useState<LifecycleFilter>('all');
  const [completionFilter, setCompletionFilter] = useState<CompletionFilter>('all');
  const [updatedSort, setUpdatedSort] = useState<UpdatedSort>('updated-desc');
  const [pageSize, setPageSize] = useState(20);
  const [page, setPage] = useState(1);
  const projectDetails = useQueries({
    queries: (projects.data ?? []).map((project) => ({
      queryKey: reviewKeys.project(project.projectRefId),
      queryFn: ({ signal }: { signal?: AbortSignal }) => api.getProjectDetail(project.projectRefId, { signal }),
      enabled: Boolean(projects.data),
      retry: false,
    })),
  });
  const detailsByProject = useMemo(() => {
    const details = new Map<string, ProjectDetail>();
    for (const detailQuery of projectDetails) {
      if (detailQuery.data) details.set(detailQuery.data.project.projectRefId, detailQuery.data);
    }
    return details;
  }, [projectDetails]);

  const submitProject = async (values: ProjectFormValues) => {
    setError(null);
    setProjectActionError(null);
    try {
      const project = await mutations.createProject.mutateAsync(values);
      setCreating(false);
      navigate(`/${props.entryMode}/projects/${project.projectRefId}`);
    } catch (caught) {
      setError(actionError(caught));
    }
  };

  const archiveProject = async (projectRefId: string) => {
    setProjectActionError(null);
    try {
      await mutations.archiveProject.mutateAsync({ projectRefId });
    } catch (caught) {
      setProjectActionError(actionError(caught));
    }
  };

  const restoreProject = async (projectRefId: string) => {
    setProjectActionError(null);
    try {
      await mutations.restoreProject.mutateAsync({ projectRefId });
    } catch (caught) {
      setProjectActionError(actionError(caught));
    }
  };

  const deleteProject = async (projectRefId: string, projectName: string) => {
    setProjectActionError(null);
    const confirmed = window.confirm(`确认删除项目「${projectName}」？删除后项目会从审阅列表移除，历史记录和媒体文件仍保留。`);
    if (!confirmed) return;
    try {
      await mutations.deleteProject.mutateAsync({ projectRefId, confirmed: true });
    } catch (caught) {
      setProjectActionError(actionError(caught));
    }
  };

  const projectActionPending =
    mutations.archiveProject.isPending || mutations.restoreProject.isPending || mutations.deleteProject.isPending;
  const filteredProjects = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    return (projects.data ?? [])
      .filter((project) => {
        if (!normalizedQuery) return true;
        return (
          project.name.toLowerCase().includes(normalizedQuery) ||
          project.code.toLowerCase().includes(normalizedQuery) ||
          project.description.toLowerCase().includes(normalizedQuery)
        );
      })
      .filter((project) => lifecycleFilter === 'all' || project.status === lifecycleFilter)
      .filter((project) => {
        if (completionFilter === 'all') return true;
        return projectCompletion(detailsByProject.get(project.projectRefId)) === completionFilter;
      })
      .sort((left, right) => {
        const leftTime = Date.parse(left.updatedAt);
        const rightTime = Date.parse(right.updatedAt);
        const diff = (Number.isFinite(leftTime) ? leftTime : 0) - (Number.isFinite(rightTime) ? rightTime : 0);
        return updatedSort === 'updated-asc' ? diff : -diff;
      });
  }, [completionFilter, detailsByProject, lifecycleFilter, projects.data, query, updatedSort]);
  const totalPages = Math.max(1, Math.ceil(filteredProjects.length / pageSize));
  const safePage = Math.min(page, totalPages);
  const visibleProjects = filteredProjects.slice((safePage - 1) * pageSize, safePage * pageSize);
  const resetPage = () => setPage(1);

  return (
    <AppShell
      entryMode={props.entryMode}
      homeHref={`/${props.entryMode}/projects`}
      entryLinks={entryLinksFor(props.entryMode)}
      right={
        <CapabilityGate entryMode={props.entryMode} capability="review.project.create">
          <button className="fj-review-primary" onClick={() => setCreating((value) => !value)}>
            <IconText icon="plus">新建项目</IconText>
          </button>
        </CapabilityGate>
      }
    >
      <ProjectListContent
        entryMode={props.entryMode}
        creating={creating}
        createPending={mutations.createProject.isPending}
        createError={error}
        projectActionError={projectActionError}
        projectsLoading={projects.isLoading}
        projectsError={projects.error}
        projects={projects.data}
        filteredProjects={filteredProjects}
        visibleProjects={visibleProjects}
        projectActionPending={projectActionPending}
        query={query}
        lifecycleFilter={lifecycleFilter}
        completionFilter={completionFilter}
        updatedSort={updatedSort}
        pageSize={pageSize}
        safePage={safePage}
        totalPages={totalPages}
        onSubmitProject={submitProject}
        onQueryChange={(value) => {
          setQuery(value);
          resetPage();
        }}
        onLifecycleChange={(value) => {
          setLifecycleFilter(value);
          resetPage();
        }}
        onCompletionChange={(value) => {
          setCompletionFilter(value);
          resetPage();
        }}
        onUpdatedSortChange={(value) => {
          setUpdatedSort(value);
          resetPage();
        }}
        onPageSizeChange={(value) => {
          setPageSize(value);
          resetPage();
        }}
        onArchive={archiveProject}
        onRestore={restoreProject}
        onDelete={deleteProject}
        onPageChange={setPage}
      />
    </AppShell>
  );
}
