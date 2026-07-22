import { Link } from 'react-router-dom';
import type { EntryMode, Project } from '../contracts/types';
import { ProjectForm, type ProjectFormValues } from '../components/ProjectForms';
import { CapabilityGate, EmptyState, ErrorView, LoadingBlock, StatusBadge } from '../components/shared';
import {
  ProjectListControls,
  type CompletionFilter,
  type LifecycleFilter,
  type UpdatedSort,
} from './project-list-controls';

export function ProjectListContent(props: {
  entryMode: EntryMode;
  creating: boolean;
  createPending: boolean;
  createError: string | null;
  projectActionError: string | null;
  projectsLoading: boolean;
  projectsError: unknown;
  projects: Project[] | undefined;
  filteredProjects: Project[];
  visibleProjects: Project[];
  projectActionPending: boolean;
  query: string;
  lifecycleFilter: LifecycleFilter;
  completionFilter: CompletionFilter;
  updatedSort: UpdatedSort;
  pageSize: number;
  safePage: number;
  totalPages: number;
  onSubmitProject(values: ProjectFormValues): Promise<void>;
  onQueryChange(value: string): void;
  onLifecycleChange(value: LifecycleFilter): void;
  onCompletionChange(value: CompletionFilter): void;
  onUpdatedSortChange(value: UpdatedSort): void;
  onPageSizeChange(value: number): void;
  onArchive(projectRefId: string): Promise<void>;
  onRestore(projectRefId: string): Promise<void>;
  onDelete(projectRefId: string, projectName: string): Promise<void>;
  onPageChange(page: number): void;
}) {
  return (
    <section className="fj-review-page fj-review-project-list">
      <div className="fj-review-page-heading">
        <div>
          <span>{props.entryMode === 'edit' ? '剪辑入口' : '审阅入口'}</span>
          <h1>项目列表</h1>
        </div>
        <p>{props.entryMode === 'edit' ? '创建项目、上传版本和下载已定稿单片。' : '审阅项目、处理意见、定稿和打包。'}</p>
      </div>
      {props.creating && (
        <section className="fj-review-side-form">
          <h2>新建项目</h2>
          <ProjectForm submitLabel="创建项目" pending={props.createPending} onSubmit={props.onSubmitProject} />
          {props.createError && <div className="fj-review-error">{props.createError}</div>}
        </section>
      )}
      <ProjectListControls
        query={props.query}
        lifecycleFilter={props.lifecycleFilter}
        completionFilter={props.completionFilter}
        updatedSort={props.updatedSort}
        pageSize={props.pageSize}
        onQueryChange={props.onQueryChange}
        onLifecycleChange={props.onLifecycleChange}
        onCompletionChange={props.onCompletionChange}
        onUpdatedSortChange={props.onUpdatedSortChange}
        onPageSizeChange={props.onPageSizeChange}
      />
      {props.projectsLoading && <LoadingBlock />}
      {props.projectsError ? <ErrorView error={props.projectsError} /> : null}
      {props.projectActionError && <div className="fj-review-error">{props.projectActionError}</div>}
      {props.projects?.length === 0 && <EmptyState title="暂无项目" detail="从剪辑入口创建第一个项目。" />}
      {props.projects && props.projects.length > 0 && props.filteredProjects.length === 0 && (
        <EmptyState title="没有匹配项目" detail="调整搜索、筛选或排序条件后再查看。" />
      )}
      <div className="fj-review-project-grid">
        {props.visibleProjects.map((project) => (
          <article
            key={project.projectRefId}
            className={`fj-review-project-card ${project.status === 'archived' ? 'is-archived' : ''}`}
            data-testid={`project-card-${project.projectRefId}`}
          >
            <div className="fj-review-project-card-head">
              <strong>{project.name}</strong>
              <StatusBadge status={project.status} />
            </div>
            <code>{project.code}</code>
            <p>{project.description}</p>
            <div className="fj-review-project-card-actions">
              <Link className="fj-review-primary" to={`/${props.entryMode}/projects/${project.projectRefId}`}>
                {props.entryMode === 'edit' ? '剪辑管理' : '成片审阅'} →
              </Link>
              {project.status === 'active' ? (
                <CapabilityGate entryMode={props.entryMode} capability="review.project.archive">
                  <button
                    className="fj-review-secondary"
                    disabled={props.projectActionPending}
                    onClick={() => props.onArchive(project.projectRefId)}
                    type="button"
                  >
                    归档
                  </button>
                </CapabilityGate>
              ) : (
                <CapabilityGate entryMode={props.entryMode} capability="review.project.restore">
                  <button
                    className="fj-review-secondary"
                    disabled={props.projectActionPending}
                    onClick={() => props.onRestore(project.projectRefId)}
                    type="button"
                  >
                    恢复
                  </button>
                </CapabilityGate>
              )}
              {project.status === 'active' ? (
                <CapabilityGate entryMode={props.entryMode} capability="review.project.delete">
                  <button
                    className="fj-review-secondary is-danger"
                    data-testid={`delete-project-${project.projectRefId}`}
                    disabled={props.projectActionPending}
                    onClick={() => void props.onDelete(project.projectRefId, project.name)}
                    type="button"
                  >
                    删除项目
                  </button>
                </CapabilityGate>
              ) : null}
            </div>
          </article>
        ))}
      </div>
      {props.projects && props.projects.length > 0 && (
        <nav className="fj-review-pagination" aria-label="项目列表分页">
          <span>
            显示 {props.filteredProjects.length === 0 ? 0 : (props.safePage - 1) * props.pageSize + 1}-
            {Math.min(props.safePage * props.pageSize, props.filteredProjects.length)} / {props.filteredProjects.length}
          </span>
          <button
            className="fj-review-secondary"
            type="button"
            disabled={props.safePage <= 1}
            onClick={() => props.onPageChange(Math.max(1, props.safePage - 1))}
          >
            上一页
          </button>
          <span>
            第 {props.safePage} / {props.totalPages} 页
          </span>
          <button
            className="fj-review-secondary"
            type="button"
            disabled={props.safePage >= props.totalPages}
            onClick={() => props.onPageChange(Math.min(props.totalPages, props.safePage + 1))}
          >
            下一页
          </button>
        </nav>
      )}
    </section>
  );
}
