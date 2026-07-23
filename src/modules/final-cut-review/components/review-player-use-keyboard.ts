import { useCallback, useEffect } from 'react';
import type { ReviewVersion } from '../contracts/types';
import type { AnnotationEditorTool } from './review-player-types';

export function useReviewPlayerKeyboard(options: {
  version: ReviewVersion;
  annotationReadonly: boolean;
  clearDraft(): void;
  setTool(tool: AnnotationEditorTool): void;
  stepFrames(delta: number): void;
  togglePlay(): Promise<void>;
  onCreateIssueShortcut?(): void;
}) {
  const {
    annotationReadonly,
    clearDraft,
    onCreateIssueShortcut,
    setTool,
    stepFrames,
    togglePlay,
    version,
  } = options;
  const handleKeyboardShortcut = useCallback(
    (event: KeyboardEvent) => {
      const target = event.target;
      if (
        target instanceof HTMLElement &&
        (target.isContentEditable || ['BUTTON', 'INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName))
      ) {
        return;
      }
      if (event.key === ' ' || event.key === 'Spacebar') {
        event.preventDefault();
        void togglePlay();
        return;
      }
      if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
        event.preventDefault();
        const direction = event.key === 'ArrowLeft' ? -1 : 1;
        const oneSecondFrames = Math.max(1, Math.round(version.fpsNum / version.fpsDen));
        stepFrames(event.shiftKey ? direction * oneSecondFrames : direction);
        return;
      }
      if (event.key.toLowerCase() === 'c') {
        if (annotationReadonly) return;
        event.preventDefault();
        onCreateIssueShortcut?.();
        return;
      }
      const toolByKey: Record<string, AnnotationEditorTool> = {
        '1': 'pen',
        '2': 'arrow',
        '3': 'rect',
        '4': 'circle',
        '5': 'text',
      };
      const nextTool = toolByKey[event.key];
      if (nextTool) {
        if (annotationReadonly) return;
        event.preventDefault();
        setTool(nextTool);
        return;
      }
      if (event.key === 'Escape') {
        setTool('select');
        clearDraft();
        if (document.fullscreenElement) void document.exitFullscreen?.();
      }
    },
    [
      annotationReadonly,
      clearDraft,
      onCreateIssueShortcut,
      setTool,
      stepFrames,
      togglePlay,
      version.fpsDen,
      version.fpsNum,
    ],
  );

  useEffect(() => {
    document.addEventListener('keydown', handleKeyboardShortcut);
    return () => document.removeEventListener('keydown', handleKeyboardShortcut);
  }, [handleKeyboardShortcut]);
}
