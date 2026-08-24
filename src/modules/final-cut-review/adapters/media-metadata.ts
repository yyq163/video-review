import mediaInfoFactory, { type MediaInfoResult } from 'mediainfo.js';
import mediaInfoWasmUrl from 'mediainfo.js/MediaInfoModule.wasm?url';

import type {
  MediaMetadata,
  MediaMetadataPort,
  MediaMetadataProbeOptions,
} from '../ports';

const ALLOWED_FRAME_RATES = new Set([
  '24000/1001',
  '24/1',
  '25/1',
  '30000/1001',
  '30/1',
  '50/1',
  '60000/1001',
  '60/1',
]);
const DEFAULT_MAX_FILE_BYTES = 100 * 1024 * 1024 * 1024;
const DEFAULT_MAX_CHUNK_BYTES = 1024 * 1024;
const DEFAULT_MAX_READ_BYTES = 64 * 1024 * 1024;
const DEFAULT_TIMEOUT_MS = 15_000;

export type MediaMetadataErrorCode =
  | 'MEDIA_METADATA_ABORTED'
  | 'MEDIA_METADATA_TIMEOUT'
  | 'MEDIA_METADATA_RESOURCE_LIMIT'
  | 'MEDIA_METADATA_INVALID'
  | 'MEDIA_VIDEO_STREAM_COUNT_INVALID'
  | 'MEDIA_FRAME_RATE_UNKNOWN'
  | 'MEDIA_VARIABLE_FRAME_RATE'
  | 'MEDIA_FRAME_RATE_NOT_ALLOWED'
  | 'MEDIA_FRAME_RATE_MISMATCH';

export class MediaMetadataError extends Error {
  readonly code: MediaMetadataErrorCode;

  constructor(code: MediaMetadataErrorCode, message: string) {
    super(message);
    this.name = 'MediaMetadataError';
    this.code = code;
  }
}

export interface MediaInfoAnalyzerPort {
  analyzeData(
    size: number,
    readChunk: (size: number, offset: number) => Promise<Uint8Array>,
  ): Promise<MediaInfoResult>;
  close(): void;
}

export type MediaInfoFactoryPort = (options: {
  format: 'object';
  chunkSize: number;
  locateFile(path: string, prefix: string): string;
}) => Promise<MediaInfoAnalyzerPort>;

export interface BrowserMediaMetadataAdapterOptions {
  factory?: MediaInfoFactoryPort;
  maxFileBytes?: number;
  maxChunkBytes?: number;
  maxReadBytes?: number;
  timeoutMs?: number;
}

function positiveBoundedInteger(value: number | undefined): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0 ? value : null;
}

function positiveFinite(value: number | undefined): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : null;
}

function greatestCommonDivisor(left: number, right: number): number {
  let a = Math.abs(left);
  let b = Math.abs(right);
  while (b !== 0) {
    [a, b] = [b, a % b];
  }
  return a;
}

function normalizedFrameRate(numerator: number | undefined, denominator: number | undefined) {
  const fpsNum = positiveBoundedInteger(numerator);
  const fpsDen = positiveBoundedInteger(denominator);
  if (fpsNum === null || fpsDen === null) {
    throw new MediaMetadataError('MEDIA_FRAME_RATE_UNKNOWN', '无法识别精确视频帧率');
  }
  const divisor = greatestCommonDivisor(fpsNum, fpsDen);
  return { fpsNum: fpsNum / divisor, fpsDen: fpsDen / divisor };
}

function parseResult(
  result: MediaInfoResult,
  options: MediaMetadataProbeOptions,
): MediaMetadata {
  const tracks = result.media?.track;
  if (!tracks) {
    throw new MediaMetadataError('MEDIA_METADATA_INVALID', '媒体元数据结构无效');
  }
  const videos = tracks.filter((track) => track['@type'] === 'Video');
  if (videos.length !== 1) {
    throw new MediaMetadataError('MEDIA_VIDEO_STREAM_COUNT_INVALID', '视频必须且只能包含一条视频流');
  }
  const video = videos[0];
  if (!video || video['@type'] !== 'Video') {
    throw new MediaMetadataError('MEDIA_VIDEO_STREAM_COUNT_INVALID', '视频流结构无效');
  }
  if (video.FrameRate_Mode === 'VFR') {
    throw new MediaMetadataError('MEDIA_VARIABLE_FRAME_RATE', '不允许可变帧率视频');
  }
  if (video.FrameRate_Mode !== 'CFR') {
    throw new MediaMetadataError('MEDIA_FRAME_RATE_UNKNOWN', '无法确认视频为固定帧率');
  }
  const frameRate = normalizedFrameRate(video.FrameRate_Num, video.FrameRate_Den);
  if (!ALLOWED_FRAME_RATES.has(`${frameRate.fpsNum}/${frameRate.fpsDen}`)) {
    throw new MediaMetadataError('MEDIA_FRAME_RATE_NOT_ALLOWED', '视频帧率不在允许集合中');
  }
  if (
    options.expectedFrameRate
    && (
      frameRate.fpsNum !== options.expectedFrameRate.fpsNum
      || frameRate.fpsDen !== options.expectedFrameRate.fpsDen
    )
  ) {
    throw new MediaMetadataError('MEDIA_FRAME_RATE_MISMATCH', '视频帧率与项目帧率不匹配');
  }

  const general = tracks.find((track) => track['@type'] === 'General');
  const durationSeconds = positiveFinite(video.Duration)
    ?? (general?.['@type'] === 'General' ? positiveFinite(general.Duration) : null);
  const width = positiveBoundedInteger(video.Width);
  const height = positiveBoundedInteger(video.Height);
  const bitrateBps = general?.['@type'] === 'General'
    ? positiveFinite(general.OverallBitRate)
    : null;
  if (durationSeconds === null || width === null || height === null || bitrateBps === null) {
    throw new MediaMetadataError('MEDIA_METADATA_INVALID', '视频尺寸、时长或码率元数据无效');
  }
  const durationMs = Math.round(durationSeconds * 1000);
  if (!Number.isSafeInteger(durationMs) || durationMs <= 0) {
    throw new MediaMetadataError('MEDIA_METADATA_INVALID', '视频时长超出可安全表示范围');
  }
  return {
    durationMs,
    width,
    height,
    bitrateBps,
    ...frameRate,
    isVariableFrameRate: false,
    probeVersion: 'mediainfo.js-0.3.7',
  };
}

