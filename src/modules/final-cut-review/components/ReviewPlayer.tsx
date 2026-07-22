import {
  forwardRef,
  useCallback,
  useImperativeHandle,
  useRef,
} from 'react';
import { createPortal } from 'react-dom';
import { frameFromTimestampMs } from '../core/timecode';
import { AnnotationToolbar } from './review-player-annotation-toolbar';
import { PlaybackControls } from './review-player-playback-controls';
import { ReviewPlayerStage } from './review-player-stage';
import { ReviewTimeline } from './review-player-timeline';
import type { ReviewPlayerHandle, ReviewPlayerProps } from './review-player-types';
import { useReviewPlayerAnnotations } from './review-player-use-annotations';
import { useReviewPlayerKeyboard } from './review-player-use-keyboard';
import { useReviewPlayerPlayback } from './review-player-use-playback';
import { useReviewPlayerStage } from './review-player-use-stage';

export type { ReviewPlayerHandle, ReviewPlayerSnapshot } from './review-player-types';
export { VersionRail, VersionStrip } from './review-player-version-navigation';

export const ReviewPlayer = forwardRef<ReviewPlayerHandle, ReviewPlayerProps>(function ReviewPlayer(props, ref) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const stageRef = useRef<HTMLDivElement | null>(null);
  const resetDraftRef = useRef<(() => void) | null>(null);
  const playback = useReviewPlayerPlayback({
    videoRef,
    version: props.version,
    issues: props.issues,
    initialTimeMs: props.initialTimeMs,
    resetDraftRef,
    onTimeChange: props.onTimeChange,
    onSelectIssue: props.onSelectIssue,
    onPlaybackError: props.onPlaybackError,
  });
  const stage = useReviewPlayerStage({
    stageRef,
    version: props.version,
    loadedMediaDimensions: playback.loadedMediaDimensions,
  });
  const annotationReadonly = Boolean(props.annotationReadonlyReason);
  const annotations = useReviewPlayerAnnotations({
    stageRef,
    videoRef,
    scopeKey: `${props.version.reviewItemId}:${props.version.versionId}`,
    displayVideoWidth: stage.displayVideoWidth,
    displayVideoHeight: stage.displayVideoHeight,
    displayMaxScale: stage.displayMaxScale,
    readonly: annotationReadonly,
    onDraftChange: props.onDraftChange,
    onPause: () => playback.setPlaying(false),
  });
  resetDraftRef.current = annotations.clearDraft;
  const setVideoNode = useCallback((node: HTMLVideoElement | null) => {
    videoRef.current = node;
  }, []);
  const setStageNode = useCallback((node: HTMLDivElement | null) => {
    stageRef.current = node;
  }, []);
  const setTextInputNode = useCallback(
    (node: HTMLInputElement | null) => {
      annotations.textInputRef.current = node;
    },
    [annotations.textInputRef],
  );

  useImperativeHandle(
    ref,
    () => ({
      playbackToTarget: playback.playbackToTarget,
      clearDraft: annotations.clearDraft,
      snapshot() {
        const frameNumber = frameFromTimestampMs(
          playback.currentMs,
          props.version.fpsNum,
          props.version.fpsDen,
        );
        const stageRect = stageRef.current?.getBoundingClientRect();
        return {
          timestampMs: playback.currentMs,
          frameNumber,
          canvasWidth: Math.round(stage.videoRect.width || stageRect?.width || 1280),
          canvasHeight: Math.round(stage.videoRect.height || stageRect?.height || 720),
          videoWidth: stage.displayVideoWidth,
          videoHeight: stage.displayVideoHeight,
        };
      },
    }),
    [
      annotations.clearDraft,
      playback.currentMs,
      playback.playbackToTarget,
      props.version.fpsDen,
      props.version.fpsNum,
      stage.displayVideoHeight,
      stage.displayVideoWidth,
      stage.videoRect.height,
      stage.videoRect.width,
    ],
  );

  useReviewPlayerKeyboard({
    version: props.version,
    annotationReadonly,
    clearDraft: annotations.clearDraft,
    setTool: annotations.setTool,
    stepFrames: playback.stepFrames,
    togglePlay: playback.togglePlay,
    onCreateIssueShortcut: props.onCreateIssueShortcut,
  });

  const annotationToolbar = (
    <AnnotationToolbar
      tool={annotations.tool}
      color={annotations.color}
      lineWidth={annotations.lineWidth}
      fontSize={annotations.fontSize}
      readonlyReason={props.annotationReadonlyReason}
      canUndo={annotations.draftShapes.length > 0}
      canRedo={annotations.redoShapes.length > 0}
      onTool={annotations.setTool}
      onColor={annotations.handleColorChange}
      onLineWidth={annotations.setLineWidth}
      onFontSize={annotations.handleFontSizeChange}
      onUndo={annotations.undo}
      onRedo={annotations.redo}
      onClear={annotations.clearDraft}
    />
  );
  const isToolbarDocked = Boolean(props.annotationToolbarHost);
  const showInlineToolbar = !isToolbarDocked && !props.disableInlineAnnotationToolbar;
  const currentFrameForAnnotations = frameFromTimestampMs(
    playback.currentMs,
    props.version.fpsNum,
    props.version.fpsDen,
  );
  const activeSelectedAnnotationSet =
    props.selectedAnnotationSet &&
    props.selectedAnnotationSet.versionId === props.version.versionId &&
    props.selectedAnnotationSet.frameNumber === currentFrameForAnnotations
      ? props.selectedAnnotationSet
      : null;
  const activeTextShape = annotations.activeTextShapeId
    ? annotations.draftShapes.find(
        (shape) =>
          shape.shapeId === annotations.activeTextShapeId && shape.tool === 'text' && shape.points?.[0],
      ) ?? null
    : null;
  const activeTextPoint = activeTextShape?.points?.[0];
  const selectedIssueIndex = playback.orderedIssues.findIndex(
    (issue) => issue.issueId === props.selectedIssueId,
  );

  return (
    <>
      <div
        className="fj-review-player-shell"
        data-testid="review-player"
        data-media-state={playback.mediaState}
        data-paused={!playback.playing}
        data-toolbar-docked={isToolbarDocked ? 'true' : 'false'}
        data-display-mode={stage.displayMode}
      >
        {showInlineToolbar ? <div className="fj-review-toolbar-inline-dock">{annotationToolbar}</div> : null}
        <section className="fj-review-player-card">
          <ReviewPlayerStage
            version={props.version}
            videoRef={setVideoNode}
            stageRef={setStageNode}
            textInputRef={setTextInputNode}
            annotationReadonly={annotationReadonly}
            muted={playback.muted}
            mediaState={playback.mediaState}
            containedMediaStyle={stage.containedMediaStyle}
            annotationLayerStyle={stage.annotationLayerStyle}
            displayVideoWidth={stage.displayVideoWidth}
            displayVideoHeight={stage.displayVideoHeight}
            selectedAnnotationSet={activeSelectedAnnotationSet}
            selectedIssueId={props.selectedIssueId}
            draftShapes={annotations.draftShapes}
            activeShape={annotations.activeShape}
            activeTextShape={activeTextPoint ? activeTextShape : null}
            videoRect={stage.videoRect}
            onBeginDraw={annotations.beginDraw}
            onMoveDraw={annotations.moveDraw}
            onEndDraw={annotations.endDraw}
            onLoadedMetadata={(video) => {
              if (video.videoWidth > 0 && video.videoHeight > 0) {
                playback.setLoadedMediaDimensions({
                  versionId: props.version.versionId,
                  width: video.videoWidth,
                  height: video.videoHeight,
                });
              }
              playback.setDurationMs(
                Number.isFinite(video.duration) ? Math.round(video.duration * 1000) : props.version.durationMs,
              );
              playback.setMediaState('ready');
            }}
            onCanPlay={() => playback.setMediaState('ready')}
            onWaiting={() => playback.setMediaState('loading')}
            onError={() => {
              playback.setMediaState('error');
              props.onPlaybackError('媒体加载失败');
            }}
            onPlay={() => playback.setPlaying(true)}
            onPause={() => playback.setPlaying(false)}
            onTimeUpdate={playback.handleMediaTimeUpdate}
            onEnded={() => playback.setPlaying(false)}
            onUpdateTextShape={annotations.updateDraftTextShape}
            onCloseTextEditor={annotations.closeTextEditor}
          />
          <ReviewTimeline
            issues={props.issues}
            selectedIssueId={props.selectedIssueId}
            currentMs={playback.currentMs}
            durationMs={playback.durationMs}
            fpsNum={props.version.fpsNum}
            fpsDen={props.version.fpsDen}
            onSeek={playback.seekToMs}
            onSelect={props.onSelectIssue}
          />
          <PlaybackControls
            version={props.version}
            currentMs={playback.currentMs}
            durationMs={playback.durationMs}
            playing={playback.playing}
            muted={playback.muted}
            volume={playback.volume}
            playbackRate={playback.playbackRate}
            timecodeInput={playback.timecodeInput}
            timecodeError={playback.timecodeError}
            onPlayPause={playback.togglePlay}
            onStep={playback.stepFrames}
            onVolume={playback.changeVolume}
            onMuted={playback.changeMuted}
            onRate={playback.changeRate}
            onTimecodeInput={playback.setTimecodeInput}
            onTimecodeSubmit={playback.submitTimecode}
            displayMode={stage.displayMode}
            onDisplayMode={stage.setDisplayMode}
            onFullscreen={() => stageRef.current?.requestFullscreen?.()}
            previousIssueDisabled={selectedIssueIndex <= 0}
            nextIssueDisabled={
              selectedIssueIndex < 0 || selectedIssueIndex >= playback.orderedIssues.length - 1
            }
            autoPause={playback.autoPause}
            onPreviousIssue={() => {
              if (selectedIssueIndex > 0) props.onSelectIssue(playback.orderedIssues[selectedIssueIndex - 1]);
            }}
            onNextIssue={() => {
              if (selectedIssueIndex >= 0 && selectedIssueIndex < playback.orderedIssues.length - 1) {
                props.onSelectIssue(playback.orderedIssues[selectedIssueIndex + 1]);
              }
            }}
            onAutoPause={playback.setAutoPause}
          />
        </section>
      </div>
      {props.annotationToolbarHost ? createPortal(annotationToolbar, props.annotationToolbarHost) : null}
    </>
  );
});
