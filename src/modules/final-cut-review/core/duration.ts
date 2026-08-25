import type { ReviewFrameRate } from '../contracts/types';
import { formatTimestampTimecode } from './timecode';

export type ReviewDurationBandClass = 'is-short' | 'is-warning' | 'is-standard';

function normalizedDurationMs(durationMs: number): number {
  return Number.isFinite(durationMs) ? Math.max(0, durationMs) : 0;
}

export function floorReviewDurationSeconds(durationMs: number): number {
  return Math.floor(normalizedDurationMs(durationMs) / 1000);
}

export function reviewDurationBandClass(durationMs: number): ReviewDurationBandClass {
  const seconds = floorReviewDurationSeconds(durationMs);
  if (seconds < 51) return 'is-short';
  if (seconds < 61) return 'is-warning';
  return 'is-standard';
}

export function formatReviewDurationSeconds(durationMs: number): string {
  return `${floorReviewDurationSeconds(durationMs)} 秒`;
}

export function formatReviewDurationTimecode(
  durationMs: number,
  frameRate: ReviewFrameRate,
): string {
  return formatTimestampTimecode(
    normalizedDurationMs(durationMs),
    frameRate.fpsNum,
    frameRate.fpsDen,
  );
}
