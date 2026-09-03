import { afterEach, describe, expect, it, vi } from 'vitest';
import { createReviewRuntime } from '../entry/runtime';

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('HTTP technical QC rerun command', () => {
  it('posts an idempotent edit command and maps the accepted result', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(String(input)).toBe(
        'http://192.0.2.51:18171/api/v1/final-cut-review/edit/projects/prj_qc/technical-qc/rerun',
      );
      expect(init?.method).toBe('POST');
      const headers = init?.headers as Record<string, string>;
      const envelope = JSON.parse(String(init?.body)) as {
        command_id: string;
        command_type: string;
        payload: Record<string, unknown>;
      };
      expect(envelope.command_type).toBe('RerunProjectTechnicalQc');
      expect(envelope.payload).toEqual({ project_ref_id: 'prj_qc' });
      expect(envelope.command_id).toMatch(/^RerunProjectTechnicalQc_/);
      expect(headers['Idempotency-Key']).toBe(envelope.command_id);

      return new Response(
        JSON.stringify({
          data: {
            project_ref_id: 'prj_qc',
            status: 'queued',
            queued_item_count: 12,
          },
          meta: { request_id: 'req-rerun-qc', contract_version: '1.0' },
        }),
        { status: 202, headers: { 'Content-Type': 'application/json' } },
      );
    });
    vi.stubGlobal('fetch', fetchMock);

    const runtime = createReviewRuntime({
      apiBaseUrl: 'http://192.0.2.51:18171',
    });
    const api = runtime.getApi('edit');
    const context = api.entryPolicy.createContext('edit');

    const result = await api.rerunProjectTechnicalQc(
      { projectRefId: 'prj_qc' },
      context,
    );

    expect(result).toEqual({
      projectRefId: 'prj_qc',
      status: 'queued',
      queuedItemCount: 12,
    });
    expect(fetchMock).toHaveBeenCalledOnce();
    runtime.dispose();
  });

  it('posts the same command through the review entry route', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(String(input)).toBe(
        'http://192.0.2.51:18171/api/v1/final-cut-review/review/projects/prj_qc/technical-qc/rerun',
      );
      expect(init?.method).toBe('POST');
      const envelope = JSON.parse(String(init?.body)) as {
        command_id: string;
        command_type: string;
        payload: Record<string, unknown>;
      };
      expect(envelope.command_type).toBe('RerunProjectTechnicalQc');
      expect(envelope.payload).toEqual({ project_ref_id: 'prj_qc' });

      return new Response(
        JSON.stringify({
          data: {
            project_ref_id: 'prj_qc',
            status: 'queued',
            queued_item_count: 3,
          },
          meta: { request_id: 'req-review-rerun-qc', contract_version: '1.0' },
        }),
        { status: 202, headers: { 'Content-Type': 'application/json' } },
      );
    });
    vi.stubGlobal('fetch', fetchMock);

    const runtime = createReviewRuntime({
      apiBaseUrl: 'http://192.0.2.51:18171',
    });
    const api = runtime.getApi('review');
    const context = api.entryPolicy.createContext('review');

    const result = await api.rerunProjectTechnicalQc(
      { projectRefId: 'prj_qc' },
      context,
    );

    expect(result).toEqual({
      projectRefId: 'prj_qc',
      status: 'queued',
      queuedItemCount: 3,
    });
    expect(fetchMock).toHaveBeenCalledOnce();
    runtime.dispose();
  });
});
