import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type {
  EntryMode,
  ReviewAnnotationShape,
  ReviewIssue,
  ReviewItemId,
  ReviewVersion,
  VersionId,
} from '../contracts/types';
import { normalizedPathToCanvasPath, normalizedVideoPointToCanvasPoint } from '../core/coordinates';
import { formatTimestampTimecode } from '../core/timecode';
import { useVersionIssues } from '../entry/use-review-queries';

interface VersionComparePanelProps {
  entryMode: EntryMode;
  projectRefId: string;
  reviewItemId: ReviewItemId;
  versions: ReviewVersion[];
  currentVersionId: VersionId;
  workspaceVersionId: VersionId;
  workspaceIssues: ReviewIssue[];
}

type ComparePlaybackState = 'loading' | 'ready' | 'playing' | 'paused' | 'waiting' | 'seeking' | 'error';

function comparePlaybackLabel(version: ReviewVersion, state: ComparePlaybackState): string {
  const labels: Record<ComparePlaybackState, string> = {
    loading: '正在加载',
    ready: '可播放',
    playing: '播放中',
    paused: '已暂停',
    waiting: '缓冲中',
    seeking: '正在定位',
    error: '播放失败',
  };
  return `${version.label} ${labels[state]}`;
}

function issueTimecode(issue: ReviewIssue, version: ReviewVersion) {
  return formatTimestampTimecode(issue.timestampMs, version.fpsNum, version.fpsDen);
}

function renderCompareShape(shape: ReviewAnnotationShape, canvasWidth: number, canvasHeight: number) {
  const strokeWidth = Math.max(2, shape.lineWidth * 2);
  const textFontSize = Math.min(96, Math.max(12, shape.fontSize ?? 32));
  if (shape.points?.length) {
    const points = normalizedPathToCanvasPath({ points: shape.points, canvasWidth, canvasHeight });
    if (shape.tool === 'arrow' && points.length >= 2) {
      const start = points[0];
      const end = points[points.length - 1];
      const angle = Math.atan2(end.y - start.y, end.x - start.x);
      const arrowStrokeWidth = Math.max(4, strokeWidth);
      const haloStrokeWidth = arrowStrokeWidth + 6;
      const size = Math.max(22, arrowStrokeWidth * 4.5);
      const left = {
        x: end.x - size * Math.cos(angle - Math.PI / 6),
        y: end.y - size * Math.sin(angle - Math.PI / 6),
      };
      const right = {
        x: end.x - size * Math.cos(angle + Math.PI / 6),
        y: end.y - size * Math.sin(angle + Math.PI / 6),
      };
      const headPath = `M ${left.x} ${left.y} L ${end.x} ${end.y} L ${right.x} ${right.y}`;
      return (
        <g key={shape.shapeId}>
          <line x1={start.x} y1={start.y} x2={end.x} y2={end.y} stroke="rgba(0,0,0,0.72)" strokeWidth={haloStrokeWidth} strokeLinecap="round" />
          <path d={headPath} fill="none" stroke="rgba(0,0,0,0.72)" strokeWidth={haloStrokeWidth} strokeLinecap="round" strokeLinejoin="round" />
          <line x1={start.x} y1={start.y} x2={end.x} y2={end.y} stroke={shape.color} strokeWidth={arrowStrokeWidth} strokeLinecap="round" />
          <path d={headPath} fill="none" stroke={shape.color} strokeWidth={arrowStrokeWidth} strokeLinecap="round" strokeLinejoin="round" />
        </g>
      );
    }

    if (shape.tool === 'text') {
      const point = normalizedVideoPointToCanvasPoint({ point: shape.points[0], canvasWidth, canvasHeight });
      return (
        <text
          key={shape.shapeId}
          x={point.x}
          y={point.y}
          fill={shape.color}
          fontSize={textFontSize}
          fontWeight="700"
          paintOrder="stroke"
          stroke="rgba(0,0,0,0.58)"
          strokeWidth={Math.max(2, textFontSize * 0.08)}
        >
          {shape.text || '文字'}
        </text>
      );
    }

    return (
      <polyline
        key={shape.shapeId}
        points={points.map((point) => `${point.x},${point.y}`).join(' ')}
        fill="none"
        stroke={shape.color}
        strokeWidth={strokeWidth}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    );
  }

  if (!shape.bounds) return null;
  const x = shape.bounds.x * canvasWidth;
  const y = shape.bounds.y * canvasHeight;
  const width = shape.bounds.width * canvasWidth;
  const height = shape.bounds.height * canvasHeight;
  if (shape.tool === 'circle') {
    return (
      <ellipse
        key={shape.shapeId}
        cx={x + width / 2}
        cy={y + height / 2}
        rx={Math.max(1, width / 2)}
        ry={Math.max(1, height / 2)}
        fill="transparent"
        stroke={shape.color}
        strokeWidth={strokeWidth}
      />
    );
  }

  return (
    <rect key={shape.shapeId} x={x} y={y} width={width} height={height} rx="3" fill="transparent" stroke={shape.color} strokeWidth={strokeWidth} />
  );
}

