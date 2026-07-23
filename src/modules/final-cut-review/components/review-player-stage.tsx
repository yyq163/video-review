import { useRef, type CSSProperties, type PointerEventHandler, type RefCallback } from 'react';
import type { ReviewAnnotationSet, ReviewAnnotationShape, ReviewVersion } from '../contracts/types';
import type { ContainedVideoRect } from '../core/coordinates';
import { DraftAnnotationLayer, SavedAnnotationLayer } from './review-player-annotation-layers';
import { shapeFontSize } from './review-player-annotation-utils';
import type { PlayerMediaState } from './review-player-types';

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function normalizedCoordinate(value: number): number {
  return Number.isFinite(value) ? clamp(value, 0, 1) : 0;
}

function createTextEditorStyle(
  shape: ReviewAnnotationShape,
  videoRect: ContainedVideoRect,
  displayVideoWidth: number,
): CSSProperties | undefined {
  const point = shape.points?.[0];
  const frameWidth = Number.isFinite(videoRect.width) ? Math.max(0, videoRect.width) : 0;
  const frameHeight = Number.isFinite(videoRect.height) ? Math.max(0, videoRect.height) : 0;
  if (!point || frameWidth === 0 || frameHeight === 0) return undefined;

  const intrinsicWidth = Number.isFinite(displayVideoWidth) ? Math.max(1, displayVideoWidth) : 1;
  const scale = frameWidth / intrinsicWidth;
  const maxFontSize = Math.max(1, (frameHeight - 6) / 1.2);
  const fontSize = Math.min(maxFontSize, Math.max(14, shapeFontSize(shape) * scale));
  const desiredWidth = clamp((shape.text?.length ?? 4) * fontSize * 0.68 + 20, 120, 260);
  const width = Math.min(frameWidth, desiredWidth);
  const height = Math.min(frameHeight, Math.max(34, fontSize * 1.2 + 6));
  const maxLeft = Math.max(0, frameWidth - width);
  const maxTop = Math.max(0, frameHeight - height);

  return {
    color: shape.color,
    fontSize: `${fontSize}px`,
    height: `${height}px`,
    left: `${clamp(normalizedCoordinate(point.x) * frameWidth, 0, maxLeft)}px`,
    top: `${clamp(normalizedCoordinate(point.y) * frameHeight - height * 0.85, 0, maxTop)}px`,
    width: `${width}px`,
  };
}

interface ReviewPlayerStageProps {
  version: ReviewVersion;
  videoRef: RefCallback<HTMLVideoElement>;
  stageRef: RefCallback<HTMLDivElement>;
  textInputRef: RefCallback<HTMLInputElement>;
  annotationReadonly: boolean;
  muted: boolean;
  mediaState: PlayerMediaState;
  containedMediaStyle: CSSProperties;
  annotationLayerStyle: CSSProperties;
  displayVideoWidth: number;
  displayVideoHeight: number;
  selectedAnnotationSet: ReviewAnnotationSet | null;
  selectedIssueId?: string;
  draftShapes: ReviewAnnotationShape[];
  activeShape: ReviewAnnotationShape | null;
  activeTextShape: ReviewAnnotationShape | null;
  videoRect: ContainedVideoRect;
  onBeginDraw: PointerEventHandler<HTMLDivElement>;
  onMoveDraw: PointerEventHandler<HTMLDivElement>;
  onEndDraw(): void;
  onLoadedMetadata(video: HTMLVideoElement): void;
  onCanPlay(): void;
  onWaiting(): void;
  onSeeked(video: HTMLVideoElement): void;
  onError(): void;
  onPlay(): void;
  onPause(): void;
  onTimeUpdate(video: HTMLVideoElement): void;
  onEnded(): void;
  onUpdateTextShape(shapeId: string, patch: Partial<ReviewAnnotationShape>): void;
  onCloseTextEditor(): void;
}