function validateLimit(value: number, label: string): number {
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw new TypeError(`${label} must be a positive safe integer`);
  }
  return value;
}

export class BrowserMediaMetadataAdapter implements MediaMetadataPort {
  private readonly factory: MediaInfoFactoryPort;
  private readonly maxFileBytes: number;
  private readonly maxChunkBytes: number;
  private readonly maxReadBytes: number;
  private readonly timeoutMs: number;

  constructor(options: BrowserMediaMetadataAdapterOptions = {}) {
    this.factory = options.factory ?? ((factoryOptions) => mediaInfoFactory(factoryOptions));
    this.maxFileBytes = validateLimit(options.maxFileBytes ?? DEFAULT_MAX_FILE_BYTES, 'maxFileBytes');
    this.maxChunkBytes = validateLimit(options.maxChunkBytes ?? DEFAULT_MAX_CHUNK_BYTES, 'maxChunkBytes');
    this.maxReadBytes = validateLimit(options.maxReadBytes ?? DEFAULT_MAX_READ_BYTES, 'maxReadBytes');
    this.timeoutMs = validateLimit(options.timeoutMs ?? DEFAULT_TIMEOUT_MS, 'timeoutMs');
  }

  async probe(blob: Blob, options: MediaMetadataProbeOptions = {}): Promise<MediaMetadata> {
    if (!Number.isSafeInteger(blob.size) || blob.size <= 0 || blob.size > this.maxFileBytes) {
      throw new MediaMetadataError('MEDIA_METADATA_RESOURCE_LIMIT', '视频文件超出本地预检资源上限');
    }
    if (options.signal?.aborted) {
      throw new MediaMetadataError('MEDIA_METADATA_ABORTED', '媒体预检已取消');
    }

    let analyzer: MediaInfoAnalyzerPort | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let abortListener: (() => void) | undefined;
    const timeout = new Promise<never>((_resolve, reject) => {
      timer = setTimeout(
        () => reject(new MediaMetadataError('MEDIA_METADATA_TIMEOUT', '媒体预检超时')),
        this.timeoutMs,
      );
    });
    const aborted = new Promise<never>((_resolve, reject) => {
      abortListener = () => reject(new MediaMetadataError('MEDIA_METADATA_ABORTED', '媒体预检已取消'));
      options.signal?.addEventListener('abort', abortListener, { once: true });
    });
    const controlled = <T>(promise: Promise<T>) => Promise.race([promise, timeout, aborted]);

    try {
      const analyzerPromise = this.factory({
        format: 'object',
        chunkSize: this.maxChunkBytes,
        locateFile: () => mediaInfoWasmUrl,
      });
      try {
        analyzer = await controlled(analyzerPromise);
      } catch (error) {
        void analyzerPromise.then(
          (lateAnalyzer) => lateAnalyzer.close(),
          () => undefined,
        );
        throw error;
      }
      let bytesRead = 0;
      const result = await controlled(analyzer.analyzeData(blob.size, async (requestedSize, offset) => {
        if (options.signal?.aborted) {
          throw new MediaMetadataError('MEDIA_METADATA_ABORTED', '媒体预检已取消');
        }
        if (
          !Number.isSafeInteger(requestedSize)
          || !Number.isSafeInteger(offset)
          || requestedSize <= 0
          || offset < 0
          || offset >= blob.size
        ) {
          throw new MediaMetadataError('MEDIA_METADATA_INVALID', '媒体解析器请求了无效文件范围');
        }
        const boundedSize = Math.min(requestedSize, this.maxChunkBytes, blob.size - offset);
        if (bytesRead + boundedSize > this.maxReadBytes) {
          throw new MediaMetadataError('MEDIA_METADATA_RESOURCE_LIMIT', '媒体预检超出累计读取上限');
        }
        bytesRead += boundedSize;
        const buffer = await blob.slice(offset, offset + boundedSize).arrayBuffer();
        if (buffer.byteLength !== boundedSize) {
          throw new MediaMetadataError('MEDIA_METADATA_INVALID', '媒体分块读取长度不一致');
        }
        return new Uint8Array(buffer);
      }));
      return parseResult(result, options);
    } finally {
      if (timer !== undefined) clearTimeout(timer);
      if (abortListener) options.signal?.removeEventListener('abort', abortListener);
      analyzer?.close();
    }
  }
}
