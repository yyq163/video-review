import { describe, expect, it, vi } from 'vitest';
import type {
  ProjectDTO,
  ReviewItemDTO,
  ReviewVersionDTO,
} from '../contracts-generated/backend-contract';
import type { HttpReviewQueries } from './http-review-queries';
import { HttpReviewProjects } from './http-review-projects';
import type { HttpReviewTransport } from './http-review-transport';

const project: ProjectDTO = {
  project_ref_id: 'project-summary',
  project_code: 'SUMMARY-1',
  project_name: 'Summary project',
  description: 'single aggregate',
  source: 'local',
  lifecycle_status: 'active',
  completion_status: 'in_progress',
  lock_version: 3,
  created_at: '2026-07-31T00:00:00.000Z',
  updated_at: '2026-07-31T00:01:00.000Z',
};

describe('HTTP project summary adapter', () => {
  it('uses one aggregate route, trusts server delete eligibility, and exposes only ready protected media URLs', async () => {
    const requestJson = vi.fn().mockResolvedValue({
      project,
      items: [
        {
          id: 'item-ready',
          project_ref_id: project.project_ref_id,
          item_code: '1',
          episode_no: 1,
          title: 'Ready item',
          workflow_state: 'changes_requested',
          lock_version: 5,
          current_version_id: 'version-ready',
          current_version: {
            id: 'version-ready',
            version_no: 2,
            version_label: 'V2',
            duration_ms: 10_000,
            file_size: 20_000,
            playback_status: 'ready',
            playback_url: '/api/v1/final-cut-review/projects/project-summary/items/item-ready/versions/version-ready/stream',
            thumbnail_status: 'ready',
            thumbnail_url: '/api/v1/final-cut-review/projects/project-summary/items/item-ready/versions/version-ready/thumbnail',
          },
          unresolved_current_version_count: 2,
          finalization: null,
          revocation_cleanup_status: 'none',
          bulk_delete: {
            eligible: false,
            locked: true,
            reason: 'REVIEW_STARTED',
          },
        },
        {
          id: 'item-malicious',
          project_ref_id: project.project_ref_id,
          item_code: '3',
          episode_no: 3,
          title: 'Malicious media URL',
          workflow_state: 'in_review',
          lock_version: 1,
          current_version_id: 'version-malicious',
          current_version: {
            id: 'version-malicious',
            version_no: 1,
            version_label: 'V1',
            duration_ms: 5_000,
            file_size: 9_000,
            playback_status: 'ready',
            playback_url: 'https://evil.example/steal-cookie.mp4',
            thumbnail_status: 'ready',
            thumbnail_url: '//evil.example/track.jpg',
          },
          unresolved_current_version_count: 0,
          finalization: null,
          revocation_cleanup_status: 'none',
          bulk_delete: {
            eligible: false,
            locked: false,
            reason: 'REVIEW_STARTED',
          },
        },
        {
          id: 'item-pending',
          project_ref_id: project.project_ref_id,
          item_code: '2',
          episode_no: 2,
          title: 'Pending item',
          workflow_state: 'pending_review',
          lock_version: 1,
          current_version_id: 'version-pending',
          current_version: {
            id: 'version-pending',
            version_no: 1,
            version_label: 'V1',
            duration_ms: 5_000,
            file_size: 9_000,
            playback_status: 'pending',
            playback_url: '/must-not-consume',
            thumbnail_status: 'pending',
            thumbnail_url: '/must-not-consume',
          },
          unresolved_current_version_count: 0,
          finalization: null,
          revocation_cleanup_status: 'none',
          bulk_delete: {
            eligible: true,
            locked: false,
            reason: null,
          },
        },
      ],
    });
    const transport = {
      requestJson,
      baseUrl: 'https://review.example',
    } as unknown as HttpReviewTransport;
    const projects = new HttpReviewProjects(
      transport,
      {} as HttpReviewQueries,
    );

    const summary = await projects.getProjectSummary(project.project_ref_id);

    expect(requestJson).toHaveBeenCalledTimes(1);
    expect(requestJson).toHaveBeenCalledWith(
      '/api/v1/final-cut-review/projects/project-summary/summary',
      { signal: undefined },
    );
    expect(summary.project.completionStatus).toBe('in_progress');
    expect(summary.items[0]).toMatchObject({
      currentVersionId: 'version-ready',
      status: 'changes_requested',
      unresolvedCurrentVersionCount: 2,
      bulkDelete: {
        eligible: false,
        locked: true,
        reason: 'REVIEW_STARTED',
      },
      currentVersion: {
        playbackUrl:
          'https://review.example/api/v1/final-cut-review/projects/project-summary/items/item-ready/versions/version-ready/stream',
        thumbnailUrl:
          'https://review.example/api/v1/final-cut-review/projects/project-summary/items/item-ready/versions/version-ready/thumbnail',
      },
    });
    expect(
      summary.items.find((item) => item.reviewItemId === 'item-pending')?.currentVersion,
    ).toMatchObject({
      playbackStatus: 'pending',
      playbackUrl: null,
      thumbnailStatus: 'pending',
      thumbnailUrl: null,
    });
    expect(
      summary.items.find((item) => item.reviewItemId === 'item-malicious')?.currentVersion,
    ).toMatchObject({
      playbackStatus: 'failed',
      playbackUrl: null,
      thumbnailStatus: 'failed',
      thumbnailUrl: null,
    });
  });

  it('loads only the selected version issue list when workspace refreshes and never fans out historical issue details', async () => {
    const item: ReviewItemDTO = {
      id: 'item-workspace',
      project_ref_id: project.project_ref_id,
      item_code: '1',
      episode_no: 1,
      title: 'Workspace item',
      workflow_status: 'in_review',
      current_version_id: 'version-2',
      current_version_no: 2,
      ui_status: '审阅中',
      active_finalization_id: null,
      unresolved_current_version_count: 1,
      resolved_current_version_count: 0,
      historical_version_count: 1,
      is_finalized: false,
      lock_version: 2,
      created_at: project.created_at,
      updated_at: project.updated_at,
    };
    const media = {
      original_file_id: 'original-1',
      original_filename: 'episode.mp4',
      mime_type: 'video/mp4',
      file_size: 1_000,
      sha256: 'a'.repeat(64),
      duration_ms: 1_000,
      width: 1920,
      height: 1080,
      fps_num: 25,
      fps_den: 1,
      media_probe_version: 'ffprobe-test',
    };
    const versions: ReviewVersionDTO[] = [
      {
        id: 'version-1',
        project_ref_id: project.project_ref_id,
        review_item_id: item.id,
        version_no: 1,
        version_label: 'V1',
        is_current: false,
        original_media: media,
        playback_status: 'ready',
        playback_asset_id: 'playback-1',
        lock_version: 1,
        created_at: project.created_at,
      },
      {
        id: 'version-2',
        project_ref_id: project.project_ref_id,
        review_item_id: item.id,
        previous_version_id: 'version-1',
        version_no: 2,
        version_label: 'V2',
        is_current: true,
        original_media: { ...media, original_file_id: 'original-2' },
        playback_status: 'processing',
        playback_asset_id: null,
        lock_version: 1,
        created_at: project.updated_at,
      },
    ];
    const requestJson = vi.fn(async (path: string) => {
      if (path.endsWith(`/items/${item.id}`)) return item;
      throw new Error(`unexpected JSON request ${path}`);
    });
    const requestList = vi.fn(async (path: string) => {
      if (path.endsWith(`/items/${item.id}/versions`)) return versions;
      throw new Error(`unexpected list request ${path}`);
    });
    const transport = {
      requestJson,
      requestList,
      client: { getProject: vi.fn().mockResolvedValue(project) },
      queryInit: vi.fn(),
      baseUrl: 'https://review.example',
    } as unknown as HttpReviewTransport;
    const queries = {
      issuesForVersion: vi.fn().mockResolvedValue([]),
      issueWithMessages: vi.fn(),
      optionalFinalization: vi.fn().mockResolvedValue(null),
    } as unknown as HttpReviewQueries;
    const projects = new HttpReviewProjects(transport, queries);

    await projects.getWorkspace({
      projectRefId: project.project_ref_id,
      reviewItemId: item.id,
    });
    await projects.getWorkspace({
      projectRefId: project.project_ref_id,
      reviewItemId: item.id,
    });

    expect(queries.issuesForVersion).toHaveBeenCalledTimes(2);
    expect(queries.issuesForVersion).toHaveBeenNthCalledWith(
      1,
      project.project_ref_id,
      item.id,
      'version-2',
      undefined,
    );
    expect(queries.issuesForVersion).toHaveBeenNthCalledWith(
      2,
      project.project_ref_id,
      item.id,
      'version-2',
      undefined,
    );
    expect(queries.issueWithMessages).not.toHaveBeenCalled();
    expect(
      vi.mocked(queries.issuesForVersion).mock.calls.some((call) => call[2] === 'version-1'),
    ).toBe(false);
  });
});
