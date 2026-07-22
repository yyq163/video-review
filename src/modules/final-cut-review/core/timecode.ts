import { z } from 'zod';
import type { ReviewFrameRate } from '../contracts/types';

const timecodeSchema = z.string().regex(/^\d{2}:\d{2}:\d{2}:\d{2}$/, '时间码格式必须为 HH:MM:SS:FF');

export function assertValidFrameRate(fpsNum: number, fpsDen: number): void {
  if (!Number.isInteger(fpsNum) || !Number.isInteger(fpsDen) || fpsNum <= 0 || fpsDen <= 0) {
    throw new Error('帧率必须使用正整数 fpsNum/fpsDen');
  }
}

export function frameRateToFps({ fpsNum, fpsDen }: ReviewFrameRate): number {
  assertValidFrameRate(fpsNum, fpsDen);
  return fpsNum / fpsDen;
}

export function frameFromTimestampMs(timestampMs: number, fpsNum: number, fpsDen: number): number {
  assertValidFrameRate(fpsNum, fpsDen);
  return Math.max(0, Math.floor((Math.max(0, timestampMs) * fpsNum) / (1000 * fpsDen)));
}

export function timestampMsFromFrame(frameNumber: number, fpsNum: number, fpsDen: number): number {
  assertValidFrameRate(fpsNum, fpsDen);
  return Math.max(0, Math.floor((Math.max(0, frameNumber) * 1000 * fpsDen) / fpsNum));
}

export function formatReviewTimecode(frameNumber: number, fpsNum: number, fpsDen: number): string {
  assertValidFrameRate(fpsNum, fpsDen);
  const fpsForDisplay = Math.round(fpsNum / fpsDen);
  const safeFrame = Math.max(0, Math.floor(frameNumber));
  const totalSeconds = Math.floor(safeFrame / fpsForDisplay);
  const frame = safeFrame % fpsForDisplay;
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const parts = [hours, minutes, seconds].map((part) => part.toString().padStart(2, '0'));
  return `${parts.join(':')}:${Math.min(frame, fpsForDisplay - 1).toString().padStart(2, '0')}`;
}

export function formatTimestampTimecode(timestampMs: number, fpsNum: number, fpsDen: number): string {
  return formatReviewTimecode(frameFromTimestampMs(timestampMs, fpsNum, fpsDen), fpsNum, fpsDen);
}

export function parseReviewTimecode(input: string, fpsNum: number, fpsDen: number): number {
  assertValidFrameRate(fpsNum, fpsDen);
  const value = timecodeSchema.parse(input.trim());
  const [hours, minutes, seconds, frame] = value.split(':').map(Number);
  const fpsForDisplay = Math.ceil(fpsNum / fpsDen);
  if (minutes > 59 || seconds > 59 || frame >= fpsForDisplay) {
    throw new Error(`时间码帧号必须小于 ${fpsForDisplay}`);
  }
  const baseSeconds = hours * 3600 + minutes * 60 + seconds;
  return Math.floor((baseSeconds * fpsNum) / fpsDen) + frame;
}

export function validatePlaybackTargetDrift(input: {
  timestampMs: number;
  frameNumber: number;
  fpsNum: number;
  fpsDen: number;
}): void {
  const expectedFrame = frameFromTimestampMs(input.timestampMs, input.fpsNum, input.fpsDen);
  if (Math.abs(expectedFrame - input.frameNumber) > 1) {
    throw new Error('意见时间码与帧号偏差超过一个审阅帧');
  }
}

export function clampTimeMs(timeMs: number, durationMs: number): number {
  return Math.min(Math.max(0, timeMs), Math.max(0, durationMs));
}

export function clampFrame(frameNumber: number, durationMs: number, fpsNum: number, fpsDen: number): number {
  const maxFrame = frameFromTimestampMs(durationMs, fpsNum, fpsDen);
  return Math.min(Math.max(0, Math.floor(frameNumber)), maxFrame);
}
