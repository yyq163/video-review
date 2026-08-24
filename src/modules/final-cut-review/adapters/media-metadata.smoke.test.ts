import { openAsBlob } from 'node:fs';
import { createRequire } from 'node:module';

import mediaInfoFactory from 'mediainfo.js';
import { expect, it } from 'vitest';

import { BrowserMediaMetadataAdapter, type MediaInfoFactoryPort } from './media-metadata';

const realMediaPath = process.env.FCR_REAL_MEDIA_SMOKE_PATH;
const realSmoke = realMediaPath ? it : it.skip;

realSmoke('incrementally probes an authorized real media file without whole-blob arrayBuffer', async () => {
  const source = await openAsBlob(realMediaPath!);
  let sliceCalls = 0;
  const incrementalBlob = {
    size: source.size,
    slice(start?: number, end?: number, contentType?: string) {
      sliceCalls += 1;
      return source.slice(start, end, contentType);
    },
    async arrayBuffer(): Promise<ArrayBuffer> {
      throw new Error('whole-blob arrayBuffer must never be called');
    },
  } as Blob;
  const wasmPath = createRequire(import.meta.url).resolve('mediainfo.js/MediaInfoModule.wasm');
  const factory: MediaInfoFactoryPort = (options) => mediaInfoFactory({
    ...options,
    locateFile: () => wasmPath,
  });
  const adapter = new BrowserMediaMetadataAdapter({ factory });

  const metadata = await adapter.probe(incrementalBlob, {
    expectedFrameRate: { fpsNum: 30, fpsDen: 1 },
  });

  expect(metadata).toMatchObject({
    fpsNum: 30,
    fpsDen: 1,
    isVariableFrameRate: false,
    width: 2160,
    height: 3840,
  });
  expect(metadata.durationMs).toBeGreaterThan(60_000);
  expect(metadata.bitrateBps).toBeGreaterThan(0);
  expect(sliceCalls).toBeGreaterThan(0);
});
