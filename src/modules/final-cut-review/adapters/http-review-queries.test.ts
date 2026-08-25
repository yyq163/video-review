import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  FinalCutReviewClient,
  type ReviewIssueDTO,
  type ReviewIssueRevisionDTO,
} from '../contracts-generated/backend-contract';
import { HttpReviewQueries } from './http-review-queries';
import { HttpReviewTransport } from './http-review-transport';

const currentRevision: ReviewIssueRevisionDTO = {
  id: 'rev_2',
  project_ref_id: 'project_1',
  review_item_id: 'item_1',
  version_id: 'version_1',
  issue_id: 'issue_1',
  revision_no: 2,
  content: 'current content',
  annotation_set_id: null,
  created_at: '2026-07-14T02:00:00.000Z',
};

const issueDto: ReviewIssueDTO = {
  id: 'issue_1',
  project_ref_id: 'project_1',
  review_item_id: 'item_1',
  version_id: 'version_1',
  issue_no: 1,
  status: 'unresolved',
  current_revision_id: currentRevision.id,
  timestamp_ms: 240,
  frame_number: 6,
  playback_target: {
    project_ref_id: 'project_1',
    review_item_id: 'item_1',
    version_id: 'version_1',
    issue_id: 'issue_1',
    revision_id: currentRevision.id,
    timestamp_ms: 240,
    frame_number: 6,
  },
  current_revision: currentRevision,
  current_annotation_set: null,
  deleted_at: null,
  lock_version: 2,
  created_at: '2026-07-14T01:00:00.000Z',
  updated_at: '2026-07-14T02:00:00.000Z',
};

interface MockListPage {
  page: number;
  totalCount: number;
  data: unknown[];
}

function idRecords(first: number, last: number): Array<{ id: number }> {
  return Array.from({ length: last - first + 1 }, (_, index) => ({ id: first + index }));
}

