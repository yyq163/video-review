import type { MediaMetadataPort, MediaMetadataProbeOptions } from '../ports';

export class MockMediaMetadataAdapter implements MediaMetadataPort {
  async probe(blob: Blob, options: MediaMetadataProbeOptions = {}) {
    if (options.signal?.aborted) throw new DOMException('媒体预检已取消', 'AbortError');
    const frameRate = options.expectedFrameRate ?? { fpsNum: 25, fpsDen: 1 };
    return {
      durationMs: 60_500,
      width: 1920,
      height: 1080,
      bitrateBps: Math.max(1, blob.size * 8 * 1000 / 60_500),
      fpsNum: frameRate.fpsNum,
      fpsDen: frameRate.fpsDen,
      isVariableFrameRate: false as const,
      probeVersion: 'mediainfo.js-0.3.7' as const,
    };
  }
}
