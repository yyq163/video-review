import { afterEach, describe, expect, it, vi } from 'vitest';
import { createReviewRuntime } from '../entry/runtime';

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('HTTP project creation without crypto.randomUUID', () => {
  it('uses getRandomValues for both request and command IDs on a LAN HTTP origin', async () => {
    let byteOffset = 0;
    const getRandomValues = vi.fn((target: Uint8Array) => {
      target.set(Array.from({ length: target.length }, (_, index) => (byteOffset + index) & 0xff));
      byteOffset += target.length;
      return target;
    });
    vi.stubGlobal('crypto', { getRandomValues });

    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(String(input)).toBe('http://192.0.2.51:18171/api/v1/final-cut-review/edit/projects');
      expect(init?.method).toBe('POST');
      const headers = init?.headers as Record<string, string>;
      const command = JSON.parse(String(init?.body)) as {
        command_id: string;
        command_type: string;
        contract_version: string;
        payload: Record<string, unknown>;
      };
      expect(headers['X-Request-ID']).toBe('00010203-0405-4607-8809-0a0b0c0d0e0f');
      expect(command).toEqual({
        command_id: 'CreateProject_10111213-1415-4617-9819-1a1b1c1d1e1f',
        command_type: 'CreateProject',
        contract_version: '1.0',
        payload: {
          project_code: 'QA-HTTP',
          project_name: 'LAN HTTP project',
          description: 'UUID fallback regression',
        },
      });
      expect(headers['Idempotency-Key']).toBe(command.command_id);

      return new Response(
        JSON.stringify({
          data: {
            project_ref_id: 'prj_lan_http',
            project_code: 'QA-HTTP',
            project_name: 'LAN HTTP project',
            description: 'UUID fallback regression',
            source: 'local',
            external_project_id: null,
            lifecycle_status: 'active',
            completion_status: 'empty',
            deleted_at: null,
            lock_version: 1,
            created_at: '2026-07-16T00:00:00.000Z',
            updated_at: '2026-07-16T00:00:00.000Z',
          },
          meta: { request_id: 'req-lan-http', contract_version: '1.0' },
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      );
    });
    vi.stubGlobal('fetch', fetchMock);

    const runtime = createReviewRuntime({ apiBaseUrl: 'http://192.0.2.51:18171' });
    const api = runtime.getApi('edit');
    const context = api.entryPolicy.createContext('edit');
    const project = await api.createProject(
      {
        name: 'LAN HTTP project',
        code: 'QA-HTTP',
        description: 'UUID fallback regression',
      },
      context,
    );

    expect(context.requestId).toBe('00010203-0405-4607-8809-0a0b0c0d0e0f');
    expect(project).toMatchObject({ projectRefId: 'prj_lan_http', code: 'QA-HTTP', name: 'LAN HTTP project' });
    expect(getRandomValues).toHaveBeenCalledTimes(2);
    expect(fetchMock).toHaveBeenCalledOnce();
    runtime.dispose();
  });
});