function stubListPages(pages: MockListPage[]) {
  let responseIndex = 0;
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const response = pages[responseIndex];
    if (!response) {
      throw new Error(`unexpected request ${String(input)}`);
    }
    responseIndex += 1;
    const requestedPage = Number(new URL(String(input)).searchParams.get('page'));
    expect(requestedPage).toBe(response.page);
    return new Response(JSON.stringify({
      data: response.data,
      meta: { total_count: response.totalCount, page: response.page, page_size: 200 },
    }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function reviewTransport(): HttpReviewTransport {
  const baseUrl = 'https://review.example';
  return new HttpReviewTransport('review', new FinalCutReviewClient(baseUrl), baseUrl);
}

describe('HttpReviewQueries issue revisions', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('loads revision history, deduplicates the current revision, and orders it by revision number', async () => {
    const firstRevision: ReviewIssueRevisionDTO = {
      ...currentRevision,
      id: 'rev_1',
      revision_no: 1,
      content: 'original content',
      created_at: '2026-07-14T01:00:00.000Z',
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const pathname = new URL(url).pathname;
      if (pathname.endsWith('/messages')) {
        return new Response(JSON.stringify({ data: [], meta: { total_count: 0, page: 1, page_size: 200 } }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (pathname.endsWith('/revisions')) {
        return new Response(JSON.stringify({
          data: [currentRevision, firstRevision],
          meta: { total_count: 2, page: 1, page_size: 200 },
        }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      throw new Error(`unexpected URL ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    const baseUrl = 'https://review.example';
    const queries = new HttpReviewQueries(
      new HttpReviewTransport('review', new FinalCutReviewClient(baseUrl), baseUrl),
    );

    const issue = await queries.issueWithMessages(
      'project_1',
      'item_1',
      'version_1',
      'issue_1',
      issueDto,
    );

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock).toHaveBeenCalledWith(
      `${baseUrl}/api/v1/final-cut-review/projects/project_1/items/item_1/versions/version_1/issues/issue_1/revisions?page=1&page_size=200`,
      expect.objectContaining({ credentials: 'include' }),
    );
    expect(issue.revisions.map((revision) => [revision.revisionNo, revision.revisionId])).toEqual([
      [1, 'rev_1'],
      [2, 'rev_2'],
    ]);
    expect(issue.currentRevision).toMatchObject({
      revisionId: issueDto.current_revision.id,
      revisionNo: issueDto.current_revision.revision_no,
      content: issueDto.current_revision.content,
    });
    expect(issue.revisions[1]).toBe(issue.currentRevision);
    expect(Object.isFrozen(issue.revisions)).toBe(true);
    expect(issue.body).toBe(issueDto.current_revision.content);
  });

  it('keeps a version issue list lightweight until one issue detail is requested', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const pathname = new URL(String(input)).pathname;
      if (pathname.endsWith('/versions/version_1/issues')) {
        return new Response(JSON.stringify({
          data: [issueDto],
          meta: { total_count: 1, page: 1, page_size: 200 },
        }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (pathname.endsWith('/issues/issue_1')) {
        return new Response(JSON.stringify({
          data: issueDto,
          meta: { request_id: 'issue-detail' },
        }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (pathname.endsWith('/issues/issue_1/messages')) {
        return new Response(JSON.stringify({
          data: [],
          meta: { total_count: 0, page: 1, page_size: 200 },
        }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (pathname.endsWith('/issues/issue_1/revisions')) {
        return new Response(JSON.stringify({
          data: [currentRevision],
          meta: { total_count: 1, page: 1, page_size: 200 },
        }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      throw new Error(`unexpected URL ${String(input)}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    const queries = new HttpReviewQueries(reviewTransport());

    const summaries = await queries.issuesForVersion(
      'project_1',
      'item_1',
      'version_1',
    );

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(summaries[0]).toMatchObject({
      issueId: 'issue_1',
      replies: [],
    });
    expect(summaries[0]?.revisions).toHaveLength(1);
    expect(fetchMock.mock.calls.map(([input]) => String(input))).not.toEqual(
      expect.arrayContaining([
        expect.stringContaining('/messages'),
        expect.stringContaining('/revisions'),
      ]),
    );

    const detail = await queries.issueWithMessages(
      'project_1',
      'item_1',
      'version_1',
      'issue_1',
    );

    expect(detail.issueId).toBe('issue_1');
    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(fetchMock.mock.calls.slice(1).map(([input]) => new URL(String(input)).pathname)).toEqual(
      expect.arrayContaining([
        '/api/v1/final-cut-review/projects/project_1/items/item_1/versions/version_1/issues/issue_1',
        '/api/v1/final-cut-review/projects/project_1/items/item_1/versions/version_1/issues/issue_1/messages',
        '/api/v1/final-cut-review/projects/project_1/items/item_1/versions/version_1/issues/issue_1/revisions',
      ]),
    );
  });

  it('loads every page of a version issue list without truncating after 200 opinions', async () => {
    const issueForIndex = (index: number): ReviewIssueDTO => {
      const issueId = `issue_${index}`;
      const revisionId = `revision_${index}`;
      return {
        ...issueDto,
        id: issueId,
        issue_no: index,
        current_revision_id: revisionId,
        playback_target: {
          ...issueDto.playback_target,
          issue_id: issueId,
          revision_id: revisionId,
        },
        current_revision: {
          ...issueDto.current_revision,
          id: revisionId,
          issue_id: issueId,
        },
      };
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const page = Number(new URL(String(input)).searchParams.get('page'));
      const data = page === 1
        ? Array.from({ length: 200 }, (_, index) => issueForIndex(index + 1))
        : [issueForIndex(201)];
      return new Response(JSON.stringify({
        data,
        meta: { total_count: 201, page, page_size: 200 },
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    const issues = await new HttpReviewQueries(reviewTransport()).issuesForVersion(
      'project_1',
      'item_1',
      'version_1',
    );

    expect(issues).toHaveLength(201);
    expect(issues[0].issueId).toBe('issue_1');
    expect(issues.at(-1)?.issueId).toBe('issue_201');
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls.map(([input]) => new URL(String(input)).searchParams.get('page'))).toEqual([
      '1',
      '2',
    ]);
  });

  it('aggregates every page instead of silently truncating long HTTP lists', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input));
      const page = Number(url.searchParams.get('page'));
      const data = page === 1
        ? Array.from({ length: 200 }, (_, index) => ({ project_ref_id: `project_${index + 1}` }))
        : [{ project_ref_id: 'project_201' }];
      return new Response(JSON.stringify({
        data,
        meta: { total_count: 201, page, page_size: 200 },
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
    vi.stubGlobal('fetch', fetchMock);
    const baseUrl = 'https://review.example';
    const transport = new HttpReviewTransport('review', new FinalCutReviewClient(baseUrl), baseUrl);

    const records = await transport.requestList<Array<{ project_ref_id: string }>>(
      '/api/v1/final-cut-review/projects',
    );

    expect(records).toHaveLength(201);
    expect(records.at(-1)).toEqual({ project_ref_id: 'project_201' });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      `${baseUrl}/api/v1/final-cut-review/projects?page=1&page_size=200`,
      expect.objectContaining({ credentials: 'include' }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      `${baseUrl}/api/v1/final-cut-review/projects?page=2&page_size=200`,
      expect.objectContaining({ credentials: 'include' }),
    );
  });

  it('restarts from page one when an insertion changes total_count between pages', async () => {
    const fetchMock = stubListPages([
      { page: 1, totalCount: 201, data: idRecords(1, 200) },
      { page: 2, totalCount: 202, data: [{ id: 200 }, { id: 201 }] },
      { page: 1, totalCount: 202, data: [{ id: 0 }, ...idRecords(1, 199)] },
      { page: 2, totalCount: 202, data: idRecords(200, 201) },
    ]);

    const records = await reviewTransport().requestList<Array<{ id: number }>>('/projects');

    expect(records.map(({ id }) => id)).toEqual(Array.from({ length: 202 }, (_, index) => index));
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it('restarts from page one when a deletion changes total_count between pages', async () => {
    const fetchMock = stubListPages([
      { page: 1, totalCount: 201, data: idRecords(1, 200) },
      { page: 2, totalCount: 200, data: [] },
      { page: 1, totalCount: 200, data: idRecords(2, 201) },
    ]);

    const records = await reviewTransport().requestList<Array<{ id: number }>>('/projects');

    expect(records).toEqual(idRecords(2, 201));
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it('discards a snapshot when an offset boundary repeats a resource id', async () => {
    const fetchMock = stubListPages([
      { page: 1, totalCount: 201, data: idRecords(1, 200) },
      { page: 2, totalCount: 201, data: [{ id: 200 }] },
      { page: 1, totalCount: 201, data: idRecords(1, 200) },
      { page: 2, totalCount: 201, data: [{ id: 201 }] },
    ]);

    const records = await reviewTransport().requestList<Array<{ id: number }>>('/projects');

    expect(records).toEqual(idRecords(1, 201));
    expect(new Set(records.map(({ id }) => id))).toHaveLength(201);
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it('returns only after a bounded retry sequence reaches a valid snapshot', async () => {
    const fetchMock = stubListPages([
      { page: 1, totalCount: 201, data: idRecords(1, 200) },
      { page: 2, totalCount: 202, data: [{ id: 201 }, { id: 202 }] },
      { page: 1, totalCount: 201, data: idRecords(1, 200) },
      { page: 2, totalCount: 201, data: [{ id: 200 }] },
      { page: 1, totalCount: 201, data: idRecords(1, 200) },
      { page: 2, totalCount: 201, data: [{ id: 201 }] },
    ]);

    const records = await reviewTransport().requestList<Array<{ id: number }>>('/projects');

    expect(records).toEqual(idRecords(1, 201));
    expect(fetchMock).toHaveBeenCalledTimes(6);
  });

  it('fails closed when pagination keeps changing across every snapshot attempt', async () => {
    const changingPages = Array.from({ length: 3 }, () => [
      { page: 1, totalCount: 201, data: idRecords(1, 200) },
      { page: 2, totalCount: 201, data: [{ id: 200 }] },
    ]).flat();
    const fetchMock = stubListPages(changingPages);

    await expect(reviewTransport().requestList('/projects')).rejects.toThrow(
      'HTTP list pagination did not stabilize after 3 snapshot attempts: resource id 200 was repeated on page 2',
    );
    expect(fetchMock).toHaveBeenCalledTimes(6);
  });
});

describe('HttpReviewQueries technical QC progress', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('uses the project-scoped running progress resource and maps only episode media time fields', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({
      data: {
        episodes: [
          { review_item_id: 'item-2', episode_no: 2, processed_ms: 250, total_ms: 1_000 },
          { review_item_id: 'item-1', episode_no: 1, processed_ms: 500, total_ms: 2_000 },
        ],
      },
      meta: { request_id: 'technical-qc-progress', contract_version: '1.0' },
    }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }));
    vi.stubGlobal('fetch', fetchMock);
    const queries = new HttpReviewQueries(reviewTransport());

    const progress = await queries.technicalQcProgress('project_1');

    expect(fetchMock).toHaveBeenCalledWith(
      'https://review.example/api/v1/final-cut-review/projects/project_1/technical-qc-progress',
      expect.objectContaining({ credentials: 'include' }),
    );
    expect(progress).toEqual({
      episodes: [
        { reviewItemId: 'item-2', episodeNo: 2, processedMs: 250, totalMs: 1_000 },
        { reviewItemId: 'item-1', episodeNo: 1, processedMs: 500, totalMs: 2_000 },
      ],
    });
    expect(Object.keys(progress)).toEqual(['episodes']);
    expect(Object.keys(progress.episodes[0])).toEqual([
      'reviewItemId',
      'episodeNo',
      'processedMs',
      'totalMs',
    ]);
  });
});