function ComparePane({
  side,
  version,
  issues,
  onPlaybackEvent,
  onVideoElement,
  issuesLoading,
  issuesError,
}: {
  side: 'left' | 'right';
  version: ReviewVersion;
  issues: ReviewIssue[];
  onPlaybackEvent: (side: 'left' | 'right', event: 'play' | 'pause' | 'seek' | 'timeupdate') => void;
  onVideoElement: (element: HTMLVideoElement | null) => void;
  issuesLoading: boolean;
  issuesError: boolean;
}) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [playbackState, setPlaybackState] = useState<ComparePlaybackState>('loading');
  const shapes = issues.flatMap((issue) => issue.currentAnnotationSet?.shapes ?? []);
  const playbackReady = version.playbackStatus === 'ready' && Boolean(version.playbackUrl);
  useEffect(() => {
    const video = videoRef.current;
    if (!video || !playbackReady || !version.playbackUrl) {
      onVideoElement(null);
      return;
    }
    setPlaybackState('loading');
    video.setAttribute('src', version.playbackUrl);
    video.load();
    onVideoElement(video);
    return () => {
      onVideoElement(null);
      video.pause();
      video.removeAttribute('src');
      video.load();
    };
  }, [onVideoElement, playbackReady, version.playbackUrl, version.versionId]);

  return (
    <article className="fj-review-compare-pane" data-testid={`version-compare-${side}`}>
      <header>
        <strong>{version.label}</strong>
        <span>{version.fileName}</span>
      </header>
      <div className="fj-review-compare-frame">
        {playbackReady ? (
          <video
            key={version.versionId}
            ref={videoRef}
            controls
            crossOrigin="use-credentials"
            preload="metadata"
            aria-label={`${side === 'left' ? '左侧' : '右侧'}版本播放器 ${version.label}`}
            onCanPlay={() => setPlaybackState('ready')}
            onError={() => setPlaybackState('error')}
            onLoadStart={() => setPlaybackState('loading')}
            onPause={() => {
              setPlaybackState('paused');
              onPlaybackEvent(side, 'pause');
            }}
            onPlay={() => onPlaybackEvent(side, 'play')}
            onPlaying={() => setPlaybackState('playing')}
            onSeeking={() => setPlaybackState('seeking')}
            onSeeked={() => {
              setPlaybackState('ready');
              onPlaybackEvent(side, 'seek');
            }}
            onTimeUpdate={() => onPlaybackEvent(side, 'timeupdate')}
            onWaiting={() => setPlaybackState('waiting')}
          />
        ) : (
          <div className="fj-review-media-not-ready" role="status">
            {version.playbackStatus === 'failed' ? '播放资产生成失败' : '播放资产生成中'}
          </div>
        )}
        <svg
          className="fj-review-compare-annotation-layer"
          data-testid={`version-compare-${side}-annotation-layer`}
          viewBox={`0 0 ${version.width} ${version.height}`}
          aria-label={`${version.label} 独立批注层`}
        >
          {shapes.map((shape) => renderCompareShape(shape, version.width, version.height))}
        </svg>
      </div>
      {playbackReady ? (
        <div
          className={`fj-review-compare-playback-status is-${playbackState}`}
          data-testid={`version-compare-${side}-playback-status`}
          role={playbackState === 'error' ? 'alert' : 'status'}
        >
          {comparePlaybackLabel(version, playbackState)}
        </div>
      ) : null}
      <dl>
        <div>
          <dt>分辨率</dt>
          <dd>
            {version.width}x{version.height}
          </dd>
        </div>
        <div>
          <dt>帧率</dt>
          <dd>
            {version.fpsNum}/{version.fpsDen}
          </dd>
        </div>
        <div>
          <dt>时长</dt>
          <dd>{Math.round(version.durationMs / 1000)}s</dd>
        </div>
        <div>
          <dt>上传</dt>
          <dd>{new Date(version.uploadedAt).toLocaleString()}</dd>
        </div>
      </dl>
      <ul className="fj-review-compare-issues" aria-label={`${version.label} 独立意见`}>
        {issuesLoading ? (
          <li>正在加载当前比较版本意见...</li>
        ) : issuesError ? (
          <li role="alert">比较版本意见加载失败</li>
        ) : issues.length ? (
          issues.map((issue) => (
            <li key={issue.issueId}>
              <span>#{issue.issueNo.toString().padStart(3, '0')}</span>
              <span>{issueTimecode(issue, version)}</span>
              <span>{issue.status === 'resolved' ? '已修改' : '未修改'}</span>
              <p>{issue.body}</p>
            </li>
          ))
        ) : (
          <li>当前版本无意见</li>
        )}
      </ul>
    </article>
  );
}

export function VersionComparePanel(props: VersionComparePanelProps) {
  const sortedVersions = useMemo(() => [...props.versions].sort((a, b) => a.versionNo - b.versionNo), [props.versions]);
  if (sortedVersions.length < 2) return null;
  return <VersionCompareBody {...props} sortedVersions={sortedVersions} />;
}

