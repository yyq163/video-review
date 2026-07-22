import { afterEach, describe, expect, it, vi } from 'vitest';
import { createReviewRuntime } from '../entry/runtime';

function envelope(data: object) {
  return new Response(
    JSON.stringify({ data, meta: { request_id: 'req-metadata', contract_version: '1.0' } }),
    { status: 200, headers: { 'Content-Type': 'application/json' } },
  );
}

describe('HTTP metadata commands', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('updates editable fields with lock versions and never sends immutable codes', async () => {
    const project = {
      project_ref_id: 'prj_metadata',
      project_code: 'PROJECT-001',
      project_name: 'Original project',
      description: 'Original description',
      source: 'local',
      external_project_id: null,
      lifecycle_status: 'active',
      completion_status: 'in_progress',
      deleted_at: null,
      lock_version: 4,
      created_at: '2026-07-14T00:00:00.000Z',
      updated_at: '2026-07-14T00:00:00.000Z',
    };
    const item = {
      id: 'item_metadata',
      project_ref_id: project.project_ref_id,
      item_code: 'ITEM-001',
      episode_no: 28,
      title: 'Original item',
      workflow_status: 'pending_review',
      current_version_id: 'ver_metadata',
      current_version_no: 1,
      ui_status: '待审核',
      active_finalization_id: null,
      unresolved_current_version_count: 0,
      resolved_current_version_count: 0,
      historical_version_count: 0,
      is_finalized: false,
      lock_version: 7,
      created_at: '2026-07-14T00:00:00.000Z',
      updated_at: '2026-07-14T00:00:00.000Z',
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? 'GET';
      if (url.endsWith(`/projects/${project.project_ref_id}`) && method === 'GET') {
        return envelope(project);
      }
      if (url.endsWith(`/edit/projects/${project.project_ref_id}`) && method === 'PATCH') {
        const command = JSON.parse(String(init?.body)) as { payload: Record<string, unknown> };
        expect(command.payload).toEqual({
          project_ref_id: project.project_ref_id,
          project_name: 'Updated project',
          description: '',
        });
        expect(init?.headers).toMatchObject({ 'If-Match': '4' });
        return envelope({ ...project, project_name: 'Updated project', description: '', lock_version: 5 });
      }
      if (url.endsWith(`/projects/${project.project_ref_id}/items/${item.id}`) && method === 'GET') {
        return envelope(item);
      }
      if (url.endsWith(`/edit/projects/${project.project_ref_id}/items/${item.id}`) && method === 'PATCH') {
        const command = JSON.parse(String(init?.body)) as { payload: Record<string, unknown> };
        expect(command.payload).toEqual({
          project_ref_id: project.project_ref_id,
          review_item_id: item.id,
          title: 'Updated item',
          episode_no: 29,
        });
        expect(init?.headers).toMatchObject({ 'If-Match': '7' });
        return envelope({ ...item, title: 'Updated item', episode_no: 29, lock_version: 8 });
      }
      throw new Error(`Unexpected request: ${method} ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    const runtime = createReviewRuntime({ apiBaseUrl: 'https://review.example' });
    const api = runtime.getApi('edit');
    const context = api.entryPolicy.createContext('edit');
    const updatedProject = await api.updateProject(
      {
        projectRefId: project.project_ref_id,
        name: 'Updated project',
        code: 'ATTEMPTED-CODE-CHANGE',
        description: '',
      },
      context,
    );
    const updatedItem = await api.updateReviewItem(
      {
        projectRefId: project.project_ref_id,
        reviewItemId: item.id,
        title: 'Updated item',
        episode: '29',
      },
      context,
    );

    expect(updatedProject).toMatchObject({ code: 'PROJECT-001', name: 'Updated project', description: '' });
    expect(updatedItem).toMatchObject({ title: 'Updated item', episode: '29', itemCode: 'ITEM-001' });
    runtime.dispose();
  });

  it('preserves a nonnumeric item code when a title-only edit omits episode_no', async () => {
    const item = {
      id: 'item_nonnumeric',
      project_ref_id: 'prj_metadata',
      item_code: 'CUT-A',
      episode_no: null,
      title: 'Original cut',
      workflow_status: 'pending_review',
      current_version_id: 'ver_nonnumeric',
      current_version_no: 1,
      ui_status: '待审核',
      active_finalization_id: null,
      unresolved_current_version_count: 0,
      resolved_current_version_count: 0,
      historical_version_count: 0,
      is_finalized: false,
      lock_version: 3,
      created_at: '2026-07-14T00:00:00.000Z',
      updated_at: '2026-07-14T00:00:00.000Z',
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? 'GET';
      if (url.endsWith(`/projects/${item.project_ref_id}/items/${item.id}`) && method === 'GET') {
        return envelope(item);
      }
      if (url.endsWith(`/edit/projects/${item.project_ref_id}/items/${item.id}`) && method === 'PATCH') {
        const command = JSON.parse(String(init?.body)) as { payload: Record<string, unknown> };
        expect(command.payload).toEqual({
          project_ref_id: item.project_ref_id,
          review_item_id: item.id,
          title: 'Renamed cut',
        });
        return envelope({ ...item, title: 'Renamed cut', lock_version: 4 });
      }
      throw new Error(`Unexpected request: ${method} ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    const runtime = createReviewRuntime({ apiBaseUrl: 'https://review.example' });
    const api = runtime.getApi('edit');
    const updatedItem = await api.updateReviewItem(
      {
        projectRefId: item.project_ref_id,
        reviewItemId: item.id,
        title: 'Renamed cut',
        episode: 'CUT-A',
      },
      api.entryPolicy.createContext('edit'),
    );

    expect(updatedItem).toMatchObject({ title: 'Renamed cut', episode: 'CUT-A', itemCode: 'CUT-A' });
    runtime.dispose();
  });
});
