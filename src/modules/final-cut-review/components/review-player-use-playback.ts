import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type RefObject,
} from 'react';
import type { ReviewIssue, ReviewPlaybackTarget, ReviewVersion } from '../contracts/types';
import {
  PlaybackRequestSequencer,
  sortedIssuesForPlayback,
  targetTimeMsForVersion,
} from '../core/playback';
import {
  clampFrame,
  clampTimeMs,
  formatTimestampTimecode,
  frameFromTimestampMs,
  parseReviewTimecode,
  timestampMsFromFrame,
} from '../core/timecode';
import { waitForCanPlay, waitForEvent, waitForMetadata, waitForVideoFrame } from './review-player-media';
import type { PlayerMediaState } from './review-player-types';

interface PlaybackOptions {
  videoRef: RefObject<HTMLVideoElement | null>;
  version: ReviewVersion;
  issues: ReviewIssue[];
  initialTimeMs?: number;
  resetDraftRef: { current: (() => void) | null };
  onTimeChange(timeMs: number): void;
  onSelectIssue(issue: ReviewIssue): void;
  onPlaybackError(error: string | null): void;
}

function playbackErrorMessage(error: unknown): string {
  if (
    error !== null &&
    typeof error === 'object' &&
    'message' in error &&
    typeof error.message === 'string' &&
    error.message.length > 0
  ) {
    return error.message;
  }
  return '播放失败';
}

