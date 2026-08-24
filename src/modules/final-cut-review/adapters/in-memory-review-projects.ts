import type { Project, ProjectRefId } from '../contracts/types';
import { invariant } from '../core/errors';
import { createUuid } from '../core/uuid';
import { nowIso } from './in-memory-review-clones';
import type { InMemoryReviewStore } from './in-memory-review-store';

const ALLOWED_PROJECT_FRAME_RATES = new Set([
  '24000/1001',
  '24/1',
  '25/1',
  '30000/1001',
  '30/1',
  '50/1',
  '60000/1001',
  '60/1',
]);

function validateFrameRate(frameRate: { fpsNum: number; fpsDen: number }): void {
  invariant(
    Number.isSafeInteger(frameRate.fpsNum)
      && Number.isSafeInteger(frameRate.fpsDen)
      && ALLOWED_PROJECT_FRAME_RATES.has(`${frameRate.fpsNum}/${frameRate.fpsDen}`),
    '项目帧率不在允许集合中',
    'VALIDATION_ERROR',
  );
}

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
    frameRate?: { fpsNum: number; fpsDen: number };
  }): Promise<Project> => {
    validateProjectMetadata(input);
    const frameRate = input.frameRate ?? { fpsNum: 25, fpsDen: 1 };
    validateFrameRate(frameRate);
    const timestamp = nowIso();
    const project: Project = {
      projectRefId: `prj_${createUuid()}`,
      name: input.name.trim(),
      code: input.code.trim(),
      description: input.description.trim(),
      status: 'active',
      completionStatus: 'empty',
      deletedAt: null,
      frameRate,
      createdAt: timestamp,
      updatedAt: timestamp,
    };
    this.store.projects.set(project.projectRefId, project);
    this.store.emitChange();
    return { ...project };
  };

  readonly lockProjectFrameRate = async (input: {
    projectRefId: ProjectRefId;
    frameRate: { fpsNum: number; fpsDen: number };
  }): Promise<Project> => {
    const current = this.store.assertProjectWritable(input.projectRefId);
    invariant(!current.frameRate, '项目帧率已锁定，不能修改', 'RESOURCE_STATE_CONFLICT');
    validateFrameRate(input.frameRate);
    const next = {
      ...current,
      frameRate: { ...input.frameRate },
      updatedAt: nowIso(),
    };
    this.store.projects.set(next.projectRefId, next);
    this.store.emitChange();
    return { ...next, frameRate: { ...input.frameRate } };
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
    const projectItems = [...this.store.items.values()].filter(
      (item) => item.projectRefId === input.projectRefId,
    );
    invariant(
      projectItems.every((item) => item.status !== 'finalized' && !item.activeFinalizationId),
      '项目仍有已定稿分集，必须先显式撤销定稿',
      'RESOURCE_STATE_CONFLICT',
    );
    const itemIds = new Set(projectItems.map((item) => item.reviewItemId));
    for (const [issueId, issue] of this.store.issues) {
      if (issue.projectRefId === input.projectRefId) this.store.issues.delete(issueId);
    }
    for (const [versionId, version] of this.store.versions) {
      if (version.projectRefId === input.projectRefId) this.store.versions.delete(versionId);
    }
    for (const [finalizationId, finalization] of this.store.finalizations) {
      if (finalization.projectRefId === input.projectRefId) {
        this.store.finalizations.delete(finalizationId);
      }
    }
    for (const itemId of itemIds) this.store.items.delete(itemId);
    this.store.projects.delete(input.projectRefId);
    this.store.emitChange();
    return { ...current };
  };

  readonly ensureProjectWritable = (projectRefId: ProjectRefId): void => {
    this.store.assertProjectWritable(projectRefId);
  };

  readonly ensureProjectNotDeleted = (projectRefId: ProjectRefId): void => {
    const project = this.store.getProject(projectRefId);
    invariant(!project.deletedAt, '项目已删除', 'RESOURCE_STATE_CONFLICT');
  };
}
