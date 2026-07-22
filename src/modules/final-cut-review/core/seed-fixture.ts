import type { Project, ReviewIssue, ReviewItem, ReviewVersion } from '../contracts/types';
import { makeIssue, makeSeedFile, makeVersion } from './seed-builders';
import type { SeedData } from './seed-types';

const seededAt = '2026-06-18T10:00:00.000Z';

export function buildSeedData(): SeedData {
  const v1File = makeSeedFile({
    originalFileId: 'file_seed_v1',
    fileName: '真千金是男的_EP28_V1.webm',
    sha256: '2dd8bb4a8ed0a0fca70b53cb9550cf2888eec62aa2f227fdf547ce9f77dbdd85',
  });
  const v2File = makeSeedFile({
    originalFileId: 'file_seed_v2',
    fileName: '真千金是男的_EP28_V2.webm',
    sha256: 'a78ded2e04a29fa25765d1546b402864f65fdb75ed06621d8024737c238f1ea3',
  });

  const project: Project = {
    projectRefId: 'prj_seed_final_cut',
    name: '真千金是男的',
    code: 'FJ-DEMO-28',
    description: '短剧成片审阅演示项目，覆盖 V1 修改、V2 独立复审、定稿和打包。',
    status: 'active',
    deletedAt: null,
    createdAt: seededAt,
    updatedAt: seededAt,
  };

  const item: ReviewItem = {
    reviewItemId: 'item_ep28',
    projectRefId: project.projectRefId,
    title: '第 28 集 · 最终成片',
    episode: '28',
    currentVersionId: 'ver_ep28_v2',
    activeFinalizationId: null,
    status: 'in_review',
    createdAt: seededAt,
    updatedAt: seededAt,
  };

  const versions: ReviewVersion[] = [
    makeVersion({
      versionId: 'ver_ep28_v1',
      projectRefId: project.projectRefId,
      reviewItemId: item.reviewItemId,
      versionNo: 1,
      file: v1File,
      status: 'changes_requested',
      uploadedAt: seededAt,
      requestedChangesAt: '2026-06-18T10:06:00.000Z',
    }),
    makeVersion({
      versionId: 'ver_ep28_v2',
      projectRefId: project.projectRefId,
      reviewItemId: item.reviewItemId,
      versionNo: 2,
      file: v2File,
      status: 'in_review',
      uploadedAt: '2026-06-18T10:16:00.000Z',
      requestedChangesAt: null,
    }),
  ];

  const issues: ReviewIssue[] = [
    makeIssue({
      issueId: 'issue_v1_001',
      issueNo: 1,
      projectRefId: project.projectRefId,
      reviewItemId: item.reviewItemId,
      versionId: 'ver_ep28_v1',
      timestampMs: 80,
      frameNumber: 2,
      status: 'unresolved',
      severity: 'blocking',
      body: 'V1 00:00:00:02 老人领口被抓动作过重，需要减弱冲突感。',
      shape: {
        shapeId: 'shape_v1_001',
        tool: 'rect',
        color: '#57e3d2',
        lineWidth: 3,
        bounds: { x: 0.35, y: 0.25, width: 0.32, height: 0.28 },
        text: '领口动作',
      },
      createdAt: '2026-06-18T10:02:00.000Z',
    }),
    makeIssue({
      issueId: 'issue_v2_001',
      issueNo: 2,
      projectRefId: project.projectRefId,
      reviewItemId: item.reviewItemId,
      versionId: 'ver_ep28_v2',
      timestampMs: 160,
      frameNumber: 4,
      status: 'unresolved',
      severity: 'normal',
      body: 'V2 00:00:00:04 右侧字幕可再向内收 12px，避免贴边。',
      shape: {
        shapeId: 'shape_v2_001',
        tool: 'arrow',
        color: '#ffcc3d',
        lineWidth: 4,
        points: [
          { x: 0.82, y: 0.64 },
          { x: 0.66, y: 0.52 },
        ],
        text: '字幕边距',
      },
      createdAt: '2026-06-18T10:20:00.000Z',
    }),
    makeIssue({
      issueId: 'issue_v2_002',
      issueNo: 3,
      projectRefId: project.projectRefId,
      reviewItemId: item.reviewItemId,
      versionId: 'ver_ep28_v2',
      timestampMs: 240,
      frameNumber: 6,
      status: 'unresolved',
      severity: 'normal',
      body: 'V2 00:00:00:06 片尾黑场前一帧需要检查字幕淡出节奏。',
      shape: {
        shapeId: 'shape_v2_002',
        tool: 'circle',
        color: '#f3576a',
        lineWidth: 3,
        bounds: { x: 0.44, y: 0.42, width: 0.2, height: 0.16 },
        text: '淡出节奏',
      },
      createdAt: '2026-06-18T10:22:00.000Z',
    }),
    makeIssue({
      issueId: 'issue_v2_003',
      issueNo: 4,
      projectRefId: project.projectRefId,
      reviewItemId: item.reviewItemId,
      versionId: 'ver_ep28_v2',
      timestampMs: 320,
      frameNumber: 8,
      status: 'unresolved',
      severity: 'normal',
      body: 'V2 00:00:00:08 右上角高光闪烁需要压低。',
      shape: {
        shapeId: 'shape_v2_003',
        tool: 'pen',
        color: '#57e3d2',
        lineWidth: 3,
        points: [
          { x: 0.58, y: 0.22 },
          { x: 0.62, y: 0.2 },
          { x: 0.68, y: 0.23 },
        ],
        text: '高光',
      },
      createdAt: '2026-06-18T10:24:00.000Z',
    }),
  ];

  return {
    projects: [project],
    items: [item],
    versions,
    issues,
    finalizations: [],
    files: [v1File, v2File],
  };
}
