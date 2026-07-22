import { useEffect, useRef } from 'react';
import {
  Maximize2,
  Minimize2,
  Pause,
  Play,
  RectangleHorizontal,
  StepBack,
  StepForward,
} from 'lucide-react';
import type { ReviewVersion } from '../contracts/types';
import {
  formatReviewTimecode,
  formatTimestampTimecode,
  frameFromTimestampMs,
} from '../core/timecode';
import type { PlayerDisplayMode } from './review-player-types';

interface PlaybackControlsProps {
  version: ReviewVersion;
  currentMs: number;
  durationMs: number;
  playing: boolean;
  muted: boolean;
  volume: number;
  playbackRate: number;
  timecodeInput: string;
  timecodeError: string | null;
  onPlayPause(): void;
  onStep(delta: -1 | 1): void;
  onVolume(volume: number): void;
  onMuted(muted: boolean): void;
  onRate(rate: number): void;
  onTimecodeInput(value: string): void;
  onTimecodeSubmit(value: string): void;
  displayMode: PlayerDisplayMode;
  onDisplayMode(mode: PlayerDisplayMode): void;
  onFullscreen(): void;
  previousIssueDisabled: boolean;
  nextIssueDisabled: boolean;
  autoPause: boolean;
  onPreviousIssue(): void;
  onNextIssue(): void;
  onAutoPause(autoPause: boolean): void;
}

export function PlaybackControls(props: PlaybackControlsProps) {
  const currentFrame = frameFromTimestampMs(props.currentMs, props.version.fpsNum, props.version.fpsDen);
  const pendingTimecodeInputRef = useRef(props.timecodeInput);
  useEffect(() => {
    pendingTimecodeInputRef.current = props.timecodeInput;
  }, [props.timecodeInput]);
  const syncTimecodeInput = (value: string) => {
    pendingTimecodeInputRef.current = value;
    props.onTimecodeInput(value);
  };
  const readTimecodeInput = (form: HTMLFormElement | null) => {
    const input = form?.elements.namedItem('timecode') as HTMLInputElement | null;
    return input?.value ?? pendingTimecodeInputRef.current ?? props.timecodeInput;
  };
  return (
    <div className="fj-review-player-controls">
      <button type="button" aria-label="上一帧" onClick={() => props.onStep(-1)}>
        <StepBack />
      </button>
      <button
        className="fj-review-play"
        type="button"
        aria-label={props.playing ? '暂停' : '播放'}
        onClick={props.onPlayPause}
      >
        {props.playing ? <Pause /> : <Play />}
      </button>
      <button type="button" aria-label="下一帧" onClick={() => props.onStep(1)}>
        <StepForward />
      </button>
      <button type="button" aria-label="上一条意见" disabled={props.previousIssueDisabled} onClick={props.onPreviousIssue}>
        上一条
      </button>
      <button type="button" aria-label="下一条意见" disabled={props.nextIssueDisabled} onClick={props.onNextIssue}>
        下一条
      </button>
      <form
        className="fj-review-timecode-form"
        autoComplete="off"
        onSubmit={(event) => {
          event.preventDefault();
          props.onTimecodeSubmit(readTimecodeInput(event.currentTarget));
        }}
      >
        <input
          aria-label="时间码输入"
          autoCapitalize="off"
          autoComplete="off"
          autoCorrect="off"
          inputMode="numeric"
          name="timecode"
          spellCheck={false}
          value={props.timecodeInput}
          onChange={(event) => syncTimecodeInput(event.target.value)}
          onInput={(event) => syncTimecodeInput(event.currentTarget.value)}
        />
        <button
          type="submit"
          onMouseDown={(event) => {
            pendingTimecodeInputRef.current = readTimecodeInput(event.currentTarget.form);
          }}
        >
          跳转
        </button>
      </form>
      <span className="fj-review-sr-only" data-testid="current-timecode">
        {formatReviewTimecode(currentFrame, props.version.fpsNum, props.version.fpsDen)}
      </span>
      <span className="fj-review-sr-only" data-testid="current-frame">
        {currentFrame}
      </span>
      <span className="fj-review-sr-only" data-testid="duration-timecode">
        {formatTimestampTimecode(props.durationMs, props.version.fpsNum, props.version.fpsDen)}
      </span>
      <label className="fj-review-volume">
        <span>音量</span>
        <input
          aria-label="音量"
          type="range"
          min={0}
          max={1}
          step={0.05}
          value={props.volume}
          onChange={(event) => props.onVolume(Number(event.target.value))}
        />
      </label>
      <button type="button" aria-label={props.muted ? '取消静音' : '静音'} onClick={() => props.onMuted(!props.muted)}>
        {props.muted ? '静音' : '有声'}
      </button>
      <select aria-label="倍速" value={props.playbackRate} onChange={(event) => props.onRate(Number(event.target.value))}>
        {[0.5, 0.75, 1, 1.25, 1.5, 2].map((rate) => (
          <option key={rate} value={rate}>
            {rate}x
          </option>
        ))}
      </select>
      <button
        type="button"
        title="适应窗口"
        aria-label="适应窗口"
        aria-pressed={props.displayMode === 'fit'}
        className={props.displayMode === 'fit' ? 'is-active' : undefined}
        onClick={() => props.onDisplayMode('fit')}
      >
        <RectangleHorizontal />
      </button>
      <button
        type="button"
        title="原始比例"
        aria-label="原始比例"
        aria-pressed={props.displayMode === 'original-ratio'}
        className={props.displayMode === 'original-ratio' ? 'is-active' : undefined}
        onClick={() => props.onDisplayMode('original-ratio')}
      >
        <Minimize2 />
      </button>
      <button type="button" title="全屏" aria-label="全屏" onClick={props.onFullscreen}>
        <Maximize2 />
      </button>
      <label className="fj-review-auto-pause">
        <input
          type="checkbox"
          checked={props.autoPause}
          onChange={(event) => props.onAutoPause(event.currentTarget.checked)}
        />
        <span>自动暂停</span>
      </label>
      {props.timecodeError ? <span className="fj-review-field-error">{props.timecodeError}</span> : null}
    </div>
  );
}
