import { describe, expect, it } from 'vitest';
import type { Project, ReviewItem } from '../contracts/types';
import { InMemoryReviewRepository } from './in-memory-review-repository';

describe('in-memory review metadata persistence', () => {
  it('persists the responsible-editor directory and locks a legacy project frame rate once', async () => {
    const timestamp = '2026-07-14T00:00:00.000Z';
    const project: Project = {
      projectRefId: 'legacy_project',
      name: 'Legacy project',
      code: 'LEGACY-001',
      description: '',
      status: 'active',
      completionStatus: 'empty',
      deletedAt: null,
      frameRate: null,
      createdAt: timestamp,
      updatedAt: timestamp,
    };
    const repository = new InMemoryReviewRepository({ projects: [project] });
    const task5Repository = repository as unknown as {
      listResponsibleEditors(): Promise<Array<{ responsibleEditorId: string; displayName: string; lockVersion: number }>>;
      createResponsibleEditor(input: { displayName: string }): Promise<{ responsibleEditorId: string; displayName: string; lockVersion: number }>;
      renameResponsibleEditor(input: { responsibleEditorId: string; displayName: string }): Promise<{ responsibleEditorId: string; displayName: string; lockVersion: number }>;
      lockProjectFrameRate(input: { projectRefId: string; frameRate: { fpsNum: number; fpsDen: number } }): Promise<Project>;
    };

    expect(await task5Repository.listResponsibleEditors()).toEqual([]);
    const created = await task5Repository.createResponsibleEditor({ displayName: ' 剪辑师甲 ' });
    expect(created).toMatchObject({ displayName: '剪辑师甲', lockVersion: 1 });
    const renamed = await task5Repository.renameResponsibleEditor({
      responsibleEditorId: created.responsibleEditorId,
      displayName: '剪辑师乙',
    });
    expect(renamed).toMatchObject({ displayName: '剪辑师乙', lockVersion: 2 });

    const locked = await task5Repository.lockProjectFrameRate({
      projectRefId: project.projectRefId,
      frameRate: { fpsNum: 25, fpsDen: 1 },
    });
    expect(locked.frameRate).toEqual({ fpsNum: 25, fpsDen: 1 });
    await expect(task5Repository.lockProjectFrameRate({
      projectRefId: project.projectRefId,
      frameRate: { fpsNum: 30, fpsDen: 1 },
    })).rejects.toThrow('项目帧率已锁定');

    const reloaded = new InMemoryReviewRepository(repository.snapshot()) as unknown as typeof task5Repository;
    expect(await reloaded.listResponsibleEditors()).toEqual([
      expect.objectContaining({ displayName: '剪辑师乙', lockVersion: 2 }),
    ]);
  });

  it('keeps item_code immutable across episode edits and snapshot reloads', async () => {
    const timestamp = '2026-07-14T00:00:00.000Z';
    const project: Project = {
      projectRefId: 'project_1',
      name: 'Project',
      code: 'PROJECT-001',
      description: '',
      status: 'active',
      completionStatus: 'empty',
      deletedAt: null,
      createdAt: timestamp,
      updatedAt: timestamp,
    };
    const item: ReviewItem & { readonly itemCode: string } = {
      reviewItemId: 'item_1',
      projectRefId: project.projectRefId,
      itemCode: 'ITEM-IMMUTABLE-001',
      title: 'Original title',
      episode: '1',
      currentVersionId: 'version_1',
      activeFinalizationId: null,
      status: 'pending_review',
      createdAt: timestamp,
      updatedAt: timestamp,
    };
    const repository = new InMemoryReviewRepository({ projects: [project], items: [item] });

    await repository.updateReviewItem({
      projectRefId: project.projectRefId,
      reviewItemId: item.reviewItemId,
      title: 'Updated title',
      episode: '2',
    });
    const snapshot = repository.snapshot();
    expect(snapshot.items[0]).toMatchObject({ episode: '2', itemCode: 'ITEM-IMMUTABLE-001' });

    const reloaded = new InMemoryReviewRepository(snapshot).snapshot();
    expect(reloaded.items[0]).toMatchObject({ episode: '2', itemCode: 'ITEM-IMMUTABLE-001' });
  });

  it('rejects metadata values excluded by the production contract', async () => {
    const timestamp = '2026-07-14T00:00:00.000Z';
    const project: Project = {
      projectRefId: 'project_1',
      name: 'Project',
      code: 'PROJECT-001',
      description: '',
      status: 'active',
      completionStatus: 'empty',
      deletedAt: null,
      createdAt: timestamp,
      updatedAt: timestamp,
    };
    const item: ReviewItem = {
      reviewItemId: 'item_1',
      projectRefId: project.projectRefId,
      title: 'Original title',
      episode: '1',
      currentVersionId: 'version_1',
      activeFinalizationId: null,
      status: 'pending_review',
      createdAt: timestamp,
      updatedAt: timestamp,
    };
    const repository = new InMemoryReviewRepository({ projects: [project], items: [item] });

    await expect(
      repository.createProject({ name: 'New project', code: '  ', description: '' }),
    ).rejects.toThrow('项目编号长度必须在 1 到 128 之间');
    await expect(
      repository.createProject({
        name: 'Unsupported fps',
        code: 'FPS-48',
        description: '',
        frameRate: { fpsNum: 48, fpsDen: 1 },
      }),
    ).rejects.toThrow('项目帧率不在允许集合中');
    const defaultFrameRate = await repository.createProject({
      name: 'Default fps',
      code: 'FPS-DEFAULT',
      description: '',
    });
    expect(defaultFrameRate.frameRate).toEqual({ fpsNum: 25, fpsDen: 1 });
    await expect(
      repository.updateProject({ projectRefId: project.projectRefId, name: '  ', code: project.code, description: '' }),
    ).rejects.toThrow('项目名称长度必须在 1 到 255 之间');
    await expect(
      repository.updateProject({
        projectRefId: project.projectRefId,
        name: project.name,
        code: project.code,
        description: 'x'.repeat(2001),
      }),
    ).rejects.toThrow('项目说明最多 2000 个字符');
    await expect(
      repository.updateReviewItem({
        projectRefId: project.projectRefId,
        reviewItemId: item.reviewItemId,
        title: '',
        episode: '0',
      }),
    ).rejects.toThrow('成片标题长度必须在 1 到 512 之间');
    await expect(
      repository.updateReviewItem({
        projectRefId: project.projectRefId,
        reviewItemId: item.reviewItemId,
        title: 'Valid title',
        episode: '0',
      }),
    ).rejects.toThrow('集数必须是大于等于 1 的整数');
  });
});
