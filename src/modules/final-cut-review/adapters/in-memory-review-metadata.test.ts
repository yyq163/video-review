import { describe, expect, it } from 'vitest';
import type { Project, ReviewItem } from '../contracts/types';
import { InMemoryReviewRepository } from './in-memory-review-repository';

describe('in-memory review metadata persistence', () => {
  it('keeps item_code immutable across episode edits and snapshot reloads', async () => {
    const timestamp = '2026-07-14T00:00:00.000Z';
    const project: Project = {
      projectRefId: 'project_1',
      name: 'Project',
      code: 'PROJECT-001',
      description: '',
      status: 'active',
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
