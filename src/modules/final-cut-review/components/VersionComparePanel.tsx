import { useCallback, useMemo, useRef, useState } from 'react';
import type { ReviewAnnotationShape, ReviewIssue, ReviewVersion, VersionId } from '../contracts/types';
import { normalizedPathToCanvasPath, normalizedVideoPointToCanvasPoint } from '../core/coordinates';
import { formatTimestampTimecode } from '../core/timecode';

interface VersionComparePanelProps {
  versions: ReviewVersion[];
  currentVersionId: VersionId;
  issues: ReviewIssue[];
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
}: {
  side: 'left' | 'right';
  version: ReviewVersion;
  issues: ReviewIssue[];
  onPlaybackEvent: (side: 'left' | 'right', event: 'play' | 'pause' | 'seek' | 'timeupdate') => void;
  onVideoElement: (element: HTMLVideoElement | null) => void;
}) {
  const shapes = issues.flatMap((issue) => issue.currentAnnotationSet?.shapes ?? []);
  return (
    <article className="fj-review-compare-pane" data-testid={`version-compare-${side}`}>
      <header>
        <strong>{version.label}</strong>
        <span>{version.fileName}</span>
      </header>
      <div className="fj-review-compare-frame">
        <video
          ref={onVideoElement}
          src={version.playbackUrl}
          controls
          preload="metadata"
          aria-label={`${side === 'left' ? '左侧' : '右侧'}版本播放器 ${version.label}`}
          onPlay={() => onPlaybackEvent(side, 'play')}
          onPause={() => onPlaybackEvent(side, 'pause')}
          onSeeked={() => onPlaybackEvent(side, 'seek')}
          onTimeUpdate={() => onPlaybackEvent(side, 'timeupdate')}
        />
        <svg
          className="fj-review-compare-annotation-layer"
          data-testid={`version-compare-${side}-annotation-layer`}
          viewBox={`0 0 ${version.width} ${version.height}`}
          aria-label={`${version.label} 独立批注层`}
        >
          {shapes.map((shape) => renderCompareShape(shape, version.width, version.height))}
        </svg>
      </div>
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
        {issues.length ? (
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

  if (sortedVersions.length < 2) return null;

  const leftVersion = sortedVersions.find((version) => version.versionId === leftVersionId) ?? sortedVersions[0];
  const rightVersion =
    sortedVersions.find((version) => version.versionId === rightVersionId && version.versionId !== leftVersion.versionId) ??
    sortedVersions.find((version) => version.versionId !== leftVersion.versionId) ??
    sortedVersions[1];
  const issuesByVersion = (versionId: VersionId) => props.issues.filter((issue) => issue.versionId === versionId);

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
          issues={issuesByVersion(leftVersion.versionId)}
          onVideoElement={setLeftVideoElement}
          onPlaybackEvent={syncPeer}
        />
        <ComparePane
          side="right"
          version={rightVersion}
          issues={issuesByVersion(rightVersion.versionId)}
          onVideoElement={setRightVideoElement}
          onPlaybackEvent={syncPeer}
        />
      </div>
    </section>
  );
}