export function ReviewPlayerStage({
  version,
  videoRef,
  stageRef,
  textInputRef,
  annotationReadonly,
  muted,
  mediaState,
  containedMediaStyle,
  annotationLayerStyle,
  displayVideoWidth,
  displayVideoHeight,
  selectedAnnotationSet,
  selectedIssueId,
  draftShapes,
  activeShape,
  activeTextShape,
  videoRect,
  onBeginDraw,
  onMoveDraw,
  onEndDraw,
  onLoadedMetadata,
  onCanPlay,
  onWaiting,
  onSeeked,
  onError,
  onPlay,
  onPause,
  onTimeUpdate,
  onEnded,
  onUpdateTextShape,
  onCloseTextEditor,
}: ReviewPlayerStageProps) {
  const textCompositionRef = useRef(false);
  const textEditorStyle = activeTextShape
    ? createTextEditorStyle(activeTextShape, videoRect, displayVideoWidth)
    : undefined;
  return (
    <div className="fj-review-player-stage">
      <div
        ref={stageRef}
        className="fj-review-video-frame"
        data-annotation-readonly={annotationReadonly ? 'true' : 'false'}
        aria-label={`${version.label} · ${version.fileName}`}
        onPointerDown={onBeginDraw}
        onPointerMove={onMoveDraw}
        onPointerUp={onEndDraw}
        onPointerCancel={onEndDraw}
      >
        <video
          ref={videoRef}
          src={version.playbackUrl}
          crossOrigin="use-credentials"
          data-version-id={version.versionId}
          style={containedMediaStyle}
          controls={false}
          muted={muted}
          playsInline
          preload="metadata"
          onLoadedMetadata={(event) => onLoadedMetadata(event.currentTarget)}
          onCanPlay={onCanPlay}
          onWaiting={onWaiting}
          onSeeked={(event) => onSeeked(event.currentTarget)}
          onError={onError}
          onPlay={onPlay}
          onPause={onPause}
          onTimeUpdate={(event) => onTimeUpdate(event.currentTarget)}
          onEnded={onEnded}
        />
        <div className="fj-review-video-fallback" style={containedMediaStyle}>
          <strong>{version.label}</strong>
          <span>{version.fileName}</span>
        </div>
        <SavedAnnotationLayer
          annotationSet={selectedAnnotationSet}
          selectedIssueId={selectedIssueId}
          canvasWidth={displayVideoWidth}
          canvasHeight={displayVideoHeight}
          layerStyle={annotationLayerStyle}
        />
        <DraftAnnotationLayer
          draftShapes={draftShapes}
          activeShape={activeShape}
          canvasWidth={displayVideoWidth}
          canvasHeight={displayVideoHeight}
          layerStyle={annotationLayerStyle}
        />
        {activeTextShape && textEditorStyle ? (
          <div
            className="fj-review-text-annotation-layer"
            data-testid="text-annotation-editor-layer"
            style={{
              ...annotationLayerStyle,
              contain: 'layout paint',
              overflow: 'hidden',
              pointerEvents: 'none',
              position: 'absolute',
            }}
          >
            <form
              className="fj-review-text-annotation-editor"
              data-testid="text-annotation-editor"
              style={{ ...textEditorStyle, pointerEvents: 'auto', position: 'absolute' }}
              onPointerDown={(event) => event.stopPropagation()}
              onSubmit={(event) => {
                event.preventDefault();
                if (textCompositionRef.current) return;
                onCloseTextEditor();
              }}
            >
              <input
                ref={textInputRef}
                aria-label="文字批注内容"
                autoComplete="off"
                value={activeTextShape.text ?? ''}
                onChange={(event) => onUpdateTextShape(activeTextShape.shapeId, { text: event.target.value })}
                onCompositionStart={() => {
                  textCompositionRef.current = true;
                }}
                onCompositionEnd={() => {
                  textCompositionRef.current = false;
                }}
                onKeyDown={(event) => {
                  if (
                    event.key === 'Enter' &&
                    (textCompositionRef.current || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229)
                  ) {
                    return;
                  }
                  if (event.key === 'Escape' || event.key === 'Enter') {
                    event.preventDefault();
                    onCloseTextEditor();
                  }
                }}
              />
            </form>
          </div>
        ) : null}
        {mediaState === 'loading' ? <div className="fj-review-media-state">媒体加载中</div> : null}
        {mediaState === 'error' ? <div className="fj-review-media-state is-error">媒体加载失败</div> : null}
      </div>
    </div>
  );
}
