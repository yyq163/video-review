import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  FinalCutReviewClient,
  type ReviewItemDTO,
  type ReviewVersionDTO,
  type UploadSessionDTO,
} from '../contracts-generated/backend-contract';
import type { ExecutionContext } from '../contracts/types';
import type { ReviewApiPort } from '../ports';
import { HttpReviewTransport } from './http-review-transport';
import { HttpReviewUploadOperation } from './http-review-upload-operation';
import {
  clearAppendVersionConfirmationRequired,
  HttpReviewUploads,
  isAppendVersionConfirmationRequired,
} from './http-review-uploads';

const BASE_URL = 'https://review.example';

afterEach(() => {
  vi.useRealTimers();
  window.sessionStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  FakeXMLHttpRequest.reset();
});

describe('HTTP review upload idempotency', () => {
  it('forwards the explicit delete confirmation in the command payload', async () => {
    const item = reviewItem(1);
    const command = vi.fn().mockResolvedValue(item);
    const transport = {
      assertWriteContext: vi.fn(),
      projectForWrite: vi.fn().mockResolvedValue(undefined),
      itemForLock: vi.fn().mockResolvedValue(item),
      command,
      baseUrl: BASE_URL,
    } as unknown as HttpReviewTransport;
    const uploads = new HttpReviewUploads(transport);

    await uploads.deleteReviewItem(
      {
        projectRefId: item.project_ref_id,
        reviewItemId: item.id,
        confirmed: true,
      },
      context('delete-confirmed'),
    );

    expect(command).toHaveBeenCalledWith(
      `/api/v1/final-cut-review/edit/projects/${item.project_ref_id}/items/${item.id}/delete`,
      'DeleteReviewItem',
      {
        project_ref_id: item.project_ref_id,
        review_item_id: item.id,
        confirmed: true,
      },
      expect.objectContaining({ requestId: 'delete-confirmed' }),
      item.lock_version,
    );
  });

  it('rejects false delete confirmation before loading or sending the command', async () => {
    const item = reviewItem(1);
    const projectForWrite = vi.fn();
    const command = vi.fn();
    const transport = {
      assertWriteContext: vi.fn(),
      projectForWrite,
      itemForLock: vi.fn(),
      command,
      baseUrl: BASE_URL,
    } as unknown as HttpReviewTransport;
    const uploads = new HttpReviewUploads(transport);

    await expect(
      uploads.deleteReviewItem(
        {
          projectRefId: item.project_ref_id,
          reviewItemId: item.id,
          confirmed: false,
        } as unknown as Parameters<ReviewApiPort['deleteReviewItem']>[0],
        context('delete-not-confirmed'),
      ),
    ).rejects.toMatchObject({ code: 'RESOURCE_STATE_CONFLICT' });

    expect(projectForWrite).not.toHaveBeenCalled();
    expect(command).not.toHaveBeenCalled();
  });

  it('releases a completed upload signature only after CreateReviewItem binding succeeds', async () => {
    const file = new File(['v1'], 'release-v1.mp4', { type: 'video/mp4', lastModified: 1 });
    const item = {
      ...reviewItem(1),
      id: 'item_release_v1',
      project_ref_id: 'prj_release_v1',
      current_version_id: 'ver_release_v1',
    };
    const version = {
      ...reviewVersion(1, file),
      id: 'ver_release_v1',
      project_ref_id: item.project_ref_id,
      review_item_id: item.id,
      previous_version_id: null,
    };
    const completed = {
      ...uploadSession(file, 'completed'),
      upload_id: 'upl_release_v1',
      file_id: 'file_release_v1',
    };
    const releaseCompleted = vi.spyOn(HttpReviewUploadOperation.prototype, 'releaseCompleted');
    const transport = {
      assertWriteContext: vi.fn(),
      projectForWrite: vi.fn().mockResolvedValue(undefined),
      command: vi.fn().mockResolvedValue(item),
      requestJson: vi.fn().mockResolvedValue(version),
      baseUrl: BASE_URL,
    } as unknown as HttpReviewTransport;
    const uploads = new HttpReviewUploads(transport, vi.fn().mockResolvedValue(completed));

    await expect(uploads.createReviewItemWithVersion({
      projectRefId: item.project_ref_id,
      title: item.title,
      episode: '1',
      file,
    }, context('release-v1'))).resolves.toMatchObject({ item: { reviewItemId: item.id } });

    expect(releaseCompleted).toHaveBeenCalledOnce();
    expect(releaseCompleted).toHaveBeenCalledWith(file, completed.upload_id);
  });

  it('rejects an unsupported extension before creating an upload session', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const uploads = new HttpReviewUploads(
      new HttpReviewTransport('edit', new FinalCutReviewClient(BASE_URL), BASE_URL),
    );
    const file = new File(['webm'], 'unsupported.webm', {
      type: 'application/octet-stream',
      lastModified: 1,
    });

    await expect(uploads.uploadFile(file, context('unsupported'))).rejects.toMatchObject({
      code: 'FILE_TYPE_NOT_ALLOWED',
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('replays upload init with the same idempotency key after its response is lost', async () => {
    const file = new File(['lost-init'], 'lost-init.mp4', { type: 'video/mp4', lastModified: 1 });
    const initiated = uploadSession(file, 'initiated');
    const receiving = uploadSession(file, 'receiving');
    const completed = { ...uploadSession(file, 'completed'), file_id: 'file_lost_init' };
    const initKeys: string[] = [];
    let initCalls = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/v1/files/uploads/init')) {
        initCalls += 1;
        initKeys.push(new Headers(init?.headers).get('Idempotency-Key') ?? '');
        if (initCalls === 1) throw new TypeError('init response lost');
        return envelope(initiated);
      }
      if (url.endsWith('/api/v1/files/uploads/upl_lost_complete/complete')) return envelope(completed);
      throw new Error(`unexpected URL ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    installFakeXhr({ status: 200, response: envelopeBody(receiving) });
    const uploads = new HttpReviewUploads(
      new HttpReviewTransport('edit', new FinalCutReviewClient(BASE_URL), BASE_URL),
    );

    await expect(uploads.uploadFile(file, context('first'))).rejects.toThrow('init response lost');
    await expect(uploads.uploadFile(file, context('retry'))).resolves.toMatchObject({
      upload_id: 'upl_lost_complete',
      file_id: 'file_lost_init',
    });

    expect(initCalls).toBe(2);
    expect(new Set(initKeys).size).toBe(1);
    expect(initKeys[0]).toMatch(/^InitUpload_/);
  });

  it('replays complete with the same upload id and idempotency key after its response is lost', async () => {
    const file = new File(['lost-complete'], 'lost-complete.mp4', { type: 'video/mp4', lastModified: 1 });
    const initiated = uploadSession(file, 'initiated');
    const receiving = uploadSession(file, 'receiving');
    const completed = { ...uploadSession(file, 'completed'), file_id: 'file_lost_complete' };
    const completeKeys: string[] = [];
    const completeBodies: Array<BodyInit | null | undefined> = [];
    let initCalls = 0;
    let completeCalls = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/v1/files/uploads/init')) {
        initCalls += 1;
        return envelope(initiated);
      }
      if (url.endsWith('/api/v1/files/uploads/upl_lost_complete/complete')) {
        completeCalls += 1;
        completeKeys.push(new Headers(init?.headers).get('Idempotency-Key') ?? '');
        completeBodies.push(init?.body);
        if (completeCalls === 1) throw new TypeError('complete response lost');
        return envelope(completed);
      }
      throw new Error(`unexpected URL ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    installFakeXhr({ status: 200, response: envelopeBody(receiving) });
    const uploads = new HttpReviewUploads(
      new HttpReviewTransport('edit', new FinalCutReviewClient(BASE_URL), BASE_URL),
    );

    await expect(uploads.uploadFile(file, context('first'))).rejects.toThrow('complete response lost');
    await expect(uploads.uploadFile(file, context('retry'))).resolves.toMatchObject({
      upload_id: 'upl_lost_complete',
      file_id: 'file_lost_complete',
    });

    expect(initCalls).toBe(1);
    expect(FakeXMLHttpRequest.instances).toHaveLength(1);
    expect(FakeXMLHttpRequest.instances[0]?.requestBody).toBeInstanceOf(Blob);
    expect(completeCalls).toBe(2);
    expect(new Set(completeKeys).size).toBe(1);
    expect(completeKeys[0]).toMatch(/^CompleteUpload_/);
    expect(completeBodies).toEqual([undefined, undefined]);
  });

  it.each([
    { currentVersionNo: 1, expectedLabel: 'V2' },
    { currentVersionNo: 2, expectedLabel: 'V3' },
    { currentVersionNo: 3, expectedLabel: 'V4' },
  ])(
    'reuses the completed upload and UploadReviewVersion command for a lost $expectedLabel response',
    async ({ currentVersionNo, expectedLabel }) => {
      const file = new File([expectedLabel], `${expectedLabel.toLowerCase()}.mp4`, {
        type: 'video/mp4',
        lastModified: currentVersionNo,
      });
      const item = reviewItem(currentVersionNo);
      const version = reviewVersion(currentVersionNo + 1, file);
      const updatedItem = {
        ...item,
        workflow_status: 'pending_review' as const,
        current_version_id: version.id,
        current_version_no: version.version_no,
      };
      const requestJson = vi.fn().mockResolvedValueOnce(item).mockResolvedValue(updatedItem);
      const command = vi.fn().mockRejectedValueOnce(new TypeError('append response lost')).mockResolvedValueOnce(version);
      const projectForWrite = vi.fn().mockResolvedValue(undefined);
      const transport = {
        assertWriteContext: vi.fn(),
        projectForWrite,
        requestJson,
        command,
        baseUrl: BASE_URL,
      } as unknown as HttpReviewTransport;
      const upload = vi.fn().mockResolvedValue({
        ...uploadSession(file, 'completed'),
        upload_id: `upl_${expectedLabel.toLowerCase()}`,
        file_id: `file_${expectedLabel.toLowerCase()}`,
      });
      const releaseCompleted = vi.spyOn(HttpReviewUploadOperation.prototype, 'releaseCompleted');
      const uploads = new HttpReviewUploads(transport, upload);
      const input = {
        projectRefId: item.project_ref_id,
        reviewItemId: item.id,
        file,
        versionNote: `${expectedLabel} note`,
        changeSummary: `${expectedLabel} changes`,
        supersedeReason: '',
      };

      await expect(uploads.appendVersion(input, context('first'))).rejects.toThrow('append response lost');
      expect(releaseCompleted).not.toHaveBeenCalled();
      expect(isAppendVersionConfirmationRequired(item.project_ref_id, item.id)).toBe(true);
      const persisted = Array.from({ length: window.sessionStorage.length }, (_, index) => {
        const key = window.sessionStorage.key(index) ?? '';
        return [key, window.sessionStorage.getItem(key)] as const;
      });
      expect(persisted).toHaveLength(1);
      expect(persisted[0]?.[1]).toBe('required');
      expect(JSON.stringify(persisted)).not.toContain(file.name);
      expect(JSON.stringify(persisted)).not.toContain(input.versionNote);
      expect(JSON.stringify(persisted)).not.toContain(input.changeSummary);
      expect(JSON.stringify(persisted)).not.toContain('UploadReviewVersion_');
      await expect(uploads.appendVersion(input, context('retry'))).resolves.toMatchObject({
        label: expectedLabel,
        status: 'pending_review',
      });

      expect(projectForWrite).toHaveBeenCalledTimes(2);
      expect(requestJson).toHaveBeenCalledTimes(2);
      expect(upload).toHaveBeenCalledTimes(1);
      expect(command).toHaveBeenCalledTimes(2);
      expect(releaseCompleted).toHaveBeenCalledOnce();
      expect(releaseCompleted).toHaveBeenCalledWith(file, `upl_${expectedLabel.toLowerCase()}`);
      const firstCall = command.mock.calls[0];
      const retryCall = command.mock.calls[1];
      const firstOptions = firstCall?.[5] as { commandId: string; idempotent: boolean };
      const retryOptions = retryCall?.[5] as { commandId: string; idempotent: boolean };
      expect(firstCall?.[1]).toBe('UploadReviewVersion');
      expect(retryCall?.[1]).toBe('UploadReviewVersion');
      expect(firstCall?.[2]).toEqual(retryCall?.[2]);
      expect(firstCall?.[4]).toBe(item.lock_version);
      expect(retryCall?.[4]).toBe(item.lock_version);
      expect(firstOptions.idempotent).toBe(true);
      expect(firstOptions.commandId).toMatch(/^UploadReviewVersion_/);
      expect(retryOptions.commandId).toBe(firstOptions.commandId);
      expect(isAppendVersionConfirmationRequired(item.project_ref_id, item.id)).toBe(true);
      clearAppendVersionConfirmationRequired(item.project_ref_id, item.id);
      expect(isAppendVersionConfirmationRequired(item.project_ref_id, item.id)).toBe(false);
    },
  );
});

describe('Safari-compatible binary upload transport', () => {
  it('registers listeners before open/send and sends a credentialed raw Blob with progress', async () => {
    const file = new File(['part-body'], 'part.mp4', { type: 'video/mp4', lastModified: 1 });
    const receiving = uploadSession(file, 'receiving');
    installFakeXhr({
      status: 200,
      response: envelopeBody(receiving),
      progress: [{ loaded: 4, total: file.size }],
    });
    const transport = new HttpReviewTransport('edit', new FinalCutReviewClient(BASE_URL), BASE_URL);
    const progress = vi.fn();

    await expect(
      transport.requestBinaryJson('/api/v1/files/uploads/upl_transport/parts/1', file, context('xhr-request'), progress),
    ).resolves.toMatchObject({ status: 'receiving' });

    const xhr = FakeXMLHttpRequest.instances[0];
    expect(xhr).toBeDefined();
    expect(xhr?.method).toBe('PUT');
    expect(xhr?.url).toBe(`${BASE_URL}/api/v1/files/uploads/upl_transport/parts/1`);
    expect(xhr?.async).toBe(true);
    expect(xhr?.withCredentials).toBe(true);
    expect(xhr?.requestHeaders.get('x-request-id')).toBe('xhr-request');
    expect(xhr?.requestHeaders.get('content-type')).toBe('application/octet-stream');
    expect(xhr?.requestBody).toBe(file);
    expect(progress).toHaveBeenCalledWith(4);

    const order = xhr?.order ?? [];
    expect(order.indexOf('upload-listener:progress')).toBeLessThan(order.indexOf('open'));
    const sendIndex = order.indexOf('send');
    for (const listener of [
      'upload-listener:progress',
      'xhr-listener:load',
      'xhr-listener:error',
      'xhr-listener:timeout',
      'xhr-listener:abort',
    ]) {
      expect(order.indexOf(listener)).toBeGreaterThanOrEqual(0);
      expect(order.indexOf(listener)).toBeLessThan(sendIndex);
    }
  });

  it('parses the normal error envelope into FinalCutReviewHttpError', async () => {
    installFakeXhr({
      status: 413,
      response: {
        error: {
          code: 'FILE_TOO_LARGE',
          message: '上传分片过大',
          http_status: 413,
          details: {},
          request_id: 'req-error',
          timestamp: '2026-07-16T00:00:00.000Z',
          contract_version: '1.0',
        },
      },
    });
    const transport = new HttpReviewTransport('edit', new FinalCutReviewClient(BASE_URL), BASE_URL);

    await expect(
      transport.requestBinaryJson('/api/v1/files/uploads/upl_error/parts/1', new Blob(['x']), context('xhr-error')),
    ).rejects.toMatchObject({ code: 'FILE_TOO_LARGE', httpStatus: 413, requestId: 'req-error' });
  });

  it('renews the 180 second inactivity timer on every progress event and rejects only once before abort', async () => {
    vi.useFakeTimers();
    installFakeXhr({ terminal: 'none' });
    const transport = new HttpReviewTransport('edit', new FinalCutReviewClient(BASE_URL), BASE_URL);
    const progress = vi.fn();
    const request = transport.requestBinaryJson(
      '/api/v1/files/uploads/upl_timeout/parts/1',
      new Blob(['timeout']),
      context('xhr-timeout'),
      progress,
    );
    const rejection = expect(request).rejects.toThrow('上传分片长时间无进度');
    const xhr = FakeXMLHttpRequest.instances[0];

    await vi.advanceTimersByTimeAsync(120_000);
    xhr?.upload.emitProgress(1, 7, false);
    await vi.advanceTimersByTimeAsync(120_000);
    xhr?.upload.emitProgress(2, 7, true);
    await vi.advanceTimersByTimeAsync(179_999);
    expect(xhr?.aborted).toBe(false);
    expect(progress).toHaveBeenCalledTimes(1);
    expect(progress).toHaveBeenCalledWith(2);

    await vi.advanceTimersByTimeAsync(1);
    await rejection;
    expect(xhr?.abortCalls).toBe(1);
  });
});

describe('HTTP upload retry identity and bounded parts', () => {
  it('rejects a different same-signature File while the original upload is in flight', async () => {
    const firstFile = new File([new Uint8Array([1, 2, 3])], 'collision.mp4', {
      type: 'video/mp4',
      lastModified: 21,
    });
    const collidingFile = new File([new Uint8Array([4, 5, 6])], 'collision.mp4', {
      type: 'video/mp4',
      lastModified: 21,
    });
    const initiated = uploadSession(firstFile, 'initiated');
    const receiving = uploadSession(firstFile, 'receiving');
    const completed = { ...uploadSession(firstFile, 'completed'), file_id: 'file_collision_first' };
    let initCalls = 0;
    let completeCalls = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/api/v1/files/uploads/init')) {
        initCalls += 1;
        return envelope(initiated);
      }
      if (url.endsWith('/api/v1/files/uploads/upl_lost_complete/complete')) {
        completeCalls += 1;
        return envelope(completed);
      }
      throw new Error(`unexpected URL ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    installFakeXhr({ terminal: 'none' });
    const operation = new HttpReviewUploadOperation(
      new HttpReviewTransport('edit', new FinalCutReviewClient(BASE_URL), BASE_URL),
    );

    const firstUpload = operation.upload(firstFile, context('collision-first'));
    await vi.waitFor(() => expect(FakeXMLHttpRequest.instances).toHaveLength(1));

    await expect(
      operation.upload(collidingFile, context('collision-second')),
    ).rejects.toMatchObject({ code: 'CLIENT_UPLOAD_FILE_IDENTITY_CONFLICT' });
    expect(initCalls).toBe(1);
    expect(completeCalls).toBe(0);
    expect(FakeXMLHttpRequest.instances).toHaveLength(1);

    const xhr = FakeXMLHttpRequest.instances[0] as FakeXMLHttpRequest;
    xhr.status = 200;
    xhr.responseText = JSON.stringify(envelopeBody(receiving));
    xhr.emit('load');
    await expect(firstUpload).resolves.toMatchObject({ file_id: 'file_collision_first' });
    expect(completeCalls).toBe(1);
  });

  it('keeps ten simultaneous upload operations independent', async () => {
    const files = Array.from({ length: 10 }, (_, index) => new File(
      [new Uint8Array([index])],
      `concurrent-${index}.mp4`,
      { type: 'video/mp4', lastModified: index },
    ));
    const sessions = new Map(files.map((file, index) => [
      file.name,
      { ...uploadSession(file, 'initiated'), upload_id: `upl_concurrent_${index}` },
    ]));
    const initKeys = new Set<string>();
    let completeCalls = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/v1/files/uploads/init')) {
        const payload = JSON.parse(String(init?.body)) as { original_filename: string };
        initKeys.add(new Headers(init?.headers).get('Idempotency-Key') ?? '');
        const session = sessions.get(payload.original_filename);
        if (!session) throw new Error(`unexpected file ${payload.original_filename}`);
        return envelope(session);
      }
      const uploadId = url.match(/\/uploads\/(upl_concurrent_\d+)\/complete$/)?.[1];
      if (uploadId) {
        completeCalls += 1;
        const index = Number(uploadId.slice('upl_concurrent_'.length));
        return envelope({ ...uploadSession(files[index] as File, 'completed'), upload_id: uploadId, file_id: `file_${index}` });
      }
      throw new Error(`unexpected URL ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    installFakeXhr(...files.map(() => ({ terminal: 'none' as const })));
    const uploads = new HttpReviewUploads(
      new HttpReviewTransport('edit', new FinalCutReviewClient(BASE_URL), BASE_URL),
    );

    const completion = Promise.all(files.map((file, index) => (
      uploads.uploadFile(file, context(`concurrent-${index}`))
    )));

    await vi.waitFor(() => expect(FakeXMLHttpRequest.instances).toHaveLength(10));
    expect(completeCalls).toBe(0);
    for (const xhr of FakeXMLHttpRequest.instances) {
      const uploadId = xhr.url.match(/\/uploads\/(upl_concurrent_\d+)\/parts\/1$/)?.[1];
      expect(uploadId).toBeDefined();
      const index = Number(uploadId?.slice('upl_concurrent_'.length));
      xhr.status = 200;
      xhr.responseText = JSON.stringify(envelopeBody({
        ...uploadSession(files[index] as File, 'receiving'),
        upload_id: uploadId as string,
      }));
      xhr.emit('load');
    }
    const completed = await completion;

    expect(completed.map((session) => session.file_id)).toEqual(
      Array.from({ length: 10 }, (_, index) => `file_${index}`),
    );
    expect(FakeXMLHttpRequest.instances).toHaveLength(10);
    expect(new Set(FakeXMLHttpRequest.instances.map((xhr) => xhr.url)).size).toBe(10);
    expect(initKeys.size).toBe(10);
    expect(completeCalls).toBe(10);
  });

  it.each([
    { label: 'network', first: { terminal: 'error' as const }, aborts: 0 },
    { label: 'malformed envelope', first: { status: 200, responseText: '<html>' }, aborts: 0 },
    { label: 'timeout', first: { terminal: 'timeout' as const }, aborts: 1 },
  ])('retries a lost part after $label with a new same-signature File', async ({ first, aborts }) => {
    const firstFile = new File([new Uint8Array([1, 2, 3])], 'same.mp4', {
      type: 'video/mp4',
      lastModified: 7,
    });
    const retryFile = new File([new Uint8Array([4, 5, 6])], 'same.mp4', {
      type: 'video/mp4',
      lastModified: 7,
    });
    const initiated = uploadSession(firstFile, 'initiated');
    const receiving = uploadSession(firstFile, 'receiving');
    const completed = { ...uploadSession(firstFile, 'completed'), file_id: 'file_same' };
    let initCalls = 0;
    const requestedUrls: string[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      requestedUrls.push(url);
      if (url.endsWith('/api/v1/files/uploads/init')) {
        initCalls += 1;
        return envelope(initiated);
      }
      if (url.endsWith('/api/v1/files/uploads/upl_lost_complete/complete')) return envelope(completed);
      throw new Error(`unexpected URL ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    installFakeXhr(first, { status: 200, response: envelopeBody(receiving) });
    const uploads = new HttpReviewUploads(
      new HttpReviewTransport('edit', new FinalCutReviewClient(BASE_URL), BASE_URL),
    );

    await expect(uploads.uploadFile(firstFile, context('lost-part'))).rejects.toBeDefined();
    await expect(uploads.uploadFile(retryFile, context('retry-part'))).resolves.toMatchObject({
      upload_id: 'upl_lost_complete',
      file_id: 'file_same',
    });

    expect(initCalls).toBe(1);
    expect(FakeXMLHttpRequest.instances).toHaveLength(2);
    expect(FakeXMLHttpRequest.instances.map((xhr) => xhr.url)).toEqual([
      `${BASE_URL}/api/v1/files/uploads/upl_lost_complete/parts/1`,
      `${BASE_URL}/api/v1/files/uploads/upl_lost_complete/parts/1`,
    ]);
    expect(FakeXMLHttpRequest.instances[0]?.abortCalls).toBe(aborts);
    expect(requestedUrls.some((url) => url.includes('/abort'))).toBe(false);
  });

  it('rejects a same-signature replacement whose persisted prefix differs and preserves the original resume state', async () => {
    const firstPartBytes = 8 * 1024 * 1024;
    const trustedPrefix = new Blob([new Uint8Array(firstPartBytes).fill(0x11)]);
    const conflictingPrefix = new Blob([new Uint8Array(firstPartBytes).fill(0x22)]);
    const originalTail = new Uint8Array([1, 2, 3]);
    const firstFile = new File([trustedPrefix, originalTail], 'persisted-prefix.mp4', {
      type: 'video/mp4',
      lastModified: 31,
    });
    const conflictingFile = new File([conflictingPrefix, originalTail], firstFile.name, {
      type: firstFile.type,
      lastModified: firstFile.lastModified,
    });
    const initiated = uploadSession(firstFile, 'initiated');
    const receiving = uploadSession(firstFile, 'receiving');
    const completed = { ...uploadSession(firstFile, 'completed'), file_id: 'file_persisted_prefix' };
    let initCalls = 0;
    let completeCalls = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/api/v1/files/uploads/init')) {
        initCalls += 1;
        return envelope(initiated);
      }
      if (url.endsWith(`/api/v1/files/uploads/${initiated.upload_id}/complete`)) {
        completeCalls += 1;
        return envelope(completed);
      }
      throw new Error(`unexpected URL ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    installFakeXhr(
      { status: 200, response: envelopeBody(receiving) },
      { terminal: 'error' },
      { status: 200, response: envelopeBody(receiving) },
    );
    const operation = new HttpReviewUploadOperation(
      new HttpReviewTransport('edit', new FinalCutReviewClient(BASE_URL), BASE_URL),
    );
    const identityReadSpy = vi.spyOn(FileReader.prototype, 'readAsArrayBuffer');

    await expect(operation.upload(firstFile, context('prefix-first'))).rejects.toThrow(
      '上传分片网络请求失败',
    );
    expect(FakeXMLHttpRequest.instances).toHaveLength(2);

    await expect(
      operation.upload(conflictingFile, context('prefix-conflict')),
    ).rejects.toMatchObject({ code: 'CLIENT_UPLOAD_FILE_IDENTITY_CONFLICT' });
    expect(FakeXMLHttpRequest.instances).toHaveLength(2);
    expect(completeCalls).toBe(0);
    expect(identityReadSpy).toHaveBeenCalled();

    identityReadSpy.mockClear();
    await expect(operation.upload(firstFile, context('prefix-original-retry'))).resolves.toMatchObject({
      file_id: 'file_persisted_prefix',
    });
    expect(identityReadSpy).not.toHaveBeenCalled();
    expect(initCalls).toBe(1);
    expect(completeCalls).toBe(1);
    expect(FakeXMLHttpRequest.instances.map((xhr) => xhr.url)).toEqual([
      `${BASE_URL}/api/v1/files/uploads/${initiated.upload_id}/parts/1`,
      `${BASE_URL}/api/v1/files/uploads/${initiated.upload_id}/parts/2`,
      `${BASE_URL}/api/v1/files/uploads/${initiated.upload_id}/parts/2`,
    ]);
    const retriedTail = await blobBytes(FakeXMLHttpRequest.instances[2]?.requestBody as Blob);
    expect(Array.from(retriedTail)).toEqual(Array.from(originalTail));
  });

  it('atomically adopts one verified replacement and uploads only its exact remaining bytes', async () => {
    const firstPartBytes = 8 * 1024 * 1024;
    const sharedPrefixByte = 0x33;
    const sharedPrefix = new Blob([new Uint8Array(firstPartBytes).fill(sharedPrefixByte)]);
    const firstFile = new File([sharedPrefix, new Uint8Array([1, 2, 3, 4])], 'verified-resume.mp4', {
      type: 'video/mp4',
      lastModified: 32,
    });
    const replacementTail = new Uint8Array([9, 8, 7, 6]);
    const replacementFile = new File([sharedPrefix, replacementTail], firstFile.name, {
      type: firstFile.type,
      lastModified: firstFile.lastModified,
    });
    const competingFile = new File([sharedPrefix, new Uint8Array([6, 7, 8, 9])], firstFile.name, {
      type: firstFile.type,
      lastModified: firstFile.lastModified,
    });
    const initiated = uploadSession(firstFile, 'initiated');
    const receiving = uploadSession(firstFile, 'receiving');
    const completed = { ...uploadSession(replacementFile, 'completed'), file_id: 'file_verified_resume' };
    let completeCalls = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/api/v1/files/uploads/init')) return envelope(initiated);
      if (url.endsWith(`/api/v1/files/uploads/${initiated.upload_id}/complete`)) {
        completeCalls += 1;
        return envelope(completed);
      }
      throw new Error(`unexpected URL ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    installFakeXhr(
      { status: 200, response: envelopeBody(receiving) },
      { terminal: 'error' },
      { status: 200, response: envelopeBody(receiving) },
    );
    const operation = new HttpReviewUploadOperation(
      new HttpReviewTransport('edit', new FinalCutReviewClient(BASE_URL), BASE_URL),
    );

    await expect(operation.upload(firstFile, context('verified-first'))).rejects.toThrow(
      '上传分片网络请求失败',
    );
    const replacementUpload = operation.upload(replacementFile, context('verified-replacement'));
    await expect(
      operation.upload(competingFile, context('verified-competing')),
    ).rejects.toMatchObject({ code: 'CLIENT_UPLOAD_FILE_IDENTITY_CONFLICT' });
    await expect(replacementUpload).resolves.toMatchObject({ file_id: 'file_verified_resume' });

    expect(completeCalls).toBe(1);
    expect(FakeXMLHttpRequest.instances.map((xhr) => xhr.url)).toEqual([
      `${BASE_URL}/api/v1/files/uploads/${initiated.upload_id}/parts/1`,
      `${BASE_URL}/api/v1/files/uploads/${initiated.upload_id}/parts/2`,
      `${BASE_URL}/api/v1/files/uploads/${initiated.upload_id}/parts/2`,
    ]);
    const persistedPrefix = await blobBytes(FakeXMLHttpRequest.instances[0]?.requestBody as Blob);
    const resumedTail = await blobBytes(FakeXMLHttpRequest.instances[2]?.requestBody as Blob);
    expect(persistedPrefix).toHaveLength(firstPartBytes);
    expect(persistedPrefix[0]).toBe(sharedPrefixByte);
    expect(persistedPrefix.at(-1)).toBe(sharedPrefixByte);
    expect(Array.from(resumedTail)).toEqual(Array.from(replacementTail));
  });

  it('compares the full file before retrying an uncertain complete response', async () => {
    const firstFile = new File(['trusted'], 'uncertain-complete.mp4', {
      type: 'video/mp4',
      lastModified: 33,
    });
    const conflictingFile = new File(['hostile'], firstFile.name, {
      type: firstFile.type,
      lastModified: firstFile.lastModified,
    });
    const initiated = uploadSession(firstFile, 'initiated');
    const receiving = uploadSession(firstFile, 'receiving');
    const completed = { ...uploadSession(firstFile, 'completed'), file_id: 'file_uncertain_complete' };
    let completeCalls = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/api/v1/files/uploads/init')) return envelope(initiated);
      if (url.endsWith(`/api/v1/files/uploads/${initiated.upload_id}/complete`)) {
        completeCalls += 1;
        if (completeCalls === 1) throw new TypeError('complete response lost');
        return envelope(completed);
      }
      throw new Error(`unexpected URL ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    installFakeXhr({ status: 200, response: envelopeBody(receiving) });
    const operation = new HttpReviewUploadOperation(
      new HttpReviewTransport('edit', new FinalCutReviewClient(BASE_URL), BASE_URL),
    );

    await expect(operation.upload(firstFile, context('complete-first'))).rejects.toThrow(
      'complete response lost',
    );
    await expect(
      operation.upload(conflictingFile, context('complete-conflict')),
    ).rejects.toMatchObject({ code: 'CLIENT_UPLOAD_FILE_IDENTITY_CONFLICT' });
    expect(completeCalls).toBe(1);
    expect(FakeXMLHttpRequest.instances).toHaveLength(1);

    await expect(operation.upload(firstFile, context('complete-original-retry'))).resolves.toMatchObject({
      file_id: 'file_uncertain_complete',
    });
    expect(completeCalls).toBe(2);
    expect(FakeXMLHttpRequest.instances).toHaveLength(1);
  });

  it.each([
    {
      label: 'a malformed DTO',
      expectedCode: 'UPLOAD_RESPONSE_INVALID',
      firstData: () => ({ upload_id: 'upl_lost_complete', status: 'receiving' }),
    },
    {
      label: 'a mismatched upload id',
      expectedCode: 'UPLOAD_RESPONSE_ID_MISMATCH',
      firstData: (file: File) => ({
        ...uploadSession(file, 'receiving'),
        upload_id: 'upl_wrong_session',
      }),
    },
  ])('retains the original session and retries the same part after $label', async ({ expectedCode, firstData }) => {
    const firstFile = new File(['strict'], 'strict-response.mp4', { type: 'video/mp4', lastModified: 11 });
    const retryFile = new File(['retry!'], 'strict-response.mp4', { type: 'video/mp4', lastModified: 11 });
    const initiated = uploadSession(firstFile, 'initiated');
    const receiving = uploadSession(firstFile, 'receiving');
    const completed = { ...uploadSession(firstFile, 'completed'), file_id: 'file_strict' };
    let initCalls = 0;
    let completeCalls = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/api/v1/files/uploads/init')) {
        initCalls += 1;
        return envelope(initiated);
      }
      if (url.endsWith('/api/v1/files/uploads/upl_lost_complete/complete')) {
        completeCalls += 1;
        return envelope(completed);
      }
      throw new Error(`unexpected URL ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    installFakeXhr(
      { status: 200, response: { data: firstData(firstFile), meta: { request_id: 'req-invalid', contract_version: '1.0' } } },
      { status: 200, response: envelopeBody(receiving) },
    );
    const uploads = new HttpReviewUploads(
      new HttpReviewTransport('edit', new FinalCutReviewClient(BASE_URL), BASE_URL),
    );

    await expect(uploads.uploadFile(firstFile, context('invalid-part'))).rejects.toMatchObject({
      code: expectedCode,
    });
    await expect(uploads.uploadFile(retryFile, context('retry-valid-part'))).resolves.toMatchObject({
      upload_id: initiated.upload_id,
      file_id: 'file_strict',
    });

    expect(initCalls).toBe(1);
    expect(completeCalls).toBe(1);
    expect(FakeXMLHttpRequest.instances.map((xhr) => xhr.url)).toEqual([
      `${BASE_URL}/api/v1/files/uploads/${initiated.upload_id}/parts/1`,
      `${BASE_URL}/api/v1/files/uploads/${initiated.upload_id}/parts/1`,
    ]);
  });

  it('fans one deferred upload out to every progress subscriber without regression', async () => {
    const file = new File([new Uint8Array(8)], 'subscribers.mp4', { type: 'video/mp4', lastModified: 12 });
    const initiated = uploadSession(file, 'initiated');
    const receiving = uploadSession(file, 'receiving');
    const completed = { ...uploadSession(file, 'completed'), file_id: 'file_subscribers' };
    let initCalls = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/api/v1/files/uploads/init')) {
        initCalls += 1;
        return envelope(initiated);
      }
      if (url.endsWith('/api/v1/files/uploads/upl_lost_complete/complete')) return envelope(completed);
      throw new Error(`unexpected URL ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    installFakeXhr({ terminal: 'none' });
    const uploads = new HttpReviewUploads(
      new HttpReviewTransport('edit', new FinalCutReviewClient(BASE_URL), BASE_URL),
    );
    const firstProgress: Array<{ percent: number; bytesSent?: number }> = [];
    const secondProgress: Array<{ percent: number; bytesSent?: number }> = [];

    const first = uploads.uploadFile(file, context('subscriber-first'), (progress) => firstProgress.push(progress));
    await vi.waitFor(() => expect(FakeXMLHttpRequest.instances).toHaveLength(1));
    const second = uploads.uploadFile(file, context('subscriber-second'), (progress) => secondProgress.push(progress));
    const xhr = FakeXMLHttpRequest.instances[0] as FakeXMLHttpRequest;
    xhr.upload.emitProgress(6, 8);
    xhr.upload.emitProgress(3, 8);
    xhr.upload.emitProgress(8, 8);
    xhr.status = 200;
    xhr.responseText = JSON.stringify(envelopeBody(receiving));
    xhr.emit('load');

    await expect(Promise.all([first, second])).resolves.toHaveLength(2);
    expect(initCalls).toBe(1);
    expect(FakeXMLHttpRequest.instances).toHaveLength(1);
    expect(firstProgress.some((progress) => progress.bytesSent === 6)).toBe(true);
    expect(secondProgress.some((progress) => progress.bytesSent === 6)).toBe(true);
    for (const progress of [firstProgress, secondProgress]) {
      expect(progress.map(({ percent }) => percent)).toEqual(
        [...progress.map(({ percent }) => percent)].sort((left, right) => left - right),
      );
      const bytes = progress.flatMap(({ bytesSent }) => bytesSent === undefined ? [] : [bytesSent]);
      expect(bytes).toEqual([...bytes].sort((left, right) => left - right));
    }
  });

  it('touches pending entries as LRU but never evicts uncertain or completed-unbound uploads', async () => {
    const files = Array.from({ length: 32 }, (_, index) => new File(
      [new Uint8Array([index])],
      `pending-${index}.mp4`,
      { type: 'video/mp4', lastModified: index },
    ));
    let initCalls = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/v1/files/uploads/init')) {
        initCalls += 1;
        const payload = JSON.parse(String(init?.body)) as { original_filename: string };
        const index = Number(payload.original_filename.match(/^pending-(\d+)\.mp4$/)?.[1]);
        return envelope({
          ...uploadSession(files[index] as File, 'initiated'),
          upload_id: `upl_pending_${index}`,
        });
      }
      const uploadId = url.match(/\/uploads\/(upl_pending_(\d+))\/complete$/)?.[1];
      if (uploadId) {
        const index = Number(uploadId.slice('upl_pending_'.length));
        return envelope({
          ...uploadSession(files[index] as File, 'completed'),
          upload_id: uploadId,
          file_id: `file_pending_${index}`,
        });
      }
      throw new Error(`unexpected URL ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    installFakeXhr(
      ...files.map(() => ({ terminal: 'error' as const })),
      {
        status: 200,
        response: envelopeBody({
          ...uploadSession(files[0] as File, 'receiving'),
          upload_id: 'upl_pending_0',
        }),
      },
      {
        status: 200,
        response: envelopeBody({
          ...uploadSession(files[1] as File, 'receiving'),
          upload_id: 'upl_pending_1',
        }),
      },
    );
    const uploads = new HttpReviewUploads(
      new HttpReviewTransport('edit', new FinalCutReviewClient(BASE_URL), BASE_URL),
    );

    for (const [index, file] of files.entries()) {
      await expect(uploads.uploadFile(file, context(`pending-${index}`))).rejects.toThrow(
        '上传分片网络请求失败',
      );
    }
    const firstRetry = new File(['x'], files[0]?.name ?? '', { type: 'video/mp4', lastModified: 0 });
    await expect(uploads.uploadFile(firstRetry, context('pending-first-retry'))).resolves.toMatchObject({
      upload_id: 'upl_pending_0',
    });

    const overflow = new File(['x'], 'pending-overflow.mp4', { type: 'video/mp4', lastModified: 99 });
    await expect(uploads.uploadFile(overflow, context('pending-overflow'))).rejects.toMatchObject({
      code: 'CLIENT_UPLOAD_QUEUE_FULL',
    });

    const secondRetry = new File(['x'], files[1]?.name ?? '', { type: 'video/mp4', lastModified: 1 });
    await expect(uploads.uploadFile(secondRetry, context('pending-second-retry'))).resolves.toMatchObject({
      upload_id: 'upl_pending_1',
    });
    await expect(uploads.uploadFile(firstRetry, context('pending-first-cached'))).resolves.toMatchObject({
      upload_id: 'upl_pending_0',
    });

    expect(initCalls).toBe(32);
    expect(FakeXMLHttpRequest.instances).toHaveLength(34);
  });

  it('releases a completed signature only when the exact upload id is confirmed bound', async () => {
    const file = new File(['a'], 'release-exact.mp4', { type: 'video/mp4', lastModified: 13 });
    let initCalls = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/api/v1/files/uploads/init')) {
        initCalls += 1;
        return envelope({
          ...uploadSession(file, 'initiated'),
          upload_id: `upl_release_${initCalls}`,
        });
      }
      const uploadId = url.match(/\/uploads\/(upl_release_\d+)\/complete$/)?.[1];
      if (uploadId) {
        return envelope({
          ...uploadSession(file, 'completed'),
          upload_id: uploadId,
          file_id: `file_${uploadId}`,
        });
      }
      throw new Error(`unexpected URL ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    installFakeXhr(
      {
        status: 200,
        response: envelopeBody({
          ...uploadSession(file, 'receiving'),
          upload_id: 'upl_release_1',
        }),
      },
      {
        status: 200,
        response: envelopeBody({
          ...uploadSession(file, 'receiving'),
          upload_id: 'upl_release_2',
        }),
      },
    );
    const operation = new HttpReviewUploadOperation(
      new HttpReviewTransport('edit', new FinalCutReviewClient(BASE_URL), BASE_URL),
    );

    await expect(operation.upload(file, context('release-first'))).resolves.toMatchObject({
      upload_id: 'upl_release_1',
    });
    expect(operation.releaseCompleted(file, 'upl_wrong')).toBe(false);
    await expect(operation.upload(file, context('release-cached'))).resolves.toMatchObject({
      upload_id: 'upl_release_1',
    });
    expect(initCalls).toBe(1);

    const collidingFile = new File(['b'], file.name, {
      type: file.type,
      lastModified: file.lastModified,
    });
    await expect(
      operation.upload(collidingFile, context('release-collision')),
    ).rejects.toMatchObject({ code: 'CLIENT_UPLOAD_FILE_IDENTITY_CONFLICT' });
    expect(operation.releaseCompleted(collidingFile, 'upl_release_1')).toBe(false);
    expect(initCalls).toBe(1);
    expect(FakeXMLHttpRequest.instances).toHaveLength(1);

    expect(operation.releaseCompleted(file, 'upl_release_1')).toBe(true);
    await expect(operation.upload(collidingFile, context('release-second'))).resolves.toMatchObject({
      upload_id: 'upl_release_2',
    });
    expect(initCalls).toBe(2);
  });

  it('emits first-part in-flight progress between 5 and 85 before completion', async () => {
    const file = new File([new Uint8Array(8)], 'progress.mp4', { type: 'video/mp4', lastModified: 9 });
    const initiated = uploadSession(file, 'initiated');
    const receiving = uploadSession(file, 'receiving');
    const completed = { ...uploadSession(file, 'completed'), file_id: 'file_progress' };
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/api/v1/files/uploads/init')) return envelope(initiated);
      if (url.endsWith('/api/v1/files/uploads/upl_lost_complete/complete')) return envelope(completed);
      throw new Error(`unexpected URL ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    installFakeXhr({
      status: 200,
      response: envelopeBody(receiving),
      progress: [{ loaded: 4, total: 8 }],
    });
    const uploads = new HttpReviewUploads(
      new HttpReviewTransport('edit', new FinalCutReviewClient(BASE_URL), BASE_URL),
    );
    const progress: Array<{ stage: string; percent: number; bytesSent?: number }> = [];

    await uploads.uploadFile(file, context('progress'), (event) => progress.push(event));

    expect(progress).toContainEqual(expect.objectContaining({
      stage: 'uploading',
      percent: 45,
      bytesSent: 4,
    }));
    const inFlightProgress = progress.filter((event) => event.stage === 'uploading' && event.bytesSent === 4);
    expect(inFlightProgress.every((event) => event.percent >= 5 && event.percent <= 85)).toBe(true);
  });

  it('chooses dynamic parts that stay within 8-64 MiB and never exceed 256 parts', async () => {
    const minPartBytes = 8 * 1024 * 1024;
    const maxPartBytes = 64 * 1024 * 1024;
    const sparseFile = fakeSizedFile(minPartBytes * 256 + 1, 'dynamic.mp4');
    const initiated = uploadSession(sparseFile, 'initiated');
    const completed = { ...uploadSession(sparseFile, 'completed'), file_id: 'file_dynamic' };
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/api/v1/files/uploads/init')) return envelope(initiated);
      if (url.endsWith('/api/v1/files/uploads/upl_lost_complete/complete')) return envelope(completed);
      throw new Error(`unexpected URL ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    installFakeXhr();
    const uploads = new HttpReviewUploads(
      new HttpReviewTransport('edit', new FinalCutReviewClient(BASE_URL), BASE_URL),
    );

    await uploads.uploadFile(sparseFile, context('dynamic-parts'));

    expect(FakeXMLHttpRequest.instances).toHaveLength(256);
    const partSizes = FakeXMLHttpRequest.instances.map((xhr) => (xhr.requestBody as Blob).size);
    expect(partSizes[0]).toBeGreaterThanOrEqual(minPartBytes);
    expect(Math.max(...partSizes)).toBeLessThanOrEqual(maxPartBytes);
    expect(FakeXMLHttpRequest.instances.at(-1)?.url.endsWith('/parts/256')).toBe(true);
  });
});

describe('HTTP upload MIME normalization', () => {
  it.each([
    { name: 'clip.mp4', type: '', expected: 'video/mp4' },
    { name: 'clip.m4v', type: 'video/x-m4v', expected: 'video/mp4' },
    { name: 'clip.mov', type: 'application/octet-stream', expected: 'video/quicktime' },
    { name: 'clip.qt', type: 'video/x-quicktime; charset=binary', expected: 'video/quicktime' },
  ])('normalizes $name with $type to $expected', async ({ name, type, expected }) => {
    const file = new File(['mime'], name, { type, lastModified: 1 });
    let initPayload: Record<string, unknown> | undefined;
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      initPayload = JSON.parse(String(init?.body)) as Record<string, unknown>;
      throw new TypeError('stop after init');
    });
    vi.stubGlobal('fetch', fetchMock);
    const uploads = new HttpReviewUploads(
      new HttpReviewTransport('edit', new FinalCutReviewClient(BASE_URL), BASE_URL),
    );

    await expect(uploads.uploadFile(file, context('mime'))).rejects.toThrow('stop after init');
    expect(initPayload?.mime_type).toBe(expected);
  });

  it('rejects an explicit extension/MIME mismatch before init', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const uploads = new HttpReviewUploads(
      new HttpReviewTransport('edit', new FinalCutReviewClient(BASE_URL), BASE_URL),
    );

    await expect(
      uploads.uploadFile(new File(['mismatch'], 'clip.mov', { type: 'video/mp4' }), context('mismatch')),
    ).rejects.toMatchObject({ code: 'FILE_TYPE_NOT_ALLOWED' });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

function context(requestId: string): ExecutionContext {
  return { entryMode: 'edit', requestId, createdAt: '2026-07-14T00:00:00.000Z' };
}

function uploadSession(file: File, status: UploadSessionDTO['status']): UploadSessionDTO {
  return {
    upload_id: 'upl_lost_complete',
    status,
    original_filename: file.name,
    mime_type: file.type,
    declared_size: file.size,
    received_size: status === 'initiated' ? 0 : file.size,
    file_id: null,
  };
}

function reviewItem(currentVersionNo: number): ReviewItemDTO {
  return {
    id: 'item_append_retry',
    project_ref_id: 'prj_append_retry',
    item_code: '01',
    episode_no: 1,
    title: 'Append retry',
    workflow_status: 'changes_requested',
    current_version_id: `ver_v${currentVersionNo}`,
    current_version_no: currentVersionNo,
    ui_status: '待修改',
    active_finalization_id: null,
    unresolved_current_version_count: 0,
    resolved_current_version_count: 0,
    historical_version_count: currentVersionNo - 1,
    is_finalized: false,
    lock_version: 7 + currentVersionNo,
    created_at: '2026-07-14T00:00:00.000Z',
    updated_at: '2026-07-14T00:00:00.000Z',
  };
}

function reviewVersion(versionNo: number, file: File): ReviewVersionDTO {
  const label = `V${versionNo}`;
  return {
    id: `ver_v${versionNo}`,
    project_ref_id: 'prj_append_retry',
    review_item_id: 'item_append_retry',
    previous_version_id: `ver_v${versionNo - 1}`,
    version_no: versionNo,
    version_label: label,
    is_current: true,
    original_media: {
      original_file_id: `file_${label.toLowerCase()}`,
      original_filename: file.name,
      mime_type: file.type,
      file_size: file.size,
      sha256: 'a'.repeat(64),
      duration_ms: 1000,
      width: 1920,
      height: 1080,
      fps_num: 25,
      fps_den: 1,
      media_probe_version: 'test',
    },
    playback_status: 'ready',
    playback_asset_id: `file_${label.toLowerCase()}`,
    thumbnail_asset_id: null,
    version_note: `${label} note`,
    change_summary: `${label} changes`,
    lock_version: 1,
    created_at: '2026-07-14T00:00:00.000Z',
  };
}

function envelope(data: UploadSessionDTO): Response {
  return new Response(
    JSON.stringify(envelopeBody(data)),
    { status: 200, headers: { 'Content-Type': 'application/json' } },
  );
}

function envelopeBody(data: UploadSessionDTO): object {
  return { data, meta: { request_id: 'req-upload', contract_version: '1.0' } };
}

function fakeSizedFile(size: number, name: string): File {
  const file = {
    name,
    size,
    type: 'video/mp4',
    lastModified: 1,
    slice(start = 0, end = size): Blob {
      return { size: Math.max(0, Math.min(end, size) - Math.max(start, 0)) } as Blob;
    },
  };
  return file as File;
}

type FakeXhrTerminal = 'load' | 'error' | 'timeout' | 'none';

interface FakeXhrScenario {
  status?: number;
  response?: unknown;
  responseText?: string;
  terminal?: FakeXhrTerminal;
  progress?: Array<{ loaded: number; total: number; lengthComputable?: boolean }>;
}

type FakeListener = (event: Event | ProgressEvent) => void;

class FakeUploadTarget {
  readonly listeners = new Map<string, FakeListener[]>();

  constructor(private readonly order: string[]) {}

  addEventListener(type: string, listener: EventListenerOrEventListenerObject): void {
    this.order.push(`upload-listener:${type}`);
    const callback = typeof listener === 'function'
      ? listener as FakeListener
      : (event: Event | ProgressEvent) => listener.handleEvent(event as Event);
    const listeners = this.listeners.get(type) ?? [];
    listeners.push(callback);
    this.listeners.set(type, listeners);
  }

  emitProgress(loaded: number, total: number, lengthComputable = true): void {
    const event = { loaded, total, lengthComputable } as ProgressEvent;
    for (const listener of this.listeners.get('progress') ?? []) listener(event);
  }
}

class FakeXMLHttpRequest {
  static readonly instances: FakeXMLHttpRequest[] = [];
  static readonly scenarios: FakeXhrScenario[] = [];

  static reset(): void {
    this.instances.length = 0;
    this.scenarios.length = 0;
  }

  readonly order: string[] = [];
  readonly upload = new FakeUploadTarget(this.order);
  readonly listeners = new Map<string, FakeListener[]>();
  readonly requestHeaders = new Map<string, string>();
  method = '';
  url = '';
  async = true;
  withCredentials = false;
  status = 0;
  responseText = '';
  requestBody: Document | XMLHttpRequestBodyInit | null = null;
  aborted = false;
  abortCalls = 0;

  constructor() {
    FakeXMLHttpRequest.instances.push(this);
  }

  addEventListener(type: string, listener: EventListenerOrEventListenerObject): void {
    this.order.push(`xhr-listener:${type}`);
    const callback = typeof listener === 'function'
      ? listener as FakeListener
      : (event: Event | ProgressEvent) => listener.handleEvent(event as Event);
    const listeners = this.listeners.get(type) ?? [];
    listeners.push(callback);
    this.listeners.set(type, listeners);
  }

  open(method: string, url: string | URL, async = true): void {
    this.order.push('open');
    this.method = method;
    this.url = String(url);
    this.async = async;
  }

  setRequestHeader(name: string, value: string): void {
    this.order.push(`header:${name}`);
    this.requestHeaders.set(name.toLowerCase(), value);
  }

  send(body: Document | XMLHttpRequestBodyInit | null = null): void {
    this.order.push('send');
    this.requestBody = body;
    const scenario = FakeXMLHttpRequest.scenarios.shift() ?? {
      status: 200,
      response: envelopeBody({
        upload_id: 'upl_lost_complete',
        status: 'receiving',
        original_filename: 'default.mp4',
        mime_type: 'video/mp4',
        declared_size: body instanceof Blob ? body.size : 0,
        received_size: body instanceof Blob ? body.size : 0,
        file_id: null,
      }),
    };
    for (const progress of scenario.progress ?? []) {
      this.upload.emitProgress(progress.loaded, progress.total, progress.lengthComputable ?? true);
    }
    if ((scenario.terminal ?? 'load') === 'none') return;
    this.status = scenario.status ?? 200;
    this.responseText = scenario.responseText ?? JSON.stringify(scenario.response);
    this.emit(scenario.terminal ?? 'load');
  }

  abort(): void {
    this.order.push('abort');
    this.aborted = true;
    this.abortCalls += 1;
    this.emit('abort');
  }

  emit(type: string): void {
    const event = { type } as Event;
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }
}

function installFakeXhr(...scenarios: FakeXhrScenario[]): void {
  FakeXMLHttpRequest.reset();
  FakeXMLHttpRequest.scenarios.push(...scenarios);
  vi.stubGlobal('XMLHttpRequest', FakeXMLHttpRequest as unknown as typeof XMLHttpRequest);
}

function blobBytes(blob: Blob): Promise<Uint8Array> {
  return new Promise<Uint8Array>((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener('load', () => {
      if (reader.result instanceof ArrayBuffer) resolve(new Uint8Array(reader.result));
      else reject(new Error('blob read failed'));
    });
    reader.addEventListener('error', () => reject(reader.error ?? new Error('blob read failed')));
    reader.readAsArrayBuffer(blob);
  });
}
