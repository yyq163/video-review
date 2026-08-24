import type { MediaInfoResult } from 'mediainfo.js';
import { describe, expect, it, vi } from 'vitest';

import {
  BrowserMediaMetadataAdapter,
  MediaMetadataError,
  type MediaInfoFactoryPort,
} from './media-metadata';

const cfrResult = (overrides: Record<string, unknown> = {}): MediaInfoResult => ({
  media: {
    '@ref': 'controlled.mp4',
    track: [
      {
        '@type': 'General',
        Duration: 60.5,
        OverallBitRate: 8_000_000,
      },
      {
        '@type': 'Video',
        Width: 1920,
        Height: 1080,
        Duration: 60.5,
        FrameRate_Mode: 'CFR',
        FrameRate_Num: 30_000,
        FrameRate_Den: 1_001,
        ...overrides,
      },
    ],
  },
});

function controlledBlob(size = 16) {
  const wholeArrayBuffer = vi.fn(async () => {
    throw new Error('whole-file arrayBuffer must never be called');
  });
  const slice = vi.fn((start = 0, end = size) => ({
    arrayBuffer: vi.fn(async () => Uint8Array.from(
      { length: Math.max(0, end - start) },
      (_value, index) => (start + index) % 256,
    ).buffer),
  }));
  return {
    blob: {
      size,
      slice,
      arrayBuffer: wholeArrayBuffer,
    } as unknown as Blob,
    slice,
    wholeArrayBuffer,
  };
}

function factoryFor(
  result: MediaInfoResult,
  inspect?: (size: number, readChunk: (size: number, offset: number) => Promise<Uint8Array>) => Promise<void>,
) {
  const close = vi.fn();
  const factory: MediaInfoFactoryPort = vi.fn(async () => ({
    close,
    analyzeData: async (
      size: number,
      readChunk: (size: number, offset: number) => Promise<Uint8Array>,
    ) => {
      await inspect?.(Number(size), readChunk);
      return result;
    },
  }));
  return { factory, close };
}