export function useReviewPlayerPlayback(options: PlaybackOptions) {
  const { videoRef, version, onTimeChange, onSelectIssue, onPlaybackError } = options;
  const requestAbortRef = useRef<AbortController | null>(null);
  const playRequestSequencerRef = useRef(new PlaybackRequestSequencer());
  const activeVersionIdRef = useRef(version.versionId);
  const autoPauseTriggeredRef = useRef<Set<string>>(new Set());
  const lastNaturalTimeMsRef = useRef(0);
  const [currentMs, setCurrentMs] = useState(options.initialTimeMs ?? 0);
  const [durationMs, setDurationMs] = useState(version.durationMs);
  const [playing, setPlaying] = useState(false);
  const [muted, setMuted] = useState(false);
  const [volume, setVolume] = useState(0.8);
  const [playbackRate, setPlaybackRate] = useState(1);
  const [autoPause, setAutoPause] = useState(true);
  const [mediaState, setMediaState] = useState<PlayerMediaState>('loading');
  const [timecodeInput, setTimecodeInput] = useState(
    formatTimestampTimecode(0, version.fpsNum, version.fpsDen),
  );
  const [timecodeError, setTimecodeError] = useState<string | null>(null);
  const [loadedMediaDimensions, setLoadedMediaDimensions] = useState<{
    versionId: string;
    width: number;
    height: number;
  } | null>(null);
  const orderedIssues = sortedIssuesForPlayback(options.issues);

  const publishTime = useCallback(
    (ms: number) => {
      const clamped = clampTimeMs(ms, durationMs);
      setCurrentMs(clamped);
      setTimecodeInput(formatTimestampTimecode(clamped, version.fpsNum, version.fpsDen));
      onTimeChange(clamped);
    },
    [durationMs, onTimeChange, version.fpsDen, version.fpsNum],
  );

  useLayoutEffect(() => {
    if (activeVersionIdRef.current === version.versionId) return;
    activeVersionIdRef.current = version.versionId;
    requestAbortRef.current?.abort();
    requestAbortRef.current = null;
    playRequestSequencerRef.current.cancel();
    videoRef.current?.pause();
    const nextMs = clampTimeMs(options.initialTimeMs ?? 0, version.durationMs);
    setCurrentMs(nextMs);
    setDurationMs(version.durationMs);
    setPlaying(false);
    setMediaState('loading');
    setTimecodeInput(formatTimestampTimecode(nextMs, version.fpsNum, version.fpsDen));
    setTimecodeError(null);
    setLoadedMediaDimensions(null);
    lastNaturalTimeMsRef.current = nextMs;
    autoPauseTriggeredRef.current.clear();
    options.resetDraftRef.current?.();
    onTimeChange(nextMs);
  }, [onTimeChange, options.initialTimeMs, options.resetDraftRef, version, videoRef]);

  useEffect(
    () => () => {
      requestAbortRef.current?.abort();
      playRequestSequencerRef.current.cancel();
    },
    [],
  );

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    video.volume = volume;
    video.muted = muted;
    video.playbackRate = playbackRate;
  }, [muted, playbackRate, version.versionId, videoRef, volume]);

  const seekToMs = useCallback(
    (ms: number) => {
      const video = videoRef.current;
      const clamped = clampTimeMs(ms, durationMs);
      if (clamped < lastNaturalTimeMsRef.current) {
        for (const issue of orderedIssues) {
          if (issue.timestampMs > clamped) autoPauseTriggeredRef.current.delete(issue.issueId);
        }
      }
      if (video) video.currentTime = clamped / 1000;
      lastNaturalTimeMsRef.current = clamped;
      publishTime(clamped);
    },
    [durationMs, orderedIssues, publishTime, videoRef],
  );

  const stepFrames = useCallback(
    (delta: number) => {
      playRequestSequencerRef.current.cancel();
      videoRef.current?.pause();
      const currentFrame = frameFromTimestampMs(currentMs, version.fpsNum, version.fpsDen);
      const targetFrame = clampFrame(currentFrame + delta, durationMs, version.fpsNum, version.fpsDen);
      seekToMs(timestampMsFromFrame(targetFrame, version.fpsNum, version.fpsDen));
      setPlaying(false);
    },
    [currentMs, durationMs, seekToMs, version, videoRef],
  );

  const submitTimecode = (rawInput: string) => {
    try {
      const normalizedInput = rawInput.trim();
      const frameNumber = parseReviewTimecode(normalizedInput, version.fpsNum, version.fpsDen);
      setTimecodeInput(normalizedInput);
      setTimecodeError(null);
      seekToMs(timestampMsFromFrame(frameNumber, version.fpsNum, version.fpsDen));
    } catch (error) {
      setTimecodeError(error instanceof Error ? error.message : '时间码无效');
    }
  };

  const togglePlay = useCallback(async () => {
    const video = videoRef.current;
    if (!video) return;
    if (video.paused) {
      const sequence = playRequestSequencerRef.current.next();
      try {
        await video.play();
        if (!playRequestSequencerRef.current.isCurrent(sequence)) return;
        setPlaying(true);
      } catch (error) {
        if (!playRequestSequencerRef.current.isCurrent(sequence)) return;
        onPlaybackError(playbackErrorMessage(error));
      }
    } else {
      playRequestSequencerRef.current.cancel();
      video.pause();
      setPlaying(false);
    }
  }, [onPlaybackError, videoRef]);

  const handleMediaPause = useCallback(() => {
    playRequestSequencerRef.current.cancel();
    setPlaying(false);
  }, []);

  const handleMediaTimeUpdate = (video: HTMLVideoElement) => {
    const nextMs = Math.round(video.currentTime * 1000);
    const previousMs = lastNaturalTimeMsRef.current;
    publishTime(nextMs);
    if (!autoPause || video.paused || video.seeking || nextMs <= previousMs) {
      lastNaturalTimeMsRef.current = nextMs;
      return;
    }
    const crossed = orderedIssues.find(
      (issue) =>
        issue.status === 'unresolved' &&
        !autoPauseTriggeredRef.current.has(issue.issueId) &&
        issue.timestampMs > previousMs &&
        issue.timestampMs <= nextMs,
    );
    lastNaturalTimeMsRef.current = nextMs;
    if (!crossed) return;
    autoPauseTriggeredRef.current.add(crossed.issueId);
    playRequestSequencerRef.current.cancel();
    video.pause();
    setPlaying(false);
    onSelectIssue(crossed);
  };

  const playbackToTarget = useCallback(
    async (target: ReviewPlaybackTarget) => {
      requestAbortRef.current?.abort();
      playRequestSequencerRef.current.cancel();
      const controller = new AbortController();
      requestAbortRef.current = controller;
      const video = videoRef.current;
      if (!video) throw new Error('播放器尚未挂载');
      if (version.versionId !== target.versionId || video.dataset.versionId !== target.versionId) {
        throw new Error('播放器媒体版本与回放目标不一致');
      }
      const targetMs = targetTimeMsForVersion(target, version);
      await waitForMetadata(video, controller.signal);
      await waitForCanPlay(video, controller.signal);
      if (version.versionId !== target.versionId || video.dataset.versionId !== target.versionId) {
        throw new Error('媒体加载期间版本已变化');
      }
      playRequestSequencerRef.current.cancel();
      video.pause();
      if (Math.abs(video.currentTime * 1000 - targetMs) >= 1) {
        const seekPromise = waitForEvent(video, 'seeked', controller.signal);
        video.currentTime = targetMs / 1000;
        await seekPromise;
        await waitForVideoFrame(video, controller.signal);
      }
      playRequestSequencerRef.current.cancel();
      video.pause();
      publishTime(targetMs);
      setPlaying(false);
    },
    [publishTime, version, videoRef],
  );

  const changeVolume = (nextVolume: number) => {
    const shouldMute = nextVolume <= 0;
    setVolume(nextVolume);
    setMuted(shouldMute);
    if (videoRef.current) {
      videoRef.current.volume = nextVolume;
      videoRef.current.muted = shouldMute;
    }
  };

  const changeMuted = (nextMuted: boolean) => {
    setMuted(nextMuted);
    if (videoRef.current) videoRef.current.muted = nextMuted;
  };

  const changeRate = (rate: number) => {
    setPlaybackRate(rate);
    if (videoRef.current) videoRef.current.playbackRate = rate;
  };

  return {
    autoPause,
    changeMuted,
    changeRate,
    changeVolume,
    currentMs,
    durationMs,
    handleMediaPause,
    handleMediaTimeUpdate,
    loadedMediaDimensions,
    mediaState,
    muted,
    orderedIssues,
    playbackRate,
    playbackToTarget,
    playing,
    seekToMs,
    setAutoPause,
    setDurationMs,
    setLoadedMediaDimensions,
    setMediaState,
    setPlaying,
    setTimecodeInput,
    stepFrames,
    submitTimecode,
    timecodeError,
    timecodeInput,
    togglePlay,
    volume,
  };
}
