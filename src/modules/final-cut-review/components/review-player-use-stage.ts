import { useLayoutEffect, useState, type CSSProperties, type RefObject } from 'react';
import type { ReviewVersion } from '../contracts/types';
import { computeContainedVideoRect } from '../core/coordinates';
import type { PlayerDisplayMode } from './review-player-types';

export function useReviewPlayerStage(options: {
  stageRef: RefObject<HTMLDivElement | null>;
  version: ReviewVersion;
  loadedMediaDimensions: { versionId: string; width: number; height: number } | null;
}) {
  const { stageRef, version, loadedMediaDimensions } = options;
  const [displayMode, setDisplayMode] = useState<PlayerDisplayMode>('fit');
  const [stageSize, setStageSize] = useState({ width: version.width, height: version.height });
  const activeMediaDimensions = loadedMediaDimensions?.versionId === version.versionId ? loadedMediaDimensions : version;
  const displayVideoWidth = Math.max(1, activeMediaDimensions.width || version.width);
  const displayVideoHeight = Math.max(1, activeMediaDimensions.height || version.height);
  const displayMaxScale = displayMode === 'original-ratio' ? 1 : Number.POSITIVE_INFINITY;
  const videoRect = computeContainedVideoRect({
    containerWidth: stageSize.width,
    containerHeight: stageSize.height,
    videoWidth: displayVideoWidth,
    videoHeight: displayVideoHeight,
    maxScale: displayMaxScale,
  });
  const containedMediaStyle: CSSProperties = {
    bottom: 'auto',
    height: `${videoRect.height}px`,
    left: `${videoRect.x}px`,
    right: 'auto',
    top: `${videoRect.y}px`,
    width: `${videoRect.width}px`,
  };

  useLayoutEffect(() => {
    const node = stageRef.current;
    if (!node) return undefined;
    const updateStageSize = () => {
      const rect = node.getBoundingClientRect();
      setStageSize({ width: Math.max(1, rect.width), height: Math.max(1, rect.height) });
    };
    updateStageSize();
    if (typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(updateStageSize);
    observer.observe(node);
    return () => observer.disconnect();
  }, [displayVideoHeight, displayVideoWidth, stageRef]);

  return {
    annotationLayerStyle: containedMediaStyle,
    containedMediaStyle,
    displayMaxScale,
    displayMode,
    displayVideoHeight,
    displayVideoWidth,
    setDisplayMode,
    videoRect,
  };
}
