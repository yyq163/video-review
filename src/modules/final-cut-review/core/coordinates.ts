import type { NormalizedBounds, NormalizedPoint } from '../contracts/types';

export interface VideoRectInput {
  containerWidth: number;
  containerHeight: number;
  videoWidth: number;
  videoHeight: number;
  maxScale?: number;
}

export interface ContainedVideoRect {
  x: number;
  y: number;
  width: number;
  height: number;
  scale: number;
}

export function computeContainedVideoRect(input: VideoRectInput): ContainedVideoRect {
  const containerWidth = Math.max(0, input.containerWidth);
  const containerHeight = Math.max(0, input.containerHeight);
  const videoWidth = Math.max(1, input.videoWidth);
  const videoHeight = Math.max(1, input.videoHeight);
  const scaleLimit = Number.isFinite(input.maxScale) && input.maxScale !== undefined ? Math.max(0, input.maxScale) : Number.POSITIVE_INFINITY;
  const scale = Math.min(containerWidth / videoWidth, containerHeight / videoHeight, scaleLimit);
  const width = videoWidth * scale;
  const height = videoHeight * scale;
  return {
    x: (containerWidth - width) / 2,
    y: (containerHeight - height) / 2,
    width,
    height,
    scale,
  };
}

export function clampNormalizedPoint(point: NormalizedPoint): NormalizedPoint {
  return {
    x: Math.min(1, Math.max(0, point.x)),
    y: Math.min(1, Math.max(0, point.y)),
  };
}

export function pointerToNormalizedVideoPoint(input: {
  clientX: number;
  clientY: number;
  containerRect: DOMRect | Pick<DOMRect, 'left' | 'top' | 'width' | 'height'>;
  videoWidth: number;
  videoHeight: number;
  maxScale?: number;
}): NormalizedPoint | null {
  const videoRect = computeContainedVideoRect({
    containerWidth: input.containerRect.width,
    containerHeight: input.containerRect.height,
    videoWidth: input.videoWidth,
    videoHeight: input.videoHeight,
    maxScale: input.maxScale,
  });
  const localX = input.clientX - input.containerRect.left - videoRect.x;
  const localY = input.clientY - input.containerRect.top - videoRect.y;
  if (localX < 0 || localY < 0 || localX > videoRect.width || localY > videoRect.height) {
    return null;
  }
  return clampNormalizedPoint({
    x: localX / videoRect.width,
    y: localY / videoRect.height,
  });
}

export function normalizedVideoPointToCanvasPoint(input: {
  point: NormalizedPoint;
  canvasWidth: number;
  canvasHeight: number;
}): NormalizedPoint {
  const clamped = clampNormalizedPoint(input.point);
  return {
    x: clamped.x * input.canvasWidth,
    y: clamped.y * input.canvasHeight,
  };
}

export function normalizedPathToCanvasPath(input: {
  points: NormalizedPoint[];
  canvasWidth: number;
  canvasHeight: number;
}): NormalizedPoint[] {
  return input.points.map((point) =>
    normalizedVideoPointToCanvasPoint({
      point,
      canvasWidth: input.canvasWidth,
      canvasHeight: input.canvasHeight,
    }),
  );
}

export function boundsFromPoints(start: NormalizedPoint, end: NormalizedPoint): NormalizedBounds {
  const first = clampNormalizedPoint(start);
  const second = clampNormalizedPoint(end);
  return {
    x: Math.min(first.x, second.x),
    y: Math.min(first.y, second.y),
    width: Math.abs(second.x - first.x),
    height: Math.abs(second.y - first.y),
  };
}