function VersionCompareBody(
  props: VersionComparePanelProps & { sortedVersions: ReviewVersion[] },
) {
  const { sortedVersions } = props;
  const [leftVersionId, setLeftVersionId] = useState(sortedVersions[0]?.versionId ?? props.currentVersionId);
  const [rightVersionId, setRightVersionId] = useState(props.currentVersionId);
  const [syncPlayback, setSyncPlayback] = useState(false);
  const leftRef = useRef<HTMLVideoElement | null>(null);
  const rightRef = useRef<HTMLVideoElement | null>(null);
  const applyingSyncRef = useRef(false);
  const lastDriftSyncAtRef = useRef(0);
  const setLeftVideoElement = useCallback((element: HTMLVideoElement | null) => {
    leftRef.current = element;
  }, []);
  const setRightVideoElement = useCallback((element: HTMLVideoElement | null) => {
    rightRef.current = element;
  }, []);

  const leftVersion = sortedVersions.find((version) => version.versionId === leftVersionId) ?? sortedVersions[0];
  const rightVersion =
    sortedVersions.find((version) => version.versionId === rightVersionId && version.versionId !== leftVersion.versionId) ??
    sortedVersions.find((version) => version.versionId !== leftVersion.versionId) ??
    sortedVersions[1];
  const leftIssues = useVersionIssues(props.entryMode, {
    projectRefId: props.projectRefId,
    reviewItemId: props.reviewItemId,
    versionId: leftVersion.versionId,
    initialData:
      props.workspaceVersionId === leftVersion.versionId
        ? props.workspaceIssues
        : undefined,
  });
  const rightIssues = useVersionIssues(props.entryMode, {
    projectRefId: props.projectRefId,
    reviewItemId: props.reviewItemId,
    versionId: rightVersion.versionId,
    initialData:
      props.workspaceVersionId === rightVersion.versionId
        ? props.workspaceIssues
        : undefined,
  });

  const syncPeer = (side: 'left' | 'right', event: 'play' | 'pause' | 'seek' | 'timeupdate') => {
    if (!syncPlayback || applyingSyncRef.current) return;
    const source = side === 'left' ? leftRef.current : rightRef.current;
    const peer = side === 'left' ? rightRef.current : leftRef.current;
    if (!source || !peer) return;
    const peerDuration = Number.isFinite(peer.duration) && peer.duration > 0 ? peer.duration : source.currentTime;
    const targetTime = Math.min(source.currentTime, peerDuration);
    const driftSeconds = Math.abs(peer.currentTime - targetTime);
    if (event === 'timeupdate') {
      const now = performance.now();
      if (driftSeconds < 0.25 || now - lastDriftSyncAtRef.current < 250) return;
      lastDriftSyncAtRef.current = now;
    }
    applyingSyncRef.current = true;
    try {
      if (driftSeconds > 0.05) {
        peer.currentTime = targetTime;
      }
      if (event === 'play' && peer.paused) {
        void peer.play().catch(() => undefined);
      }
      if (event === 'pause' && !peer.paused) {
        peer.pause();
      }
    } finally {
      window.setTimeout(() => {
        applyingSyncRef.current = false;
      }, 80);
    }
  };

  return (
    <section className="fj-review-version-compare" data-testid="version-compare-panel" aria-label="人工版本对比">
      <header className="fj-review-compare-head">
        <div>
          <strong>版本对比</strong>
          <span>同一成片条目内左右双播放器，意见和标记层按版本独立显示。</span>
        </div>
        <label>
          <input type="checkbox" checked={syncPlayback} onChange={(event) => setSyncPlayback(event.target.checked)} />
          同步播放
        </label>
      </header>
      <div className="fj-review-compare-selectors">
        <label>
          左侧版本
          <select value={leftVersion.versionId} onChange={(event) => setLeftVersionId(event.target.value)}>
            {sortedVersions.map((version) => (
              <option key={version.versionId} value={version.versionId} disabled={version.versionId === rightVersion.versionId}>
                {version.label} · {version.fileName}
              </option>
            ))}
          </select>
        </label>
        <label>
          右侧版本
          <select value={rightVersion.versionId} onChange={(event) => setRightVersionId(event.target.value)}>
            {sortedVersions.map((version) => (
              <option key={version.versionId} value={version.versionId} disabled={version.versionId === leftVersion.versionId}>
                {version.label} · {version.fileName}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="fj-review-compare-grid">
        <ComparePane
          side="left"
          version={leftVersion}
          issues={leftIssues.data ?? []}
          issuesLoading={leftIssues.isLoading}
          issuesError={leftIssues.isError}
          onVideoElement={setLeftVideoElement}
          onPlaybackEvent={syncPeer}
        />
        <ComparePane
          side="right"
          version={rightVersion}
          issues={rightIssues.data ?? []}
          issuesLoading={rightIssues.isLoading}
          issuesError={rightIssues.isError}
          onVideoElement={setRightVideoElement}
          onPlaybackEvent={syncPeer}
        />
      </div>
    </section>
  );
}
