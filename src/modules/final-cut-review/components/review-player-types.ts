import type { ReviewAnnotationSet, ReviewAnnotationShape, ReviewIssue, ReviewVersion } from '../contracts/types';

export type AnnotationEditorTool = 'select' | ReviewAnnotationShape['tool'];
export type PlayerDisplayMode = 'fit' | 'original-ratio';
export type PlayerMediaState = 'loading' | 'ready' | 'error';

export interface ReviewPlayerSnapshot {
  timestampMs: number;
  frameNumber: number;
  canvasWidth: number;
  canvasHeight: number;
  videoWidth: number;
  videoHeight: number;
}

export interface ReviewPlayerHandle {
  playbackToTarget(target: import('../contracts/types').ReviewPlaybackTarget): Promise<void>;
  clearDraft(): void;
  snapshot(): ReviewPlayerSnapshot;
}

export interface ReviewPlayerProps {
  version: ReviewVersion;
  issues: ReviewIssue[];
  selectedAnnotationSet: ReviewAnnotationSet | null;
  selectedIssueId?: string;
  initialTimeMs?: number;
  annotationReadonlyReason?: string;
  annotationToolbarHost?: HTMLElement | null;
  disableInlineAnnotationToolbar?: boolean;
  onTimeChange(timeMs: number): void;
  onDraftChange(shapes: ReviewAnnotationShape[]): void;
  onSelectIssue(issue: ReviewIssue): void;
  onPlaybackError(error: string | null): void;
  onCreateIssueShortcut?(): void;
}

export interface RectLike {
  left: number;
  top: number;
  width: number;
  height: number;
}