describe('BrowserMediaMetadataAdapter', () => {
  it('reads only bounded Blob.slice chunks and returns exact allowed CFR metadata', async () => {
    const source = controlledBlob(16);
    const { factory, close } = factoryFor(cfrResult(), async (size, readChunk) => {
      expect(size).toBe(16);
      expect([...await readChunk(8, 0)]).toEqual([0, 1, 2, 3]);
      expect([...await readChunk(8, 8)]).toEqual([8, 9, 10, 11]);
    });
    const adapter = new BrowserMediaMetadataAdapter({
      factory,
      maxChunkBytes: 4,
      maxReadBytes: 16,
    });

    await expect(adapter.probe(source.blob, {
      expectedFrameRate: { fpsNum: 30_000, fpsDen: 1_001 },
    })).resolves.toEqual({
      durationMs: 60_500,
      width: 1920,
      height: 1080,
      bitrateBps: 8_000_000,
      fpsNum: 30_000,
      fpsDen: 1_001,
      isVariableFrameRate: false,
      probeVersion: 'mediainfo.js-0.3.7',
    });
    expect(source.slice.mock.calls).toEqual([[0, 4], [8, 12]]);
    expect(source.wholeArrayBuffer).not.toHaveBeenCalled();
    expect(close).toHaveBeenCalledOnce();
  });

  it.each([
    [cfrResult({ FrameRate_Mode: 'VFR' }), 'MEDIA_VARIABLE_FRAME_RATE'],
    [cfrResult({ FrameRate_Mode: undefined }), 'MEDIA_FRAME_RATE_UNKNOWN'],
    [cfrResult({ FrameRate_Num: 48, FrameRate_Den: 1 }), 'MEDIA_FRAME_RATE_NOT_ALLOWED'],
    [
      {
        media: {
          '@ref': 'controlled.mp4',
          track: [
            { '@type': 'General', Duration: 1_000 },
            { '@type': 'Video', Width: 1, Height: 1, FrameRate_Mode: 'CFR', FrameRate_Num: 25, FrameRate_Den: 1 },
            { '@type': 'Video', Width: 1, Height: 1, FrameRate_Mode: 'CFR', FrameRate_Num: 25, FrameRate_Den: 1 },
          ],
        },
      } satisfies MediaInfoResult,
      'MEDIA_VIDEO_STREAM_COUNT_INVALID',
    ],
  ])('fails closed for VFR, unknown, non-allowed and multi-video results', async (result, code) => {
    const { factory } = factoryFor(result as MediaInfoResult);
    const adapter = new BrowserMediaMetadataAdapter({ factory });
    const source = controlledBlob();
    await expect(adapter.probe(source.blob)).rejects.toMatchObject({ code });
  });

  it('reports a project frame-rate mismatch explicitly', async () => {
    const { factory } = factoryFor(cfrResult());
    const adapter = new BrowserMediaMetadataAdapter({ factory });
    await expect(adapter.probe(controlledBlob().blob, {
      expectedFrameRate: { fpsNum: 25, fpsDen: 1 },
    })).rejects.toMatchObject({ code: 'MEDIA_FRAME_RATE_MISMATCH' });
  });

  it('enforces file/read budgets, abort and timeout while always closing initialized WASM', async () => {
    const neverFactory = vi.fn(async () => ({
      close: vi.fn(),
      analyzeData: () => new Promise<MediaInfoResult>(() => undefined),
    })) satisfies MediaInfoFactoryPort;
    const timeoutAdapter = new BrowserMediaMetadataAdapter({ factory: neverFactory, timeoutMs: 5 });
    await expect(timeoutAdapter.probe(controlledBlob().blob)).rejects.toMatchObject({
      code: 'MEDIA_METADATA_TIMEOUT',
    });
    const timedOutInstance = await neverFactory.mock.results[0]?.value;
    expect(timedOutInstance?.close).toHaveBeenCalledOnce();

    const readBudget = factoryFor(cfrResult(), async (_size, readChunk) => {
      await readChunk(8, 0);
      await readChunk(8, 8);
    });
    const budgetAdapter = new BrowserMediaMetadataAdapter({
      factory: readBudget.factory,
      maxChunkBytes: 8,
      maxReadBytes: 12,
    });
    await expect(budgetAdapter.probe(controlledBlob().blob)).rejects.toMatchObject({
      code: 'MEDIA_METADATA_RESOURCE_LIMIT',
    });
    expect(readBudget.close).toHaveBeenCalledOnce();

    const oversizedFactory = factoryFor(cfrResult());
    const oversizedAdapter = new BrowserMediaMetadataAdapter({
      factory: oversizedFactory.factory,
      maxFileBytes: 8,
    });
    await expect(oversizedAdapter.probe(controlledBlob(16).blob)).rejects.toMatchObject({
      code: 'MEDIA_METADATA_RESOURCE_LIMIT',
    });
    expect(oversizedFactory.factory).not.toHaveBeenCalled();

    const controller = new AbortController();
    controller.abort();
    await expect(oversizedAdapter.probe(controlledBlob(4).blob, {
      signal: controller.signal,
    })).rejects.toBeInstanceOf(MediaMetadataError);
    await expect(oversizedAdapter.probe(controlledBlob(4).blob, {
      signal: controller.signal,
    })).rejects.toMatchObject({ code: 'MEDIA_METADATA_ABORTED' });
  });

  it('closes a WASM instance that resolves after factory initialization timed out', async () => {
    const close = vi.fn();
    const delayedFactory: MediaInfoFactoryPort = () => new Promise((resolve) => {
      setTimeout(() => resolve({
        close,
        analyzeData: async () => cfrResult(),
      }), 20);
    });
    const adapter = new BrowserMediaMetadataAdapter({ factory: delayedFactory, timeoutMs: 5 });

    await expect(adapter.probe(controlledBlob().blob)).rejects.toMatchObject({
      code: 'MEDIA_METADATA_TIMEOUT',
    });
    await new Promise((resolve) => setTimeout(resolve, 30));

    expect(close).toHaveBeenCalledOnce();
  });
});
