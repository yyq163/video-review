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
