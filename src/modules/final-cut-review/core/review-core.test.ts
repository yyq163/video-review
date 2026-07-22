import { afterEach, describe, expect, it, vi } from 'vitest';
import type { EntryMode } from '../contracts/types';
import type { ReviewApiPort, ReviewHostBridge } from '../ports';
import { FinalCutReviewClient, FinalCutReviewHttpError, finalCutReviewRequest } from '../contracts-generated/backend-contract';
import { createReviewRuntime } from '../entry/runtime';
import { createSeedData } from './seed';
import { InMemoryReviewRepository } from '../adapters/in-memory-review-repository';
import { MockFileStorageAdapter } from '../adapters/mock-file-storage-adapter';
import { MockFinalizedPackageAdapter } from '../adapters/mock-finalized-package-adapter';
import {
  NoAccountEntryPolicyAdapter,
  NoAccountPermissionAdapter,
  NoAccountPrincipalAuthorizationAdapter,
} from '../adapters/no-account-permission-adapter';
import { SimpleWriteGuardAdapter } from '../adapters/simple-write-guard-adapter';
import { MockReviewApiAdapter } from '../adapters/mock-review-api-adapter';
import { HttpReviewApiAdapter } from '../adapters/http-review-api-adapter';
import {
  clearV1ListConfirmationRequired,
  getV1ListProtectionState,
  isV1ListConfirmationRequired,
} from '../adapters/http-review-uploads';
import { BrowserReviewHostBridge } from '../host/review-host-bridge';
import { sanitizeDownloadFileName, sanitizeFileSegment } from './file-names';
import {
  formatReviewTimecode,
  frameFromTimestampMs,
  parseReviewTimecode,
  timestampMsFromFrame,
} from './timecode';
import { computeContainedVideoRect, pointerToNormalizedVideoPoint } from './coordinates';
import { playbackTargetFromIssue, sortedIssuesForPlayback } from './playback';

function demoFile(name: string): File {
  return new File([`demo ${name}`], name, { type: 'video/mp4' });
}

type BinaryUploadResponder = (url: string, init: RequestInit) => Promise<Response>;
type BinaryUploadXhrListener = (event: Event) => void;

function stubBinaryUploadXhr(responder: BinaryUploadResponder): void {
  class BinaryUploadXMLHttpRequest {
    readonly upload = { addEventListener: () => undefined };
    withCredentials = false;
    status = 0;
    responseText = '';
    private method = '';
    private url = '';
    private readonly headers = new Headers();
    private readonly listeners = new Map<string, BinaryUploadXhrListener[]>();

    addEventListener(type: string, listener: EventListenerOrEventListenerObject): void {
      const callback = typeof listener === 'function'
        ? listener as BinaryUploadXhrListener
        : (event: Event) => listener.handleEvent(event);
      const listeners = this.listeners.get(type) ?? [];
      listeners.push(callback);
      this.listeners.set(type, listeners);
    }

    open(method: string, url: string | URL): void {
      this.method = method;
      this.url = String(url);
    }

    setRequestHeader(name: string, value: string): void {
      this.headers.set(name, value);
    }

    send(body: Document | XMLHttpRequestBodyInit | null = null): void {
      void responder(this.url, {
        method: this.method,
        headers: this.headers,
        body: body instanceof Blob ? body : undefined,
      }).then(async (response) => {
        this.status = response.status;
        this.responseText = await response.text();
        this.emit('load');
      }).catch(() => this.emit('error'));
    }

    abort(): void {
      this.emit('abort');
    }

    private emit(type: string): void {
      const event = { type } as Event;
      for (const listener of this.listeners.get(type) ?? []) listener(event);
    }
  }

  vi.stubGlobal('XMLHttpRequest', BinaryUploadXMLHttpRequest as unknown as typeof XMLHttpRequest);
}

function makeHarness() {
  const seed = createSeedData();
  const repository = new InMemoryReviewRepository(seed);
  const storage = new MockFileStorageAdapter();
  const packageAdapter = new MockFinalizedPackageAdapter();
  const permissions = new NoAccountPermissionAdapter();
  const authorization = new NoAccountPrincipalAuthorizationAdapter(permissions);
  const entryPolicy = new NoAccountEntryPolicyAdapter();
  const guard = new SimpleWriteGuardAdapter(permissions);
  for (const file of seed.files) storage.seedOriginal(file);

  const makeApi = (mode: EntryMode): ReviewApiPort => {
    const host: ReviewHostBridge = {
      entryMode: mode,
      getAuthorizationAdapter: () => authorization,
      notify: () => undefined,
      downloadBlob: () => undefined,
      downloadUrl: () => undefined,
    };
    return new MockReviewApiAdapter(mode, repository, storage, packageAdapter, guard, authorization, entryPolicy, host);
  };

  return { edit: makeApi('edit'), review: makeApi('review'), repository, storage };
}

function ctx(api: ReviewApiPort) {
  return api.entryPolicy.createContext(api.mode);
}

it('stores mock originals without reading the entire selected file', async () => {
  const storage = new MockFileStorageAdapter();
  const arrayBuffer = vi.fn(async () => {
    throw new Error('mock storage must not read the whole file');
  });
  const file = new File([new Uint8Array(1)], 'large.mov', { type: 'video/quicktime', lastModified: 123 });
  Object.defineProperty(file, 'size', { value: 2_400_000_000 });
  Object.defineProperty(file, 'arrayBuffer', { value: arrayBuffer });

  const stored = await storage.storeOriginal(file);

  expect(stored.fileName).toBe('large.mov');
  expect(stored.size).toBe(2_400_000_000);
  expect(stored.blob).toBe(file);
  expect(arrayBuffer).not.toHaveBeenCalled();
});

it('restores mock repository state from a snapshot and relinks uploaded originals', async () => {
  const repository = new InMemoryReviewRepository(createSeedData());
  const storage = new MockFileStorageAdapter();
  const file = await storage.storeOriginal(demoFile('persisted-v1.mp4'));
  const project = await repository.createProject({
    name: '持久化测试',
    code: 'persist-test',
    description: '验证 mock runtime 跨页面恢复。',
  });
  const created = await repository.createReviewItemWithVersion({
    projectRefId: project.projectRefId,
    title: '第一集',
    episode: '01',
    file,
  });

  const restored = new InMemoryReviewRepository(repository.snapshot());
  restored.relinkOriginalFiles([{ ...file, playbackUrl: 'blob:restored-original' }]);

  const detail = await restored.getProjectDetail(project.projectRefId);
  const workspace = await restored.getWorkspace({
    projectRefId: project.projectRefId,
    reviewItemId: created.item.reviewItemId,
  });

  expect(detail.items).toHaveLength(1);
  expect(workspace.currentVersion.fileName).toBe('persisted-v1.mp4');
  expect(workspace.currentVersion.playbackUrl).toBe('blob:restored-original');
});

describe('timecode and frame rate', () => {
  it.each([
    [24, 1, 1000, 24, '00:00:01:00'],
    [25, 1, 1920, 48, '00:00:01:23'],
    [30, 1, 1000, 30, '00:00:01:00'],
    [24000, 1001, 1001, 24, '00:00:01:00'],
    [30000, 1001, 1001, 30, '00:00:01:00'],
  ])('converts timestamp/frame/timecode for %i/%i', (fpsNum, fpsDen, timestampMs, frame, timecode) => {
    expect(frameFromTimestampMs(timestampMs, fpsNum, fpsDen)).toBe(frame);
    expect(formatReviewTimecode(frame, fpsNum, fpsDen)).toBe(timecode);
    expect(timestampMsFromFrame(frame, fpsNum, fpsDen)).toBeLessThanOrEqual(timestampMs);
  });

  it('validates HH:MM:SS:FF input and rejects invalid fps denominator', () => {
    expect(parseReviewTimecode('00:00:01:12', 25, 1)).toBe(37);
    expect(() => parseReviewTimecode('00:00:01:25', 25, 1)).toThrow(/帧号/);
    expect(() => frameFromTimestampMs(1000, 25, 0)).toThrow(/帧率/);
  });
});

describe('video coordinate helpers', () => {
  it('computes contained video rect for 16:9 and vertical video black bars', () => {
    expect(computeContainedVideoRect({ containerWidth: 1920, containerHeight: 1080, videoWidth: 1920, videoHeight: 1080 })).toMatchObject({
      x: 0,
      y: 0,
      width: 1920,
      height: 1080,
    });
    const wideWindowRect = computeContainedVideoRect({ containerWidth: 2100, containerHeight: 900, videoWidth: 1920, videoHeight: 1080 });
    expect(wideWindowRect.width).toBeCloseTo(1600);
    expect(wideWindowRect.height).toBe(900);
    expect(wideWindowRect.x).toBeCloseTo(250);
    expect(wideWindowRect.y).toBe(0);
    const rect = computeContainedVideoRect({ containerWidth: 1920, containerHeight: 1080, videoWidth: 1080, videoHeight: 1920 });
    expect(rect.width).toBeCloseTo(607.5);
    expect(rect.x).toBeCloseTo(656.25);
    expect(rect.y).toBe(0);
  });

  it('can preserve original media size while still centering inside the stage', () => {
    const rect = computeContainedVideoRect({
      containerWidth: 2100,
      containerHeight: 900,
      videoWidth: 1280,
      videoHeight: 720,
      maxScale: 1,
    });

    expect(rect.width).toBe(1280);
    expect(rect.height).toBe(720);
    expect(rect.x).toBe(410);
    expect(rect.y).toBe(90);
  });

  it('returns null for pointers in black bars and stable normalized points inside video', () => {
    const containerRect = { left: 0, top: 0, width: 1366, height: 768 };
    expect(pointerToNormalizedVideoPoint({ clientX: 100, clientY: 384, containerRect, videoWidth: 720, videoHeight: 1280 })).toBeNull();
    const point = pointerToNormalizedVideoPoint({ clientX: 683, clientY: 384, containerRect, videoWidth: 720, videoHeight: 1280 });
    expect(point?.x).toBeCloseTo(0.5, 2);
    expect(point?.y).toBeCloseTo(0.5, 2);
  });
});

