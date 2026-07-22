import { useCallback, useLayoutEffect, useRef, type KeyboardEvent } from 'react';

function isKeyboardEditingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  if (target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement) return true;
  if (target instanceof HTMLInputElement) {
    return !['button', 'checkbox', 'radio', 'reset', 'submit'].includes(target.type);
  }
  return false;
}

export function useWorkspaceScrollRegion() {
  const workspaceScrollRef = useRef<HTMLElement | null>(null);
  const handleWorkspaceKeyDown = useCallback((event: KeyboardEvent<HTMLElement>) => {
    if (event.defaultPrevented || isKeyboardEditingTarget(event.target)) return;
    const workspace = event.currentTarget;
    const pageStep = Math.max(160, Math.round(workspace.clientHeight * 0.72));
    if (event.key === 'PageDown') {
      workspace.scrollTop += pageStep;
      event.preventDefault();
      return;
    }
    if (event.key === 'PageUp') {
      workspace.scrollTop -= pageStep;
      event.preventDefault();
      return;
    }
    if (event.key === 'Home') {
      workspace.scrollTop = 0;
      event.preventDefault();
      return;
    }
    if (event.key === 'End') {
      workspace.scrollTop = workspace.scrollHeight;
      event.preventDefault();
    }
  }, []);

  useLayoutEffect(() => {
    const workspaceNode = workspaceScrollRef.current;
    const rootNode = workspaceNode?.closest('.fj-review-root');
    if (!workspaceNode || !(rootNode instanceof HTMLElement)) return undefined;

    let animationFrame = 0;
    const updateScrollbarWidth = () => {
      const scrollbarWidth = Math.max(0, workspaceNode.offsetWidth - workspaceNode.clientWidth);
      rootNode.style.setProperty('--fj-review-workspace-scrollbar-width', `${scrollbarWidth}px`);
    };
    const scheduleUpdate = () => {
      window.cancelAnimationFrame(animationFrame);
      animationFrame = window.requestAnimationFrame(updateScrollbarWidth);
    };

    updateScrollbarWidth();
    const resizeObserver =
      typeof ResizeObserver === 'undefined'
        ? undefined
        : new ResizeObserver(() => {
            scheduleUpdate();
          });
    resizeObserver?.observe(workspaceNode);
    window.addEventListener('resize', scheduleUpdate);

    return () => {
      window.cancelAnimationFrame(animationFrame);
      resizeObserver?.disconnect();
      window.removeEventListener('resize', scheduleUpdate);
      rootNode.style.removeProperty('--fj-review-workspace-scrollbar-width');
    };
  }, []);

  return { workspaceScrollRef, handleWorkspaceKeyDown };
}
