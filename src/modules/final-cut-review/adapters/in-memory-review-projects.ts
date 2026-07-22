import type { Project, ProjectRefId } from '../contracts/types';
import { invariant } from '../core/errors';
import { createUuid } from '../core/uuid';
import { nowIso } from './in-memory-review-clones';
import type { InMemoryReviewStore } from './in-memory-review-store';

function validateProjectMetadata(input: { name: string; code: string; description: string }): void {
  const name = input.name.trim();
  const code = input.code.trim();
  const description = input.description.trim();
  invariant(name.length >= 1 && name.length <= 255, '项目名称长度必须在 1 到 255 之间', 'VALIDATION_ERROR');
  invariant(code.length >= 1 && code.length <= 128, '项目编号长度必须在 1 到 128 之间', 'VALIDATION_ERROR');
  invariant(description.length <= 2000, '项目说明最多 2000 个字符', 'VALIDATION_ERROR');
}

export class InMemoryReviewProjects {
  constructor(private readonly store: InMemoryReviewStore) {}

  readonly createProject = async (input: {
    name: string;
    code: string;
    description: string;
  }): Promise<Project> => {
    validateProjectMetadata(input);
    const timestamp = nowIso();
    const project: Project = {
      projectRefId: `prj_${createUuid()}`,
      name: input.name.trim(),
      code: input.code.trim(),
      description: input.description.trim(),
      status: 'active',
      deletedAt: null,
      createdAt: timestamp,
      updatedAt: timestamp,
    };
    this.store.projects.set(project.projectRefId, project);
    this.store.emitChange();
    return { ...project };
  };

  readonly updateProject = async (input: {
    projectRefId: ProjectRefId;
    name: string;
    code: string;
    description: string;
  }): Promise<Project> => {
    const current = this.store.assertProjectWritable(input.projectRefId);
    validateProjectMetadata(input);
    invariant(input.code.trim() === current.code, '项目编号创建后不可修改', 'RESOURCE_STATE_CONFLICT');
    const next: Project = {
      ...current,
      name: input.name.trim(),
      code: current.code,
      description: input.description.trim(),
      updatedAt: nowIso(),
    };
    this.store.projects.set(next.projectRefId, next);
    this.store.emitChange();
    return { ...next };
  };

  readonly archiveProject = async (input: { projectRefId: ProjectRefId }): Promise<Project> => {
    const current = this.store.getProject(input.projectRefId);
    invariant(!current.deletedAt, '项目已删除', 'PROJECT_DELETED_READONLY');
    invariant(current.status === 'active', '项目已归档', 'RESOURCE_STATE_CONFLICT');
    const next: Project = { ...current, status: 'archived', updatedAt: nowIso() };
    this.store.projects.set(next.projectRefId, next);
    this.store.emitChange();
    return { ...next };
  };

  readonly restoreProject = async (input: { projectRefId: ProjectRefId }): Promise<Project> => {
    const current = this.store.getProject(input.projectRefId);
    invariant(!current.deletedAt, '项目已删除', 'PROJECT_DELETED_READONLY');
    invariant(current.status === 'archived', '项目未归档', 'RESOURCE_STATE_CONFLICT');
    const next: Project = { ...current, status: 'active', updatedAt: nowIso() };
    this.store.projects.set(next.projectRefId, next);
    this.store.emitChange();
    return { ...next };
  };

  readonly deleteProject = async (input: { projectRefId: ProjectRefId; confirmed: true }): Promise<Project> => {
    invariant(input.confirmed === true, '删除项目必须二次确认', 'RESOURCE_STATE_CONFLICT');
    const current = this.store.getProject(input.projectRefId);
    invariant(!current.deletedAt, '项目已删除', 'RESOURCE_STATE_CONFLICT');
    invariant(current.status === 'active', '归档项目只读，请先恢复项目', 'RESOURCE_STATE_CONFLICT');
    const next: Project = { ...current, deletedAt: nowIso(), updatedAt: nowIso() };
    this.store.projects.set(next.projectRefId, next);
    this.store.emitChange();
    return { ...next };
  };

  readonly ensureProjectWritable = (projectRefId: ProjectRefId): void => {
    this.store.assertProjectWritable(projectRefId);
  };

  readonly ensureProjectNotDeleted = (projectRefId: ProjectRefId): void => {
    const project = this.store.getProject(projectRefId);
    invariant(!project.deletedAt, '项目已删除', 'RESOURCE_STATE_CONFLICT');
  };
}
