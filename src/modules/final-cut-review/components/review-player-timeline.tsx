import { useRef, type PointerEvent } from 'react';
import type { ReviewIssue } from '../contracts/types';
import { formatTimestampTimecode } from '../core/timecode';

interface ReviewTimelineProps {
  issues: ReviewIssue[];
  selectedIssueId?: string;
  currentMs: number;
  durationMs: number;
  fpsNum: number;
  fpsDen: number;
  onSeek(ms: number): void;
  onSelect(issue: ReviewIssue): void;
}

export function ReviewTimeline(props: ReviewTimelineProps) {
  const timelineDraggingRef = useRef(false);
  const durationMs = Math.max(1, props.durationMs);
  const seekFromTimelinePointer = (event: PointerEvent<HTMLLabelElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    if (rect.width <= 0) return;
    const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
    props.onSeek(ratio * durationMs);
  };
  const beginTimelineDrag = (event: PointerEvent<HTMLLabelElement>) => {
    if (event.pointerType === 'mouse' && event.button !== 0) return;
    event.preventDefault();
    timelineDraggingRef.current = true;
    event.currentTarget.setPointerCapture?.(event.pointerId);
    seekFromTimelinePointer(event);
  };
  const moveTimelineDrag = (event: PointerEvent<HTMLLabelElement>) => {
    if (!timelineDraggingRef.current) return;
    event.preventDefault();
    seekFromTimelinePointer(event);
  };
  const endTimelineDrag = (event: PointerEvent<HTMLLabelElement>) => {
    if (!timelineDraggingRef.current) return;
    event.preventDefault();
    seekFromTimelinePointer(event);
    timelineDraggingRef.current = false;
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  };
  const progress = Math.min(100, Math.max(0, (props.currentMs / durationMs) * 100));
  return (
    <label
      className="fj-review-range fj-review-edge-timeline"
      data-testid="video-edge-timeline"
      onPointerDown={beginTimelineDrag}
      onPointerMove={moveTimelineDrag}
      onPointerUp={endTimelineDrag}
      onPointerCancel={endTimelineDrag}
    >
      <input
        type="range"
        aria-label="视频时间轴"
        min={0}
        max={durationMs}
        value={props.currentMs}
        onChange={(event) => props.onSeek(Number(event.target.value))}
      />
      <span style={{ width: `${progress}%` }} />
      <div className="fj-review-timeline-markers" data-testid="timeline-markers">
        {props.issues.map((issue) => {
          const left = Math.min(100, Math.max(0, (issue.timestampMs / durationMs) * 100));
          const selected = issue.issueId === props.selectedIssueId;
          return (
            <button
              key={issue.issueId}
              type="button"
              className={`fj-review-timeline-marker ${issue.status === 'resolved' ? 'is-resolved' : 'is-open'} ${selected ? 'is-selected' : ''}`}
              data-testid={`timeline-marker-${issue.issueId}`}
              style={{ left: `${left}%` }}
              title={`#${issue.issueNo} ${formatTimestampTimecode(issue.timestampMs, props.fpsNum, props.fpsDen)} ${issue.status === 'unresolved' ? '未修改' : '已修改'} ${issue.body}`}
              aria-label={`意见 #${issue.issueNo} ${issue.status === 'unresolved' ? '未修改' : '已修改'}`}
              onPointerDown={(event) => {
                event.stopPropagation();
              }}
              onClick={(event) => {
                event.preventDefault();
                event.stopPropagation();
                props.onSelect(issue);
              }}
            />
          );
        })}
      </div>
    </label>
  );
}