describe('generated HTTP envelope client', () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    window.sessionStorage.clear();
  });

  it('unwraps success envelopes from the generated HTTP client', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          data: [
            {
              project_ref_id: 'prj_http',
              project_code: 'HTTP',
              project_name: 'HTTP Project',
              source: 'local',
              external_project_id: null,
              lifecycle_status: 'active',
              completion_status: 'empty',
              lock_version: 1,
              created_at: '2026-06-20T00:00:00.000Z',
              updated_at: '2026-06-20T00:00:00.000Z',
            },
          ],
          meta: { request_id: 'req-http', contract_version: '1.0' },
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );
    vi.stubGlobal('fetch', fetchMock);

    const client = new FinalCutReviewClient('https://review.example');
    const projects = await client.listProjects();

    expect(projects).toHaveLength(1);
    expect(projects[0].project_ref_id).toBe('prj_http');
    expect(projects[0].created_at).toBe('2026-06-20T00:00:00.000Z');
    expect(projects[0].updated_at).toBe('2026-06-20T00:00:00.000Z');
    expect(fetchMock).toHaveBeenCalledWith(
      'https://review.example/api/v1/final-cut-review/projects',
      expect.objectContaining({ headers: expect.objectContaining({ 'Content-Type': 'application/json' }) }),
    );
  });

  it('throws the generated ErrorEnvelope with structured fields for failed HTTP responses', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            error: {
              code: 'PRINCIPAL_AUTHENTICATION_REQUIRED',
              message: 'missing principal',
              http_status: 401,
              details: {},
              request_id: 'req-error',
              timestamp: '2026-06-21T00:00:00Z',
              contract_version: '1.0',
            },
          }),
          { status: 401, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    );

    try {
      await finalCutReviewRequest('https://review.example', '/api/v1/final-cut-review/projects');
      throw new Error('expected request to fail');
    } catch (error) {
      expect(error).toBeInstanceOf(FinalCutReviewHttpError);
      expect(error).toMatchObject({
        message: 'missing principal',
        code: 'PRINCIPAL_AUTHENTICATION_REQUIRED',
        httpStatus: 401,
        details: {},
        requestId: 'req-error',
      });
    }
  });

  it('wires runtime to the generated HTTP adapter when an API base URL is configured', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          data: [
            {
              project_ref_id: 'prj_runtime_http',
              project_code: 'RHTTP',
              project_name: 'Runtime HTTP',
              source: 'local',
              external_project_id: null,
              lifecycle_status: 'active',
              completion_status: 'empty',
              lock_version: 1,
              created_at: '2026-06-20T00:00:00.000Z',
              updated_at: '2026-06-21T00:00:00.000Z',
            },
          ],
          meta: {
            request_id: 'req-runtime',
            contract_version: '1.0',
            total_count: 1,
            page: 1,
            page_size: 200,
          },
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );
    vi.stubGlobal('fetch', fetchMock);

    const runtime = createReviewRuntime({ apiBaseUrl: 'https://review.example/' });
    const projects = await runtime.getApi('review').listProjects();

    expect(projects).toEqual([
      expect.objectContaining({
        projectRefId: 'prj_runtime_http',
        code: 'RHTTP',
        name: 'Runtime HTTP',
        createdAt: '2026-06-20T00:00:00.000Z',
        updatedAt: '2026-06-21T00:00:00.000Z',
      }),
    ]);
    expect(fetchMock).toHaveBeenCalledWith(
      'https://review.example/api/v1/final-cut-review/projects?page=1&page_size=200',
      expect.objectContaining({ credentials: 'include' }),
    );
  });

  it('replays one V1 operation after a structured 5xx or version-read failure on the same page', async () => {
    const entryPolicy = new NoAccountEntryPolicyAdapter();
    let createCommandCalls = 0;
    const createCommandIds: string[] = [];
    let versionReads = 0;
    const media = {
      original_file_id: 'file_v1_retry',
      original_filename: 'retry.mp4',
      mime_type: 'video/mp4',
      file_size: 10,
      sha256: 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad',
      duration_ms: 1000,
      width: 1920,
      height: 1080,
      fps_num: 25,
      fps_den: 1,
      media_probe_version: 'http-test',
    };
    const item = {
      id: 'item_v1_retry',
      project_ref_id: 'prj_v1_retry',
      item_code: '28',
      episode_no: 28,
      title: 'Retry V1',
      workflow_status: 'pending_review' as const,
      current_version_id: 'ver_v1_retry',
      current_version_no: 1,
      ui_status: '待审核',
      active_finalization_id: null,
      unresolved_current_version_count: 0,
      resolved_current_version_count: 0,
      historical_version_count: 0,
      is_finalized: false,
      lock_version: 1,
      created_at: '2026-07-13T00:00:00.000Z',
      updated_at: '2026-07-13T00:00:00.000Z',
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/v1/final-cut-review/projects/prj_v1_retry')) {
        return new Response(JSON.stringify({
          data: {
            project_ref_id: 'prj_v1_retry',
            project_code: 'V1R',
            project_name: 'V1 retry',
            source: 'local',
            lifecycle_status: 'active',
            completion_status: 'empty',
            lock_version: 1,
            created_at: '2026-07-13T00:00:00.000Z',
            updated_at: '2026-07-13T00:00:00.000Z',
          },
          meta: { request_id: 'req-project', contract_version: '1.0' },
        }), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      if (url.endsWith('/api/v1/final-cut-review/edit/projects/prj_v1_retry/items')) {
        createCommandCalls += 1;
        const commandId = (init?.headers as Record<string, string>)['Idempotency-Key'];
        expect(commandId).toContain('CreateReviewItem_');
        createCommandIds.push(commandId);
        if (createCommandCalls === 1) {
          return new Response(JSON.stringify({
            error: {
              code: 'STORAGE_UNAVAILABLE',
              message: 'command response uncertain after commit',
              http_status: 503,
              details: {},
              request_id: 'req-command-failed',
              timestamp: '2026-07-13T00:00:00.000Z',
              contract_version: '1.0',
            },
          }), { status: 503, headers: { 'Content-Type': 'application/json' } });
        }
        return new Response(JSON.stringify({
          data: item,
          meta: { request_id: 'req-create', contract_version: '1.0' },
        }), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      if (url.endsWith('/api/v1/final-cut-review/projects/prj_v1_retry/items/item_v1_retry/versions/ver_v1_retry')) {
        versionReads += 1;
        if (versionReads === 1) {
          return new Response(JSON.stringify({
            error: {
              code: 'STORAGE_UNAVAILABLE',
              message: 'temporary read failure',
              http_status: 503,
              details: {},
              request_id: 'req-version-failed',
              timestamp: '2026-07-13T00:00:00.000Z',
              contract_version: '1.0',
            },
          }), { status: 503, headers: { 'Content-Type': 'application/json' } });
        }
        return new Response(JSON.stringify({
          data: {
            id: 'ver_v1_retry',
            project_ref_id: 'prj_v1_retry',
            review_item_id: 'item_v1_retry',
            previous_version_id: null,
            version_no: 1,
            version_label: 'V1',
            is_current: true,
            original_media: media,
            playback_status: 'ready',
            playback_asset_id: null,
            thumbnail_asset_id: null,
            version_note: null,
            change_summary: null,
            lock_version: 1,
            created_at: '2026-07-13T00:00:00.000Z',
          },
          meta: { request_id: 'req-version-ready', contract_version: '1.0' },
        }), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      throw new Error(`unexpected URL ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    const api = new HttpReviewApiAdapter(
      'edit',
      new FinalCutReviewClient('https://review.example'),
      entryPolicy,
      'https://review.example',
      {
        entryMode: 'edit',
        notify: () => undefined,
        downloadBlob: () => undefined,
        downloadUrl: () => undefined,
      },
    );
    const uploadSpy = vi.spyOn(
      api as unknown as { uploadFile: (file: File) => Promise<{ file_id: string }> },
      'uploadFile',
    ).mockResolvedValue({ file_id: 'file_v1_retry' });
    const file = demoFile('retry.mp4');
    const input = { projectRefId: 'prj_v1_retry', title: 'Retry V1', episode: '28', file };

    await expect(api.createReviewItemWithVersion(input, entryPolicy.createContext('edit'))).rejects.toThrow(
      'command response uncertain after commit',
    );
    expect(isV1ListConfirmationRequired('prj_v1_retry')).toBe(true);
    await expect(api.createReviewItemWithVersion(input, entryPolicy.createContext('edit'))).rejects.toThrow(
      'temporary read failure',
    );
    const retried = await api.createReviewItemWithVersion(input, entryPolicy.createContext('edit'));
    const cachedRetry = await api.createReviewItemWithVersion(input, entryPolicy.createContext('edit'));

    expect(retried.item.reviewItemId).toBe('item_v1_retry');
    expect(retried.version.versionId).toBe('ver_v1_retry');
    expect(cachedRetry.item.reviewItemId).toBe('item_v1_retry');
    expect(cachedRetry.version.versionId).toBe('ver_v1_retry');
    expect(uploadSpy).toHaveBeenCalledTimes(1);
    expect(createCommandCalls).toBe(2);
    expect(new Set(createCommandIds).size).toBe(1);
    expect(versionReads).toBe(2);
    expect(isV1ListConfirmationRequired('prj_v1_retry')).toBe(true);
    clearV1ListConfirmationRequired('prj_v1_retry');
    expect(isV1ListConfirmationRequired('prj_v1_retry')).toBe(false);
  });

  it('fails V1 protection closed when session storage reads are unavailable', () => {
    const getItem = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('storage blocked', 'SecurityError');
    });

    expect(getV1ListProtectionState('prj_storage_blocked')).toBe('storage-unavailable');
    expect(isV1ListConfirmationRequired('prj_storage_blocked')).toBe(true);

    getItem.mockRestore();
  });

  it('starts a distinct V1 operation when editable fields change for the same file', async () => {
    const entryPolicy = new NoAccountEntryPolicyAdapter();
    const commandIds: string[] = [];
    let itemNumber = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/v1/final-cut-review/projects/prj_v1_fields')) {
        return new Response(JSON.stringify({
          data: {
            project_ref_id: 'prj_v1_fields', project_code: 'V1F', project_name: 'V1 fields', source: 'local',
            external_project_id: null, lifecycle_status: 'active', completion_status: 'empty', lock_version: 1,
            created_at: '2026-07-13T00:00:00.000Z', updated_at: '2026-07-13T00:00:00.000Z',
          },
          meta: { request_id: 'req-project', contract_version: '1.0' },
        }), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      if (url.endsWith('/api/v1/final-cut-review/edit/projects/prj_v1_fields/items')) {
        itemNumber += 1;
        commandIds.push((init?.headers as Record<string, string>)['Idempotency-Key']);
        return new Response(JSON.stringify({
          data: {
            id: `item_v1_fields_${itemNumber}`, project_ref_id: 'prj_v1_fields', item_code: String(itemNumber),
            episode_no: itemNumber, title: `Fields ${itemNumber}`, workflow_status: 'pending_review',
            current_version_id: `ver_v1_fields_${itemNumber}`, current_version_no: 1, ui_status: '待审核',
            active_finalization_id: null, unresolved_current_version_count: 0, resolved_current_version_count: 0,
            historical_version_count: 0, is_finalized: false, lock_version: 1,
            created_at: '2026-07-13T00:00:00.000Z', updated_at: '2026-07-13T00:00:00.000Z',
          },
          meta: { request_id: `req-create-${itemNumber}`, contract_version: '1.0' },
        }), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      const versionMatch = url.match(/items\/item_v1_fields_(\d+)\/versions\/ver_v1_fields_(\d+)$/);
      if (versionMatch) {
        const number = Number(versionMatch[1]);
        return new Response(JSON.stringify({
          data: {
            id: `ver_v1_fields_${number}`, project_ref_id: 'prj_v1_fields', review_item_id: `item_v1_fields_${number}`,
            previous_version_id: null, version_no: 1, version_label: 'V1', is_current: true,
            original_media: {
              original_file_id: `file_v1_fields_${number}`, original_filename: 'fields.mp4', mime_type: 'video/mp4',
              file_size: 10, sha256: 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad',
              duration_ms: 1000, width: 1920, height: 1080, fps_num: 25, fps_den: 1, media_probe_version: 'http-test',
            },
            playback_status: 'ready', playback_asset_id: null, thumbnail_asset_id: null, version_note: null,
            change_summary: null, lock_version: 1, created_at: '2026-07-13T00:00:00.000Z',
          },
          meta: { request_id: `req-version-${number}`, contract_version: '1.0' },
        }), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      throw new Error(`unexpected URL ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    const api = new HttpReviewApiAdapter(
      'edit', new FinalCutReviewClient('https://review.example'), entryPolicy, 'https://review.example',
      { entryMode: 'edit', notify: () => undefined, downloadBlob: () => undefined, downloadUrl: () => undefined },
    );
    const uploadSpy = vi.spyOn(
      api as unknown as { uploadFile: (file: File) => Promise<{ file_id: string }> },
      'uploadFile',
    ).mockImplementation(async () => ({ file_id: `file_v1_fields_${itemNumber + 1}` }));
    const file = demoFile('fields.mp4');

    await api.createReviewItemWithVersion(
      { projectRefId: 'prj_v1_fields', title: 'Fields 1', episode: '1', file },
      entryPolicy.createContext('edit'),
    );
    await api.createReviewItemWithVersion(
      { projectRefId: 'prj_v1_fields', title: 'Fields 2', episode: '2', file },
      entryPolicy.createContext('edit'),
    );

    expect(uploadSpy).toHaveBeenCalledTimes(2);
    expect(commandIds).toHaveLength(2);
    expect(commandIds[0]).not.toBe(commandIds[1]);
  });

  it.each(['edit', 'review'] as const)('downloads finalized originals through a native URL in the HTTP %s adapter', async (mode) => {
    const downloadUrl = vi.fn();
    const media = {
      original_file_id: 'file_finalized_http',
      original_filename: 'Finalized Clip.mp4',
      mime_type: 'video/mp4',
      file_size: 10,
      sha256: 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad',
      duration_ms: 1000,
      width: 1920,
      height: 1080,
      fps_num: 25,
      fps_den: 1,
      media_probe_version: 'http-test',
    };
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      expect(init?.credentials).toBe('include');
      if (url.endsWith('/api/v1/final-cut-review/projects/prj_http/items/item_http/finalization')) {
        return new Response(
          JSON.stringify({
            data: {
              id: 'fin_http',
              project_ref_id: 'prj_http',
              review_item_id: 'item_http',
              version_id: 'ver_http',
              version_no: 2,
              original_media: media,
              status: 'active',
              finalized_at: '2026-07-10T00:00:00.000Z',
            },
            meta: { request_id: 'req-finalization', contract_version: '1.0' },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      }
      throw new Error(`unexpected URL ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    const entryPolicy = new NoAccountEntryPolicyAdapter();
    const api = new HttpReviewApiAdapter(
      mode,
      new FinalCutReviewClient('https://review.example'),
      entryPolicy,
      'https://review.example',
      {
        entryMode: 'review',
        notify: () => undefined,
        downloadBlob: () => undefined,
        downloadUrl,
      },
    );
    const result = await api.downloadFinalizedOriginal(
      { projectRefId: 'prj_http', reviewItemId: 'item_http' },
      entryPolicy.createContext(mode),
    );

    expect(result.fileName).toBe('Finalized Clip.mp4');
    expect(result.blob).toBeUndefined();
    expect(downloadUrl).toHaveBeenCalledWith(
      'https://review.example/api/v1/final-cut-review/projects/prj_http/items/item_http/finalized-original/download',
      'Finalized Clip.mp4',
    );
  });

  it('prepares finalized project packages before starting a native authorized download', async () => {
    const downloadUrl = vi.fn();
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/api/v1/final-cut-review/review/projects/prj_http/finalized-originals/packages')) {
        expect(init?.method).toBe('POST');
        expect(init?.credentials).toBe('include');
        expect(init?.headers).toEqual(expect.objectContaining({ 'Idempotency-Key': expect.stringContaining('PrepareFinalizedPackage_') }));
        return new Response(
          JSON.stringify({
            data: {
              id: 'pkg_http',
              project_ref_id: 'prj_http',
              status: 'ready',
              package_filename: 'HTTP Finalized Package.zip',
              expires_at: '2026-07-10T01:00:00.000Z',
              file_count: 1,
              total_bytes: 12,
              sha256: 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad',
              download_token: 'signed-package-token',
              download_token_expires_at: '2099-07-10T00:10:00.000Z',
              created_at: '2026-07-10T00:00:00.000Z',
              updated_at: '2026-07-10T00:00:00.000Z',
              items: [
                {
                  review_item_id: 'item_http',
                  version_id: 'ver_http',
                  finalization_id: 'fin_http',
                  original_file_id: 'file_http',
                  original_filename: 'clip.mp4',
                  sha256: 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad',
                  archive_name: 'clip.mp4',
                },
              ],
            },
            meta: { request_id: 'req-package', contract_version: '1.0' },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      }
      if (url.endsWith('/api/v1/final-cut-review/review/projects/prj_http/finalized-originals/packages/pkg_http/download-session')) {
        expect(url).not.toContain('download_token=');
        expect(init?.method).toBe('POST');
        expect(init?.credentials).toBe('include');
        expect(init?.headers).toEqual(expect.objectContaining({ 'X-Package-Download-Token': 'signed-package-token' }));
        return new Response(JSON.stringify({ data: { status: 'ready' }, meta: { request_id: 'req-download', contract_version: '1.0' } }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      throw new Error(`unexpected URL ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    const entryPolicy = new NoAccountEntryPolicyAdapter();
    const api = new HttpReviewApiAdapter(
      'review',
      new FinalCutReviewClient('https://review.example'),
      entryPolicy,
      'https://review.example',
      {
        entryMode: 'review',
        notify: () => undefined,
        downloadBlob: () => undefined,
        downloadUrl,
      },
    );

    const result = await api.createProjectFinalizedPackage('prj_http', entryPolicy.createContext('review'));

    expect(result.fileName).toBe('HTTP Finalized Package.zip');
    expect(result.blob).toBeUndefined();
    expect(downloadUrl).not.toHaveBeenCalled();
    await api.downloadProjectFinalizedPackage(result, entryPolicy.createContext('review'));
    expect(downloadUrl).toHaveBeenCalledWith(
      'https://review.example/api/v1/final-cut-review/review/projects/prj_http/finalized-originals/packages/pkg_http/download',
      'HTTP Finalized Package.zip',
    );
    await expect(api.downloadProjectFinalizedPackage(result, entryPolicy.createContext('review'))).rejects.toThrow(
      '项目包下载授权已失效，请重新准备。',
    );
  });

  it('rejects a failed HTTP package snapshot instead of presenting it as download-ready', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            data: {
              id: 'pkg_failed',
              project_ref_id: 'prj_http',
              status: 'failed',
              package_filename: 'failed.zip',
              expires_at: '2026-07-10T01:00:00.000Z',
              file_count: 0,
              total_bytes: 0,
              failure_details: { error_code: 'PACKAGE_SOURCE_MISSING' },
              created_at: '2026-07-10T00:00:00.000Z',
              updated_at: '2026-07-10T00:00:00.000Z',
              items: [],
            },
            meta: { request_id: 'req-package-failed', contract_version: '1.0' },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    );
    const entryPolicy = new NoAccountEntryPolicyAdapter();
    const api = new HttpReviewApiAdapter(
      'review',
      new FinalCutReviewClient('https://review.example'),
      entryPolicy,
      'https://review.example',
      {
        entryMode: 'review',
        notify: () => undefined,
        downloadBlob: () => undefined,
        downloadUrl: () => undefined,
      },
    );

    await expect(api.createProjectFinalizedPackage('prj_http', entryPolicy.createContext('review'))).rejects.toThrow(
      '项目包准备失败，请重试。',
    );
    await expect(
      api.downloadProjectFinalizedPackage(
        {
          packageId: 'pkg_failed',
          projectRefId: 'prj_http',
          packageFilename: 'failed.zip',
          fileName: 'failed.zip',
          createdAt: '2026-07-10T00:00:00.000Z',
          entries: [],
        },
        entryPolicy.createContext('review'),
      ),
    ).rejects.toThrow('项目包下载授权已失效，请重新准备。');
  });

  it('polls a preparing HTTP package until it becomes ready', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const ready = url.endsWith('/pkg_poll');
      return new Response(
        JSON.stringify({
          data: {
            id: 'pkg_poll',
            project_ref_id: 'prj_http',
            status: ready ? 'ready' : 'preparing',
            package_filename: 'poll.zip',
            expires_at: '2026-07-10T01:00:00.000Z',
            file_count: ready ? 1 : 0,
            total_bytes: ready ? 12 : 0,
            ...(ready
              ? {
                  sha256: 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad',
                  download_token: 'ready-only-token',
                  download_token_expires_at: '2099-07-10T00:10:00.000Z',
                }
              : {}),
            created_at: '2026-07-10T00:00:00.000Z',
            updated_at: '2026-07-10T00:00:00.000Z',
            items: ready
              ? [{
                  review_item_id: 'item_http',
                  version_id: 'ver_http',
                  finalization_id: 'fin_http',
                  original_file_id: 'file_http',
                  original_filename: 'clip.mp4',
                  sha256: 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad',
                  archive_name: 'clip.mp4',
                }]
              : [],
          },
          meta: { request_id: 'req-package-poll', contract_version: '1.0' },
        }),
        { status: ready ? 200 : 202, headers: { 'Content-Type': 'application/json' } },
      );
    });
    vi.stubGlobal('fetch', fetchMock);
    const entryPolicy = new NoAccountEntryPolicyAdapter();
    const api = new HttpReviewApiAdapter(
      'review',
      new FinalCutReviewClient('https://review.example'),
      entryPolicy,
      'https://review.example',
      { entryMode: 'review', notify: () => undefined, downloadBlob: () => undefined, downloadUrl: () => undefined },
    );

    await expect(api.createProjectFinalizedPackage('prj_http', entryPolicy.createContext('review'))).resolves.toMatchObject({
      packageId: 'pkg_poll',
      fileName: 'poll.zip',
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('aborts a package status poll that never returns', async () => {
    vi.useFakeTimers();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            data: {
              id: 'pkg_hung',
              project_ref_id: 'prj_http',
              status: 'preparing',
              package_filename: 'hung.zip',
              expires_at: '2099-07-10T01:00:00.000Z',
              file_count: 0,
              total_bytes: 0,
              created_at: '2026-07-10T00:00:00.000Z',
              updated_at: '2026-07-10T00:00:00.000Z',
              items: [],
            },
            meta: { request_id: 'req-package-hung', contract_version: '1.0' },
          }),
          { status: 202, headers: { 'Content-Type': 'application/json' } },
        ),
      )
      .mockImplementationOnce(() => new Promise<Response>(() => undefined));
    vi.stubGlobal('fetch', fetchMock);
    const entryPolicy = new NoAccountEntryPolicyAdapter();
    const api = new HttpReviewApiAdapter(
      'review',
      new FinalCutReviewClient('https://review.example'),
      entryPolicy,
      'https://review.example',
      { entryMode: 'review', notify: () => undefined, downloadBlob: () => undefined, downloadUrl: () => undefined },
    );

    const preparation = api.createProjectFinalizedPackage('prj_http', entryPolicy.createContext('review'));
    const rejection = expect(preparation).rejects.toThrow('项目包状态查询超时，请重试。');
    await vi.advanceTimersByTimeAsync(10_000);
    await rejection;
  });

  it('removes an unused package authorization when its server expiry is reached', async () => {
    vi.useFakeTimers();
    const expiresAt = new Date(Date.now() + 1_000).toISOString();
    const fetchMock = vi.fn(async () =>
      new Response(
        JSON.stringify({
          data: {
            id: 'pkg_expiring',
            project_ref_id: 'prj_http',
            status: 'ready',
            package_filename: 'expiring.zip',
            expires_at: '2099-07-10T01:00:00.000Z',
            file_count: 0,
            total_bytes: 0,
            sha256: 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad',
            download_token: 'expiring-token',
            download_token_expires_at: expiresAt,
            created_at: '2026-07-10T00:00:00.000Z',
            updated_at: '2026-07-10T00:00:00.000Z',
            items: [],
          },
          meta: { request_id: 'req-package-expiring', contract_version: '1.0' },
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );
    vi.stubGlobal('fetch', fetchMock);
    const entryPolicy = new NoAccountEntryPolicyAdapter();
    const api = new HttpReviewApiAdapter(
      'review',
      new FinalCutReviewClient('https://review.example'),
      entryPolicy,
      'https://review.example',
      { entryMode: 'review', notify: () => undefined, downloadBlob: () => undefined, downloadUrl: () => undefined },
    );
    const result = await api.createProjectFinalizedPackage('prj_http', entryPolicy.createContext('review'));
    await vi.advanceTimersByTimeAsync(1_001);

    await expect(api.downloadProjectFinalizedPackage(result, entryPolicy.createContext('review'))).rejects.toThrow(
      '项目包下载授权已失效，请重新准备。',
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('rejects HTTP write methods that do not belong to the configured entry before fetching', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const runtime = createReviewRuntime({ apiBaseUrl: 'https://review.example/' });
    const editApi = runtime.getApi('edit');
    const reviewApi = runtime.getApi('review');

    await expect(
      reviewApi.deleteReviewItem(
        { projectRefId: 'prj_mode', reviewItemId: 'item_mode', confirmed: true },
        reviewApi.entryPolicy.createContext('review'),
      ),
    ).rejects.toMatchObject({ code: 'EXECUTION_CONTEXT_MISMATCH' });
    await expect(
      editApi.deleteProject({ projectRefId: 'prj_mode', confirmed: true }, editApi.entryPolicy.createContext('edit')),
    ).rejects.toMatchObject({ code: 'EXECUTION_CONTEXT_MISMATCH' });
    await expect(
      editApi.deleteIssue(
        { projectRefId: 'prj_mode', reviewItemId: 'item_mode', versionId: 'ver_mode', issueId: 'iss_mode' },
        editApi.entryPolicy.createContext('edit'),
      ),
    ).rejects.toMatchObject({ code: 'EXECUTION_CONTEXT_MISMATCH' });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('uses generated HTTP queries for project detail when API runtime is configured', async () => {
    const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
      const pathname = new URL(String(url)).pathname;
      if (pathname.endsWith('/api/v1/final-cut-review/projects/prj_runtime_http')) {
        return new Response(
          JSON.stringify({
            data: {
              project_ref_id: 'prj_runtime_http',
              project_code: 'RHTTP',
              project_name: 'Runtime HTTP',
              source: 'local',
              external_project_id: null,
              lifecycle_status: 'active',
              completion_status: 'empty',
              lock_version: 1,
              created_at: '2026-06-20T00:00:00.000Z',
              updated_at: '2026-06-21T00:00:00.000Z',
            },
            meta: { request_id: 'req-project', contract_version: '1.0' },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      }
      if (pathname.endsWith('/api/v1/final-cut-review/projects/prj_runtime_http/items')) {
        return new Response(
          JSON.stringify({
            data: [],
            meta: {
              request_id: 'req-items',
              contract_version: '1.0',
              total_count: 0,
              page: 1,
              page_size: 200,
            },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      }
      throw new Error(`unexpected URL ${String(url)}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    const runtime = createReviewRuntime({ apiBaseUrl: 'https://review.example/' });
    const detail = await runtime.getApi('review').getProjectDetail('prj_runtime_http');

    expect(detail.project.projectRefId).toBe('prj_runtime_http');
    expect(detail.items).toEqual([]);
    expect(fetchMock).toHaveBeenCalledWith(
      'https://review.example/api/v1/final-cut-review/projects/prj_runtime_http/items?page=1&page_size=200',
      expect.objectContaining({ credentials: 'include' }),
    );
  });

  it('sends an Idempotency-Key when completing generated HTTP uploads', async () => {
    const largeFileSize = 32 * 1024 * 1024 + 5;
    const serverComputedSha256 = '0'.repeat(64);
    const uploadedPartSizes: number[] = [];
    const media = {
      original_file_id: 'file_http_upload',
      original_filename: 'clip.mov',
      mime_type: 'video/quicktime',
      file_size: largeFileSize,
      sha256: 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad',
      duration_ms: 1000,
      width: 1920,
      height: 1080,
      fps_num: 25,
      fps_den: 1,
      media_probe_version: 'http-test',
    };
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/api/v1/final-cut-review/projects/prj_http')) {
        return new Response(
          JSON.stringify({
            data: {
              project_ref_id: 'prj_http',
              project_code: 'HTTP',
              project_name: 'HTTP Project',
              source: 'local',
              external_project_id: null,
              lifecycle_status: 'active',
              completion_status: 'empty',
              lock_version: 1,
              created_at: '2026-06-21T00:00:00.000Z',
              updated_at: '2026-06-21T00:00:00.000Z',
            },
            meta: { request_id: 'req-project', contract_version: '1.0' },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      }
      if (url.endsWith('/api/v1/files/uploads/init')) {
        const body = JSON.parse(String(init?.body));
        expect(init?.headers).toEqual(expect.objectContaining({ 'Idempotency-Key': expect.stringContaining('InitUpload_') }));
        expect(body).toMatchObject({
          original_filename: 'clip.mov',
          mime_type: 'video/quicktime',
          file_size: largeFileSize,
          sha256: serverComputedSha256,
        });
        return new Response(
          JSON.stringify({
            data: {
              upload_id: 'upl_http',
              status: 'initiated',
              original_filename: 'clip.mov',
              mime_type: 'video/quicktime',
              declared_size: largeFileSize,
              received_size: 0,
              file_id: null,
            },
            meta: { request_id: 'req-upload-init', contract_version: '1.0' },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      }
      const partMatch = url.match(/\/api\/v1\/files\/uploads\/upl_http\/parts\/(\d+)$/);
      if (partMatch) {
        expect(init?.method).toBe('PUT');
        expect(Number(partMatch[1])).toBe(uploadedPartSizes.length + 1);
        uploadedPartSizes.push((init?.body as Blob).size);
        return new Response(
          JSON.stringify({
            data: {
              upload_id: 'upl_http',
              status: 'receiving',
              original_filename: 'clip.mov',
              mime_type: 'video/quicktime',
              declared_size: largeFileSize,
              received_size: uploadedPartSizes.reduce((total, size) => total + size, 0),
              file_id: null,
            },
            meta: { request_id: 'req-upload-part', contract_version: '1.0' },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      }
      if (url.endsWith('/api/v1/files/uploads/upl_http/complete')) {
        expect(init?.headers).toEqual(expect.objectContaining({ 'Idempotency-Key': expect.stringContaining('CompleteUpload_') }));
        expect(init?.body).toBeUndefined();
        return new Response(
          JSON.stringify({
            data: {
              upload_id: 'upl_http',
              status: 'completed',
              original_filename: 'clip.mov',
              mime_type: 'video/quicktime',
              declared_size: largeFileSize,
              received_size: largeFileSize,
              file_id: 'file_http_upload',
            },
            meta: { request_id: 'req-upload-complete', contract_version: '1.0' },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      }
      if (url.endsWith('/api/v1/final-cut-review/edit/projects/prj_http/items')) {
        return new Response(
          JSON.stringify({
            data: {
              id: 'item_http',
              project_ref_id: 'prj_http',
              item_code: '1',
              episode_no: 1,
              title: 'HTTP clip',
              workflow_status: 'pending_review',
              current_version_id: 'ver_http',
              current_version_no: 1,
              ui_status: 'pending_review',
              active_finalization_id: null,
              unresolved_current_version_count: 0,
              resolved_current_version_count: 0,
              historical_version_count: 0,
              is_finalized: false,
              lock_version: 1,
              created_at: '2026-06-21T00:00:00Z',
              updated_at: '2026-06-21T00:00:00Z',
            },
            meta: { request_id: 'req-create-item', contract_version: '1.0' },
          }),
          { status: 201, headers: { 'Content-Type': 'application/json' } },
        );
      }
      if (url.endsWith('/api/v1/final-cut-review/projects/prj_http/items/item_http/versions/ver_http')) {
        return new Response(
          JSON.stringify({
            data: {
              id: 'ver_http',
              project_ref_id: 'prj_http',
              review_item_id: 'item_http',
              previous_version_id: null,
              version_no: 1,
              version_label: 'V1',
              is_current: true,
              original_media: media,
              playback_status: 'ready',
              playback_asset_id: 'file_http_upload',
              thumbnail_asset_id: null,
              version_note: null,
              change_summary: null,
              lock_version: 1,
              created_at: '2026-06-21T00:00:00Z',
            },
            meta: { request_id: 'req-version', contract_version: '1.0' },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      }
      throw new Error(`unexpected URL ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    stubBinaryUploadXhr(fetchMock);

    const runtime = createReviewRuntime({ apiBaseUrl: 'https://review.example' });
    const context = runtime.getApi('edit').entryPolicy.createContext('edit');
    const arrayBuffer = vi.fn(async () => {
      throw new Error('upload must not read the whole file into memory');
    });
    const file = {
      name: 'clip.mov',
      type: 'video/quicktime',
      size: largeFileSize,
      lastModified: 0,
      arrayBuffer,
      slice: (start?: number, end?: number) =>
        new Blob([new Uint8Array(Math.max(0, (end ?? largeFileSize) - (start ?? 0)))], { type: 'video/quicktime' }),
    } as unknown as File;
    const result = await runtime.getApi('edit').createReviewItemWithVersion(
      { projectRefId: 'prj_http', title: 'HTTP clip', episode: '1', file },
      context,
    );

    expect(result.version.originalFileId).toBe('file_http_upload');
    expect(uploadedPartSizes).toEqual([
      8 * 1024 * 1024,
      8 * 1024 * 1024,
      8 * 1024 * 1024,
      8 * 1024 * 1024,
      5,
    ]);
    expect(arrayBuffer).not.toHaveBeenCalled();
    for (const [, init] of fetchMock.mock.calls) {
      expect(new Headers(init?.headers).has('X-Write-Guard-Verified')).toBe(false);
    }
  });

  it('sends append-version metadata in the HTTP UploadReviewVersion command envelope', async () => {
    let uploadCommandPayload: unknown;
    const media = {
      original_file_id: 'file_http_append',
      original_filename: 'metadata-v2.mp4',
      mime_type: 'video/mp4',
      file_size: demoFile('metadata-v2.mp4').size,
      sha256: 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad',
      duration_ms: 1000,
      width: 1920,
      height: 1080,
      fps_num: 25,
      fps_den: 1,
      media_probe_version: 'http-test',
    };
    const itemDto = {
      id: 'item_http_append',
      project_ref_id: 'prj_http_append',
      item_code: '01',
      episode_no: 1,
      title: 'HTTP Append Item',
      workflow_status: 'pending_review',
      current_version_id: 'ver_http_append_v1',
      current_version_no: 1,
      ui_status: 'pending_review',
      active_finalization_id: null,
      unresolved_current_version_count: 1,
      resolved_current_version_count: 0,
      historical_version_count: 0,
      is_finalized: false,
      lock_version: 7,
      created_at: '2026-07-04T00:00:00.000Z',
      updated_at: '2026-07-04T00:00:00.000Z',
    };
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/api/v1/final-cut-review/projects/prj_http_append')) {
        return new Response(
          JSON.stringify({
            data: {
              project_ref_id: 'prj_http_append',
              project_code: 'APPEND',
              project_name: 'HTTP Append',
              source: 'local',
              external_project_id: null,
              lifecycle_status: 'active',
              completion_status: 'empty',
              lock_version: 2,
              created_at: '2026-07-04T00:00:00.000Z',
              updated_at: '2026-07-04T00:00:00.000Z',
            },
            meta: { request_id: 'req-project', contract_version: '1.0' },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      }
      if (url.endsWith('/api/v1/final-cut-review/projects/prj_http_append/items/item_http_append')) {
        return new Response(JSON.stringify({ data: itemDto, meta: { request_id: 'req-item', contract_version: '1.0' } }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.endsWith('/api/v1/files/uploads/init')) {
        return new Response(
          JSON.stringify({
            data: {
              upload_id: 'upl_append',
              status: 'initiated',
              original_filename: 'metadata-v2.mp4',
              mime_type: 'video/mp4',
              declared_size: media.file_size,
              received_size: 0,
              file_id: null,
            },
            meta: { request_id: 'req-upload-init', contract_version: '1.0' },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      }
      if (/\/api\/v1\/files\/uploads\/upl_append\/parts\/1$/.test(url)) {
        expect(init?.method).toBe('PUT');
        return new Response(
          JSON.stringify({
            data: {
              upload_id: 'upl_append',
              status: 'receiving',
              original_filename: 'metadata-v2.mp4',
              mime_type: 'video/mp4',
              declared_size: media.file_size,
              received_size: media.file_size,
              file_id: null,
            },
            meta: { request_id: 'req-upload-part', contract_version: '1.0' },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      }
      if (url.endsWith('/api/v1/files/uploads/upl_append/complete')) {
        return new Response(
          JSON.stringify({
            data: {
              upload_id: 'upl_append',
              status: 'completed',
              original_filename: 'metadata-v2.mp4',
              mime_type: 'video/mp4',
              declared_size: media.file_size,
              received_size: media.file_size,
              file_id: 'file_http_append',
            },
            meta: { request_id: 'req-upload-complete', contract_version: '1.0' },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      }
      if (url.endsWith('/api/v1/final-cut-review/edit/projects/prj_http_append/items/item_http_append/versions')) {
        expect(init?.method).toBe('POST');
        expect(init?.headers).toEqual(expect.objectContaining({ 'If-Match': '7' }));
        const body = JSON.parse(String(init?.body));
        uploadCommandPayload = body.payload;
        expect(body).toMatchObject({
          command_type: 'UploadReviewVersion',
          expected_aggregate_version: 7,
          payload: {
            project_ref_id: 'prj_http_append',
            review_item_id: 'item_http_append',
            original_file_id: 'file_http_append',
            version_note: 'HTTP 版本说明',
            change_summary: 'HTTP 本次修改说明',
            supersede_reason: 'HTTP 主动补版原因',
          },
        });
        return new Response(
          JSON.stringify({
            data: {
              id: 'ver_http_append_v2',
              project_ref_id: 'prj_http_append',
              review_item_id: 'item_http_append',
              previous_version_id: 'ver_http_append_v1',
              version_no: 2,
              version_label: 'V2',
              is_current: true,
              original_media: media,
              playback_status: 'ready',
              playback_asset_id: 'file_http_append',
              thumbnail_asset_id: null,
              version_note: 'HTTP 版本说明',
              change_summary: 'HTTP 本次修改说明',
              lock_version: 1,
              created_at: '2026-07-04T00:00:00.000Z',
            },
            meta: { request_id: 'req-append-version', contract_version: '1.0' },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      }
      throw new Error(`unexpected URL ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    stubBinaryUploadXhr(fetchMock);

    const runtime = createReviewRuntime({ apiBaseUrl: 'https://review.example' });
    const edit = runtime.getApi('edit');
    const context = edit.entryPolicy.createContext('edit');
    const version = await edit.appendVersion(
      {
        projectRefId: 'prj_http_append',
        reviewItemId: 'item_http_append',
        file: demoFile('metadata-v2.mp4'),
        versionNote: 'HTTP 版本说明',
        changeSummary: 'HTTP 本次修改说明',
        supersedeReason: 'HTTP 主动补版原因',
      },
      context,
    );

    expect(uploadCommandPayload).toBeTruthy();
    expect(version.versionNote).toBe('HTTP 版本说明');
    expect(version.changeSummary).toBe('HTTP 本次修改说明');
  });

  it('sends ArchiveProject and RestoreProject command envelopes with optimistic locks', async () => {
    let status: 'active' | 'archived' = 'active';
    let lockVersion = 7;
    const projectDto = () => ({
      project_ref_id: 'prj_http_archive',
      project_code: 'ARCH',
      project_name: 'HTTP Archive',
      source: 'local',
      external_project_id: null,
      lifecycle_status: status,
      completion_status: 'empty',
      deleted_at: null,
      lock_version: lockVersion,
      created_at: '2026-07-05T00:00:00.000Z',
      updated_at: `2026-07-05T00:00:0${lockVersion - 7}.000Z`,
    });
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/api/v1/final-cut-review/projects/prj_http_archive')) {
        return new Response(JSON.stringify({ data: projectDto(), meta: { request_id: 'req-project', contract_version: '1.0' } }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.endsWith('/api/v1/final-cut-review/review/projects/prj_http_archive/archive')) {
        expect(init?.method).toBe('POST');
        expect(init?.headers).toEqual(expect.objectContaining({ 'If-Match': '7' }));
        const body = JSON.parse(String(init?.body));
        expect(body).toMatchObject({
          command_type: 'ArchiveProject',
          expected_aggregate_version: 7,
          payload: { project_ref_id: 'prj_http_archive' },
        });
        status = 'archived';
        lockVersion = 8;
        return new Response(JSON.stringify({ data: projectDto(), meta: { request_id: 'req-archive', contract_version: '1.0' } }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.endsWith('/api/v1/final-cut-review/review/projects/prj_http_archive/restore')) {
        expect(init?.method).toBe('POST');
        expect(init?.headers).toEqual(expect.objectContaining({ 'If-Match': '8' }));
        const body = JSON.parse(String(init?.body));
        expect(body).toMatchObject({
          command_type: 'RestoreProject',
          expected_aggregate_version: 8,
          payload: { project_ref_id: 'prj_http_archive' },
        });
        status = 'active';
        lockVersion = 9;
        return new Response(JSON.stringify({ data: projectDto(), meta: { request_id: 'req-restore', contract_version: '1.0' } }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      throw new Error(`unexpected URL ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    const runtime = createReviewRuntime({ apiBaseUrl: 'https://review.example' });
    const review = runtime.getApi('review');
    const context = review.entryPolicy.createContext('review');

    await expect(review.archiveProject({ projectRefId: 'prj_http_archive' }, context)).resolves.toMatchObject({
      projectRefId: 'prj_http_archive',
      status: 'archived',
    });
    await expect(review.restoreProject({ projectRefId: 'prj_http_archive' }, context)).resolves.toMatchObject({
      projectRefId: 'prj_http_archive',
      status: 'active',
    });
  });

  it('blocks HTTP upload side effects before creating or appending versions for archived projects', async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url.endsWith('/api/v1/final-cut-review/projects/prj_http_archived')) {
        return new Response(
          JSON.stringify({
            data: {
              project_ref_id: 'prj_http_archived',
              project_code: 'ARCH',
              project_name: 'HTTP Archived',
              source: 'local',
              external_project_id: null,
              lifecycle_status: 'archived',
              completion_status: 'empty',
              lock_version: 3,
              created_at: '2026-07-05T00:00:00.000Z',
              updated_at: '2026-07-05T00:00:00.000Z',
            },
            meta: { request_id: 'req-archived', contract_version: '1.0' },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      }
      throw new Error(`unexpected URL ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    const runtime = createReviewRuntime({ apiBaseUrl: 'https://review.example' });
    const edit = runtime.getApi('edit');
    const context = edit.entryPolicy.createContext('edit');

    await expect(
      edit.createReviewItemWithVersion(
        { projectRefId: 'prj_http_archived', title: 'archived', episode: '01', file: demoFile('archived-v1.mp4') },
        context,
      ),
    ).rejects.toMatchObject({ code: 'PROJECT_ARCHIVED_READONLY' });
    await expect(
      edit.appendVersion(
        { projectRefId: 'prj_http_archived', reviewItemId: 'item_archived', file: demoFile('archived-v2.mp4') },
        context,
      ),
    ).rejects.toMatchObject({ code: 'PROJECT_ARCHIVED_READONLY' });

    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/api/v1/files/uploads'))).toBe(false);
  });

  it('blocks HTTP upload side effects before appending versions for finalized items', async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url.endsWith('/api/v1/final-cut-review/projects/prj_http_finalized')) {
        return new Response(
          JSON.stringify({
            data: {
              project_ref_id: 'prj_http_finalized',
              project_code: 'FIN',
              project_name: 'HTTP Finalized',
              source: 'local',
              external_project_id: null,
              lifecycle_status: 'active',
              completion_status: 'completed',
              lock_version: 5,
              created_at: '2026-07-03T00:00:00.000Z',
              updated_at: '2026-07-03T00:00:00.000Z',
            },
            meta: { request_id: 'req-finalized-project', contract_version: '1.0' },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      }
      if (url.endsWith('/api/v1/final-cut-review/projects/prj_http_finalized/items/item_http_finalized')) {
        return new Response(
          JSON.stringify({
            data: {
              id: 'item_http_finalized',
              project_ref_id: 'prj_http_finalized',
              item_code: '01',
              episode_no: 1,
              title: 'HTTP Finalized Item',
              workflow_status: 'finalized',
              current_version_id: 'ver_http_finalized_v2',
              current_version_no: 2,
              ui_status: 'finalized',
              active_finalization_id: 'fin_http_finalized',
              unresolved_current_version_count: 0,
              resolved_current_version_count: 2,
              historical_version_count: 1,
              is_finalized: true,
              lock_version: 9,
              created_at: '2026-07-03T00:00:00.000Z',
              updated_at: '2026-07-03T00:00:00.000Z',
            },
            meta: { request_id: 'req-finalized-item', contract_version: '1.0' },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      }
      throw new Error(`unexpected URL ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    const runtime = createReviewRuntime({ apiBaseUrl: 'https://review.example' });
    const edit = runtime.getApi('edit');
    const context = edit.entryPolicy.createContext('edit');

    await expect(
      edit.appendVersion(
        {
          projectRefId: 'prj_http_finalized',
          reviewItemId: 'item_http_finalized',
          file: demoFile('finalized-v3.mp4'),
          supersedeReason: '定稿后不得补版',
        },
        context,
      ),
    ).rejects.toMatchObject({ code: 'FINALIZED_READONLY' });

    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/api/v1/files/uploads'))).toBe(false);
  });
});

describe('browser host bridge', () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    document.body.innerHTML = '';
  });

  it('attaches a sanitized temporary download anchor and cleans it up after click', () => {
    vi.useFakeTimers();
    const createObjectURL = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:download-test');
    const revokeObjectURL = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);

    new BrowserReviewHostBridge('review').downloadBlob(new Blob(['payload']), '../Unsafe Final.mp4');

    const anchor = document.body.querySelector('a');
    expect(anchor).not.toBeNull();
    expect(anchor?.href).toBe('blob:download-test');
    expect(anchor?.download).toBe('Unsafe Final.mp4');
    expect(click).toHaveBeenCalledTimes(1);
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).not.toHaveBeenCalled();

    vi.advanceTimersByTime(60_000);

    expect(document.body.querySelector('a')).toBeNull();
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:download-test');
  });
});

describe('final cut review core invariants', () => {
  it('keeps issue numbers monotonically increasing across all versions of one review item', async () => {
    const { review } = makeHarness();
    const workspace = await review.getWorkspace({ projectRefId: 'prj_seed_final_cut', reviewItemId: 'item_ep28' });
    expect([...workspace.currentIssues, ...workspace.historicalIssues].map((issue) => issue.issueNo).sort((left, right) => left - right)).toEqual([1, 2, 3, 4]);

    const created = await review.createIssue(
      {
        projectRefId: 'prj_seed_final_cut',
        reviewItemId: 'item_ep28',
        versionId: 'ver_ep28_v2',
        timestampMs: 360,
        frameNumber: 9,
        body: '新意见编号应跨版本递增。',
        severity: 'normal',
        shapes: [],
        canvasWidth: 1280,
        canvasHeight: 720,
        videoWidth: 1080,
        videoHeight: 1920,
      },
      ctx(review),
    );

    expect(created.issueNo).toBe(5);
  });

  it('soft-deletes only current-version issues while keeping revisions persisted', async () => {
    const { review, repository } = makeHarness();
    const created = await review.createIssue(
      {
        projectRefId: 'prj_seed_final_cut',
        reviewItemId: 'item_ep28',
        versionId: 'ver_ep28_v2',
        timestampMs: 520,
        frameNumber: 13,
        body: '删除后不应出现在当前意见列表。',
        severity: 'normal',
        shapes: [],
        canvasWidth: 1280,
        canvasHeight: 720,
        videoWidth: 1080,
        videoHeight: 1920,
      },
      ctx(review),
    );

    const deleted = await review.deleteIssue(
      {
        projectRefId: 'prj_seed_final_cut',
        reviewItemId: 'item_ep28',
        versionId: 'ver_ep28_v2',
        issueId: created.issueId,
      },
      ctx(review),
    );
    expect(deleted.deletedAt).toEqual(expect.any(String));

    const workspace = await review.getWorkspace({ projectRefId: 'prj_seed_final_cut', reviewItemId: 'item_ep28' });
    expect(workspace.currentIssues.some((issue) => issue.issueId === created.issueId)).toBe(false);
    const stored = repository.snapshot().issues.find((issue) => issue.issueId === created.issueId);
    expect(stored?.deletedAt).toEqual(expect.any(String));
    expect(stored?.revisions).toHaveLength(1);
  });

  it('enforces edit/review capability boundaries', async () => {
    const { edit, review } = makeHarness();
    await expect(
      edit.createIssue(
        {
          projectRefId: 'prj_seed_final_cut',
          reviewItemId: 'item_ep28',
          versionId: 'ver_ep28_v2',
          timestampMs: 0,
          frameNumber: 0,
          body: 'edit cannot create issue',
          severity: 'normal',
          shapes: [],
          canvasWidth: 1280,
          canvasHeight: 720,
          videoWidth: 1280,
          videoHeight: 720,
        },
        ctx(edit),
      ),
    ).rejects.toMatchObject({ code: 'CAPABILITY_DENIED' });

    await expect(
      review.createProject({ name: 'Review created', code: 'BAD', description: 'review cannot create project' }, ctx(review)),
    ).rejects.toMatchObject({ code: 'CAPABILITY_DENIED' });
    await expect(edit.archiveProject({ projectRefId: 'prj_seed_final_cut' }, ctx(edit))).rejects.toMatchObject({
      code: 'CAPABILITY_DENIED',
    });
    await expect(edit.deleteProject({ projectRefId: 'prj_seed_final_cut', confirmed: true }, ctx(edit))).rejects.toMatchObject({
      code: 'CAPABILITY_DENIED',
    });
  });

  it('archives projects as read-only and restores them without deleting descendants', async () => {
    const { edit, review } = makeHarness();
    const before = await edit.getProjectDetail('prj_seed_final_cut');
    const beforeVersionIds = Object.values(before.versionsByItem)
      .flat()
      .map((version) => version.versionId)
      .sort();
    const beforeIssueIds = Object.values(before.issuesByVersion)
      .flat()
      .map((issue) => issue.issueId)
      .sort();
    await review.finalizeCurrentVersion(
      {
        projectRefId: 'prj_seed_final_cut',
        reviewItemId: 'item_ep28',
        versionId: 'ver_ep28_v2',
        confirmed: true,
      },
      ctx(review),
    );

    const archived = await review.archiveProject({ projectRefId: 'prj_seed_final_cut' }, ctx(review));
    expect(archived.status).toBe('archived');
    await expect(review.deleteProject({ projectRefId: 'prj_seed_final_cut', confirmed: true }, ctx(review))).rejects.toMatchObject({
      code: 'RESOURCE_STATE_CONFLICT',
    });
    await expect(review.archiveProject({ projectRefId: 'prj_seed_final_cut' }, ctx(review))).rejects.toMatchObject({
      code: 'RESOURCE_STATE_CONFLICT',
    });
    const archivedDetail = await edit.getProjectDetail('prj_seed_final_cut');
    expect(archivedDetail.items.map((item) => item.reviewItemId)).toEqual(before.items.map((item) => item.reviewItemId));
    expect(Object.values(archivedDetail.versionsByItem).flat().map((version) => version.versionId).sort()).toEqual(beforeVersionIds);
    expect(Object.values(archivedDetail.issuesByVersion).flat().map((issue) => issue.issueId).sort()).toEqual(beforeIssueIds);

    await expect(
      edit.updateProject(
        {
          projectRefId: 'prj_seed_final_cut',
          name: 'Archived edit',
          code: 'FJ-SEED',
          description: 'archived metadata edit must fail',
        },
        ctx(edit),
      ),
    ).rejects.toMatchObject({ code: 'PROJECT_ARCHIVED_READONLY' });
    await expect(
      edit.createReviewItemWithVersion(
        {
          projectRefId: 'prj_seed_final_cut',
          title: '归档后新成片',
          episode: '99',
          file: demoFile('archived-v1.mp4'),
        },
        ctx(edit),
      ),
    ).rejects.toMatchObject({ code: 'PROJECT_ARCHIVED_READONLY' });
    await expect(
      review.createIssue(
        {
          projectRefId: 'prj_seed_final_cut',
          reviewItemId: 'item_ep28',
          versionId: 'ver_ep28_v2',
          timestampMs: 0,
          frameNumber: 0,
          body: 'archived project cannot create issue',
          severity: 'normal',
          shapes: [],
          canvasWidth: 1280,
          canvasHeight: 720,
          videoWidth: 1280,
          videoHeight: 720,
        },
        ctx(review),
      ),
    ).rejects.toMatchObject({ code: 'PROJECT_ARCHIVED_READONLY' });
    await expect(review.createProjectFinalizedPackage('prj_seed_final_cut', ctx(review))).resolves.toMatchObject({
      entries: [expect.objectContaining({ projectRefId: 'prj_seed_final_cut', versionId: 'ver_ep28_v2' })],
    });

    const restored = await review.restoreProject({ projectRefId: 'prj_seed_final_cut' }, ctx(review));
    expect(restored.status).toBe('active');
    await expect(review.restoreProject({ projectRefId: 'prj_seed_final_cut' }, ctx(review))).rejects.toMatchObject({
      code: 'RESOURCE_STATE_CONFLICT',
    });
    const restoredDetail = await edit.getProjectDetail('prj_seed_final_cut');
    expect(Object.values(restoredDetail.versionsByItem).flat().map((version) => version.versionId).sort()).toEqual(beforeVersionIds);
    expect(Object.values(restoredDetail.issuesByVersion).flat().map((issue) => issue.issueId).sort()).toEqual(beforeIssueIds);
    await expect(
      edit.createReviewItemWithVersion(
        {
          projectRefId: 'prj_seed_final_cut',
          title: '恢复后新成片',
          episode: '30',
          file: demoFile('restored-v1.mp4'),
        },
        ctx(edit),
      ),
    ).resolves.toMatchObject({ item: expect.objectContaining({ projectRefId: 'prj_seed_final_cut' }) });
  });

  it('soft-deletes projects from lists without physically deleting descendants', async () => {
    const { edit, review, repository } = makeHarness();
    const before = await edit.getProjectDetail('prj_seed_final_cut');

    const deleted = await review.deleteProject({ projectRefId: 'prj_seed_final_cut', confirmed: true }, ctx(review));
    expect(deleted.deletedAt).toEqual(expect.any(String));
    await expect(review.deleteProject({ projectRefId: 'prj_seed_final_cut', confirmed: true }, ctx(review))).rejects.toMatchObject({
      code: 'RESOURCE_STATE_CONFLICT',
    });

    await expect(edit.listProjects()).resolves.not.toEqual(
      expect.arrayContaining([expect.objectContaining({ projectRefId: 'prj_seed_final_cut' })]),
    );

    await expect(edit.getProjectDetail('prj_seed_final_cut')).rejects.toMatchObject({ code: 'PROJECT_NOT_FOUND' });
    const snapshot = repository.snapshot();
    expect(snapshot.items.filter((item) => item.projectRefId === 'prj_seed_final_cut').map((item) => item.reviewItemId)).toEqual(
      before.items.map((item) => item.reviewItemId),
    );
    expect(snapshot.versions.filter((version) => version.projectRefId === 'prj_seed_final_cut')).toHaveLength(
      Object.values(before.versionsByItem).flat().length,
    );
    expect(snapshot.issues.filter((issue) => issue.projectRefId === 'prj_seed_final_cut')).toHaveLength(
      Object.values(before.issuesByVersion).flat().length,
    );
    await expect(review.createProjectFinalizedPackage('prj_seed_final_cut', ctx(review))).rejects.toMatchObject({
      code: 'RESOURCE_STATE_CONFLICT',
    });
    await expect(
      edit.createReviewItemWithVersion(
        {
          projectRefId: 'prj_seed_final_cut',
          title: '删除后不得上传',
          episode: '99',
          file: demoFile('deleted-v1.mp4'),
        },
        ctx(edit),
      ),
    ).rejects.toMatchObject({ code: 'PROJECT_DELETED_READONLY' });
    await expect(review.archiveProject({ projectRefId: 'prj_seed_final_cut' }, ctx(review))).rejects.toMatchObject({
      code: 'PROJECT_DELETED_READONLY',
    });
  });

  it('blocks finalized item write paths before storing appended upload files', async () => {
    const { edit, review, storage } = makeHarness();
    await review.finalizeCurrentVersion(
      {
        projectRefId: 'prj_seed_final_cut',
        reviewItemId: 'item_ep28',
        versionId: 'ver_ep28_v2',
        confirmed: true,
      },
      ctx(review),
    );
    const storeOriginal = vi.spyOn(storage, 'storeOriginal');

    await expect(
      edit.appendVersion(
        {
          projectRefId: 'prj_seed_final_cut',
          reviewItemId: 'item_ep28',
          file: demoFile('finalized-v3.mp4'),
          supersedeReason: '定稿后不得补版',
        },
        ctx(edit),
      ),
    ).rejects.toMatchObject({ code: 'FINALIZED_READONLY' });
    expect(storeOriginal).not.toHaveBeenCalled();

    const writeInput = {
      projectRefId: 'prj_seed_final_cut',
      reviewItemId: 'item_ep28',
      versionId: 'ver_ep28_v2',
    };
    await expect(
      review.createIssue(
        {
          ...writeInput,
          timestampMs: 0,
          frameNumber: 0,
          body: '定稿后不能新增意见',
          severity: 'normal',
          shapes: [],
          canvasWidth: 1280,
          canvasHeight: 720,
          videoWidth: 1280,
          videoHeight: 720,
        },
        ctx(review),
      ),
    ).rejects.toMatchObject({ code: 'FINALIZED_READONLY' });
    await expect(
      review.editIssue(
        {
          ...writeInput,
          issueId: 'issue_v2_001',
          body: '定稿后不能编辑意见',
          timestampMs: 0,
          frameNumber: 0,
          shapes: [],
          canvasWidth: 1280,
          canvasHeight: 720,
          videoWidth: 1280,
          videoHeight: 720,
        },
        ctx(review),
      ),
    ).rejects.toMatchObject({ code: 'FINALIZED_READONLY' });
    await expect(
      review.replyToIssue({ ...writeInput, issueId: 'issue_v2_001', body: '定稿后不能回复' }, ctx(review)),
    ).rejects.toMatchObject({ code: 'FINALIZED_READONLY' });
    await expect(review.reopenIssue({ ...writeInput, issueId: 'issue_v2_001' }, ctx(review))).rejects.toMatchObject({
      code: 'FINALIZED_READONLY',
    });
    await expect(edit.resolveIssue({ ...writeInput, issueId: 'issue_v2_001' }, ctx(edit))).rejects.toMatchObject({
      code: 'FINALIZED_READONLY',
    });
    await expect(review.requestChanges(writeInput, ctx(review))).rejects.toMatchObject({ code: 'CAPABILITY_DENIED' });
  });

  it('requires the item-delete confirmation at the port boundary', async () => {
    const { edit } = makeHarness();
    const project = await edit.createProject(
      { code: 'CONFIRM-DELETE', name: '删除确认边界', description: '' },
      ctx(edit),
    );
    const { item } = await edit.createReviewItemWithVersion(
      {
        projectRefId: project.projectRefId,
        title: '待确认删除分集',
        episode: '1',
        file: demoFile('confirm-delete.mp4'),
      },
      ctx(edit),
    );
    const missingConfirmation = {
      projectRefId: project.projectRefId,
      reviewItemId: item.reviewItemId,
    } as unknown as Parameters<typeof edit.deleteReviewItem>[0];
    const falseConfirmation = {
      ...missingConfirmation,
      confirmed: false,
    } as unknown as Parameters<typeof edit.deleteReviewItem>[0];

    await expect(edit.deleteReviewItem(missingConfirmation, ctx(edit))).rejects.toMatchObject({
      code: 'RESOURCE_STATE_CONFLICT',
    });
    await expect(edit.deleteReviewItem(falseConfirmation, ctx(edit))).rejects.toMatchObject({
      code: 'RESOURCE_STATE_CONFLICT',
    });
    await expect(
      edit.deleteReviewItem(
        { ...missingConfirmation, confirmed: true },
        ctx(edit),
      ),
    ).resolves.toMatchObject({ reviewItemId: item.reviewItemId });
  });

  it('does not inherit or copy issues when a new version is appended', async () => {
    const { edit, review } = makeHarness();
    const v3 = await edit.appendVersion(
      {
        projectRefId: 'prj_seed_final_cut',
        reviewItemId: 'item_ep28',
        file: demoFile('EP28-V3.mp4'),
        versionNote: 'V3 版本说明',
        changeSummary: '完成 V2 审阅意见修改。',
      },
      ctx(edit),
    );
    expect(v3.versionNote).toBe('V3 版本说明');
    expect(v3.changeSummary).toBe('完成 V2 审阅意见修改。');

    const workspace = await review.getWorkspace({
      projectRefId: 'prj_seed_final_cut',
      reviewItemId: 'item_ep28',
      versionId: v3.versionId,
    });

    expect(workspace.currentVersion.label).toBe('V3');
    expect(workspace.currentVersion.status).toBe('pending_review');
    expect(workspace.currentIssues).toHaveLength(0);
    expect(workspace.historicalIssues.map((issue) => issue.versionId).sort()).toEqual([
      'ver_ep28_v1',
      'ver_ep28_v2',
      'ver_ep28_v2',
      'ver_ep28_v2',
    ]);
  });

  it('starts review implicitly on first issue and then allows the next version upload', async () => {
    const { edit, review } = makeHarness();
    const project = await edit.createProject({ name: '新项目', code: 'NEW', description: '状态机测试项目。' }, ctx(edit));
    const created = await edit.createReviewItemWithVersion(
      {
        projectRefId: project.projectRefId,
        title: '新成片',
        episode: '02',
        file: demoFile('new-v1.mp4'),
      },
      ctx(edit),
    );
    expect(created.item.status).toBe('pending_review');

    await expect(
      edit.appendVersion(
        {
          projectRefId: project.projectRefId,
          reviewItemId: created.item.reviewItemId,
          file: demoFile('new-v2.mp4'),
        },
        ctx(edit),
      ),
    ).rejects.toMatchObject({ code: 'NEXT_VERSION_REQUIRES_ISSUE' });

    await expect(
      review.startReview(
        {
          projectRefId: project.projectRefId,
          reviewItemId: created.item.reviewItemId,
          versionId: created.version.versionId,
        },
        ctx(review),
      ),
    ).rejects.toMatchObject({ code: 'CAPABILITY_DENIED' });

    const issue = await review.createIssue(
      {
        projectRefId: project.projectRefId,
        reviewItemId: created.item.reviewItemId,
        versionId: created.version.versionId,
        timestampMs: 1000,
        frameNumber: 25,
        body: '首条意见隐式开始审阅。',
        severity: 'normal',
        shapes: [],
        canvasWidth: 1280,
        canvasHeight: 720,
        videoWidth: 1280,
        videoHeight: 720,
      },
      ctx(review),
    );
    expect(issue.issueNo).toBe(1);
    const workspace = await review.getWorkspace({ projectRefId: project.projectRefId, reviewItemId: created.item.reviewItemId });
    expect(workspace.item.status).toBe('in_review');

    const nextVersion = await edit.appendVersion(
      {
        projectRefId: project.projectRefId,
        reviewItemId: created.item.reviewItemId,
        file: demoFile('new-v2.mp4'),
      },
      ctx(edit),
    );
    expect(nextVersion.label).toBe('V2');
    const nextWorkspace = await review.getWorkspace({
      projectRefId: project.projectRefId,
      reviewItemId: created.item.reviewItemId,
    });
    expect(nextWorkspace.currentIssues).toHaveLength(0);
    expect(nextWorkspace.historicalIssues.map((candidate) => candidate.issueId)).toEqual([issue.issueId]);
  });

  it('edits issue by creating a current revision and keeps replies separate from playback', async () => {
    const { review } = makeHarness();
    const before = await review.getWorkspace({ projectRefId: 'prj_seed_final_cut', reviewItemId: 'item_ep28' });
    const issue = before.currentIssues[0];
    const previousRevisionId = issue.currentRevisionId;

    const edited = await review.editIssue(
      {
        projectRefId: issue.projectRefId,
        reviewItemId: issue.reviewItemId,
        versionId: issue.versionId,
        issueId: issue.issueId,
        body: '更新为当前修订的精确批注。',
        timestampMs: 200,
        frameNumber: 5,
        shapes: [
          {
            shapeId: '',
            tool: 'rect',
            color: '#58e1d4',
            lineWidth: 3,
            bounds: { x: 0.2, y: 0.2, width: 0.3, height: 0.2 },
          },
        ],
        canvasWidth: 1366,
        canvasHeight: 768,
        videoWidth: 1920,
        videoHeight: 1080,
      },
      ctx(review),
    );

    expect(edited.currentRevisionId).not.toBe(previousRevisionId);
    expect(edited.revisions).toHaveLength(issue.revisions.length + 1);
    expect(edited.currentRevision.revisionNo).toBe(2);
    expect(edited.currentAnnotationSet?.revisionId).toBe(edited.currentRevisionId);
    expect(playbackTargetFromIssue(edited)).toMatchObject({
      revisionId: edited.currentRevisionId,
      annotationSetId: edited.currentAnnotationSet?.annotationSetId,
      timestampMs: 200,
      frameNumber: 5,
    });

    const replied = await review.replyToIssue(
      {
        projectRefId: edited.projectRefId,
        reviewItemId: edited.reviewItemId,
        versionId: edited.versionId,
        issueId: edited.issueId,
        body: '确认按新修订处理。',
      },
      ctx(review),
    );
    expect(replied.replies).toHaveLength(1);
    expect(replied.currentRevisionId).toBe(edited.currentRevisionId);
    expect(playbackTargetFromIssue(replied).revisionId).toBe(edited.currentRevisionId);
  });

  it('allows finalization with unresolved current-version issues and freezes exact media fields', async () => {
    const { review } = makeHarness();
    const before = await review.getWorkspace({ projectRefId: 'prj_seed_final_cut', reviewItemId: 'item_ep28' });
    expect(before.currentIssues.some((issue) => issue.status === 'unresolved')).toBe(true);
    const finalization = await review.finalizeCurrentVersion(
      {
        projectRefId: 'prj_seed_final_cut',
        reviewItemId: 'item_ep28',
        versionId: before.currentVersion.versionId,
        confirmed: true,
      },
      ctx(review),
    );

    expect(finalization.versionId).toBe(before.currentVersion.versionId);
    expect(finalization.originalFileId).toBe(before.currentVersion.originalFileId);
    expect(finalization.sha256).toBe(before.currentVersion.sha256);
    expect(finalization.originalMedia).toMatchObject(before.currentVersion.originalMedia);
    expect(before.historicalIssues.some((issue) => issue.versionId === 'ver_ep28_v1' && issue.status === 'unresolved')).toBe(true);
  });

  it('packages only active finalizations in the requested project', async () => {
    const { edit, review } = makeHarness();
    await review.finalizeCurrentVersion(
      {
        projectRefId: 'prj_seed_final_cut',
        reviewItemId: 'item_ep28',
        versionId: 'ver_ep28_v2',
        confirmed: true,
      },
      ctx(review),
    );

    const otherProject = await edit.createProject(
      {
        name: '其他项目',
        code: 'OTHER',
        description: '用于验证打包不能串项目。',
      },
      ctx(edit),
    );
    await edit.createReviewItemWithVersion(
      {
        projectRefId: otherProject.projectRefId,
        title: '其他成片',
        episode: '01',
        file: demoFile('other-v1.mp4'),
      },
      ctx(edit),
    );

    const pkg = await review.createProjectFinalizedPackage('prj_seed_final_cut', ctx(review));
    expect(pkg.entries).toHaveLength(1);
    expect(pkg.entries[0]).toMatchObject({
      projectRefId: 'prj_seed_final_cut',
      reviewItemId: 'item_ep28',
      versionId: 'ver_ep28_v2',
      originalFileId: 'file_seed_v2',
    });
    const { default: JSZip } = await import('jszip');
    const zip = await JSZip.loadAsync(pkg.blob!, { checkCRC32: true });
    expect(zip.file(pkg.entries[0].fileName)).not.toBeNull();
  });

  it('packages large browser file blobs with stream-based CRC and without arrayBuffer memory reads', async () => {
    const { edit, review } = makeHarness();
    const project = await edit.createProject(
      {
        name: '大文件项目',
        code: 'LARGE-PKG',
        description: '验证 mock 打包不会在浏览器端预读大文件。',
      },
      ctx(edit),
    );
    const arrayBuffer = vi.fn(async () => {
      throw new Error('package generation must not read the whole original file');
    });
    const stream = vi.fn(
      () =>
        new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(new Uint8Array([1]));
            controller.close();
          },
        }),
    );
    const file = new File([new Uint8Array([1])], 'large-original.mov', { type: 'video/quicktime', lastModified: 123 });
    Object.defineProperty(file, 'size', { value: 2_400_000_000 });
    Object.defineProperty(file, 'arrayBuffer', { value: arrayBuffer });
    Object.defineProperty(file, 'stream', { value: stream });
    const created = await edit.createReviewItemWithVersion(
      {
        projectRefId: project.projectRefId,
        title: '大文件成片',
        episode: '01',
        file,
      },
      ctx(edit),
    );
    await review.finalizeCurrentVersion(
      {
        projectRefId: project.projectRefId,
        reviewItemId: created.item.reviewItemId,
        versionId: created.version.versionId,
        confirmed: true,
      },
      ctx(review),
    );

    const pkg = await review.createProjectFinalizedPackage(project.projectRefId, ctx(review));
    expect(pkg.entries).toHaveLength(1);
    expect(pkg.blob?.type).toBe('application/zip');
    expect(pkg.entries[0].fileName).toContain('large-original.mov');
    expect(arrayBuffer).not.toHaveBeenCalled();
    expect(stream).toHaveBeenCalled();
  });

  it('fails package generation when finalized media hash no longer matches the stored original', async () => {
    const { edit, review, storage } = makeHarness();
    const project = await edit.createProject(
      {
        name: '哈希校验项目',
        code: 'HASH-PKG',
        description: '验证打包前必须校验定稿快照和原片记录。',
      },
      ctx(edit),
    );
    const created = await edit.createReviewItemWithVersion(
      {
        projectRefId: project.projectRefId,
        title: '哈希成片',
        episode: '01',
        file: demoFile('hash-v1.mp4'),
      },
      ctx(edit),
    );
    const finalization = await review.finalizeCurrentVersion(
      {
        projectRefId: project.projectRefId,
        reviewItemId: created.item.reviewItemId,
        versionId: created.version.versionId,
        confirmed: true,
      },
      ctx(review),
    );
    const stored = await storage.getOriginal(finalization.originalFileId);
    storage.seedOriginal({ ...stored, sha256: 'mismatched-source-hash' });

    await expect(review.createProjectFinalizedPackage(project.projectRefId, ctx(review))).rejects.toMatchObject({
      code: 'PACKAGE_SOURCE_HASH_MISMATCH',
    });
  });

  it('rejects cross-project, cross-item, cross-version, and cross-issue misuse', async () => {
    const { edit, review } = makeHarness();
    const project = await edit.createProject({ name: '新项目', code: 'NEW', description: '边界测试项目。' }, ctx(edit));
    const created = await edit.createReviewItemWithVersion(
      {
        projectRefId: project.projectRefId,
        title: '新成片',
        episode: '02',
        file: demoFile('new-v1.mp4'),
      },
      ctx(edit),
    );

    await expect(
      review.createIssue(
        {
          projectRefId: project.projectRefId,
          reviewItemId: created.item.reviewItemId,
          versionId: 'ver_ep28_v2',
          timestampMs: 0,
          frameNumber: 0,
          body: '串版本',
          severity: 'normal',
          shapes: [],
          canvasWidth: 1280,
          canvasHeight: 720,
          videoWidth: 1280,
          videoHeight: 720,
        },
        ctx(review),
      ),
    ).rejects.toMatchObject({ code: 'PROJECT_SCOPE_MISMATCH' });

    await expect(
      edit.resolveIssue(
        {
          projectRefId: project.projectRefId,
          reviewItemId: created.item.reviewItemId,
          versionId: created.version.versionId,
          issueId: 'issue_v2_001',
        },
        ctx(edit),
      ),
    ).rejects.toMatchObject({ code: 'PROJECT_SCOPE_MISMATCH' });
  });

  it('builds precise playback target from issue current revision and sorts next/previous issues', async () => {
    const { review } = makeHarness();
    const workspace = await review.getWorkspace({ projectRefId: 'prj_seed_final_cut', reviewItemId: 'item_ep28' });
    const [first, second, third] = workspace.currentIssues;
    const sorted = sortedIssuesForPlayback([
      { ...first, status: 'resolved', timestampMs: 1 },
      { ...third, status: 'unresolved', timestampMs: 240 },
      { ...second, status: 'unresolved', timestampMs: 240 },
    ]);
    expect(sorted.map((issue) => issue.issueId)).toEqual(['issue_v2_002', 'issue_v2_003', 'issue_v2_001']);
    const target = playbackTargetFromIssue(sorted[0]);
    expect(target).toMatchObject({
      projectRefId: 'prj_seed_final_cut',
      reviewItemId: 'item_ep28',
      versionId: 'ver_ep28_v2',
      issueId: 'issue_v2_002',
      revisionId: 'rev_issue_v2_002_001',
      annotationSetId: 'aset_issue_v2_002_001',
      timestampMs: 240,
      frameNumber: 6,
    });
  });

  it('sanitizes file names and zip path segments before download/package use', async () => {
    expect(sanitizeFileSegment('../bad/project', 'project')).toBe('bad_project');
    expect(sanitizeFileSegment('..', 'project')).toBe('project');
    expect(sanitizeDownloadFileName('..\\evil/clip.mp4', 'clip.mp4')).toBe('evil_clip.mp4');

    const { edit, review } = makeHarness();
    const project = await edit.createProject(
      {
        name: '../项目',
        code: '../PROJECT',
        description: '安全文件名测试项目。',
      },
      ctx(edit),
    );
    const created = await edit.createReviewItemWithVersion(
      {
        projectRefId: project.projectRefId,
        title: '../成片',
        episode: '../01',
        file: demoFile('../payload.mp4'),
      },
      ctx(edit),
    );
    const finalization = await review.finalizeCurrentVersion(
      {
        projectRefId: project.projectRefId,
        reviewItemId: created.item.reviewItemId,
        versionId: created.version.versionId,
        confirmed: true,
      },
      ctx(review),
    );
    expect(finalization.fileName).not.toContain('..');

    const pkg = await review.createProjectFinalizedPackage(project.projectRefId, ctx(review));
    expect(pkg.entries).toHaveLength(1);
    expect(pkg.fileName).toBe('PROJECT-finalized-originals.zip');
    expect(pkg.fileName).not.toMatch(/\.\.|\/|\\/);
    expect(pkg.entries[0].fileName).not.toMatch(/\.\.|\\/);
    expect(pkg.entries[0].fileName).toMatch(/^PROJECT\//);
  });
});
