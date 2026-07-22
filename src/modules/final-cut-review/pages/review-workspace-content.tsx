import { useCallback, useEffect, useRef, useState, type KeyboardEventHandler, type RefObject } from 'react';
import { History, MessageSquareText, X } from 'lucide-react';
import { Link } from 'react-router-dom';
import { CapabilityGate, AppShell, StatusBadge } from '../components/shared';
import { entryLinksFor } from '../entry/entry-links';
import { VersionRail } from '../components/ReviewPlayer';
import { VersionComparePanel } from '../components/VersionComparePanel';
import type { ReviewPlayerHandle } from '../components/ReviewPlayer';
import type { ReviewWorkspaceController } from './review-workspace-controller';
import { ReviewWorkspaceIssuePanel } from './review-workspace-issue-panel';
import { ReviewWorkspaceMainColumn } from './review-workspace-main-column';

const COMPACT_WORKSPACE_QUERY = '(max-width: 1279px)';

function isCompactWorkspace() {
  if (typeof window.matchMedia === 'function') return window.matchMedia(COMPACT_WORKSPACE_QUERY).matches;
  return window.innerWidth < 1280;
}

export function ReviewWorkspaceContent({
  controller,
  playerRef,
  workspaceScrollRef,
  onWorkspaceKeyDown,
}: {
  controller: ReviewWorkspaceController;
  playerRef: RefObject<ReviewPlayerHandle | null>;
  workspaceScrollRef: RefObject<HTMLElement | null>;
  onWorkspaceKeyDown: KeyboardEventHandler<HTMLElement>;
}) {
  const { data, props } = controller;
  const [versionRailOpen, setVersionRailOpen] = useState(false);
  const [issuePanelOpen, setIssuePanelOpen] = useState(false);
  const [issueSubmitting, setIssueSubmitting] = useState(false);
  const [compactWorkspace, setCompactWorkspace] = useState(isCompactWorkspace);
  const issuePanelToggleRef = useRef<HTMLButtonElement | null>(null);
  const issuePanelCloseRef = useRef<HTMLButtonElement | null>(null);
  const issuePanelDrawerRef = useRef<HTMLDivElement | null>(null);
  const issueSubmittingRef = useRef(false);
  const handleIssueSubmittingChange = useCallback((submitting: boolean) => {
    issueSubmittingRef.current = submitting;
    setIssueSubmitting(submitting);
  }, []);
  const closeIssuePanel = useCallback(() => {
    if (issueSubmittingRef.current) return;
    setIssuePanelOpen(false);
    window.requestAnimationFrame(() => issuePanelToggleRef.current?.focus());
  }, []);
  const issueDrawerActive = compactWorkspace && issuePanelOpen;

  useEffect(() => {
    const mediaQuery = typeof window.matchMedia === 'function' ? window.matchMedia(COMPACT_WORKSPACE_QUERY) : null;
    const syncCompactState = () => {
      const nextCompact = mediaQuery?.matches ?? window.innerWidth < 1280;
      setCompactWorkspace(nextCompact);
      if (!nextCompact) {
        setIssuePanelOpen(false);
        setVersionRailOpen(false);
      }
    };
    syncCompactState();
    if (mediaQuery) mediaQuery.addEventListener('change', syncCompactState);
    else window.addEventListener('resize', syncCompactState);
    return () => {
      if (mediaQuery) mediaQuery.removeEventListener('change', syncCompactState);
      else window.removeEventListener('resize', syncCompactState);
    };
  }, []);

  useEffect(() => {
    if (!issueDrawerActive) return undefined;
    const focusFrame = window.requestAnimationFrame(() => issuePanelCloseRef.current?.focus());
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        if (!issueSubmittingRef.current) closeIssuePanel();
        return;
      }
      if (event.key !== 'Tab') return;
      const focusable = Array.from(
        issuePanelDrawerRef.current?.querySelectorAll<HTMLElement>(
          'button:not(:disabled), textarea:not(:disabled), input:not(:disabled), select:not(:disabled), a[href]',
        ) ?? [],
      );
      const first = focusable[0];
      const last = focusable.at(-1);
      if (!first || !last) return;
      if (!issuePanelDrawerRef.current?.contains(document.activeElement)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
        return;
      }
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', closeOnEscape);
    return () => {
      window.cancelAnimationFrame(focusFrame);
      document.removeEventListener('keydown', closeOnEscape);
    };
  }, [closeIssuePanel, issueDrawerActive]);

  return (
    <AppShell
      entryMode={props.entryMode}
      homeHref={`/${props.entryMode}/projects`}
      entryLinks={entryLinksFor(props.entryMode)}
      viewportLocked
      right={
        <>
          <StatusBadge status={data.item.status} />
          <Link className="fj-review-secondary" to={`/${props.entryMode}/projects/${props.projectRefId}`}>
            返回项目
          </Link>
        </>
      }
    >
      <section
        aria-label="审片主工作区"
        className="fj-review-workspace"
        data-testid="review-workspace-scroll-region"
        onKeyDown={onWorkspaceKeyDown}
        ref={workspaceScrollRef}
        tabIndex={0}
      >
        <div className="fj-review-workspace-frame" data-testid="review-workspace-frame">
          <div className="fj-review-responsive-controls" aria-label="窄屏工作台面板控制">
            <button
              aria-controls="review-version-rail-region"
              aria-expanded={versionRailOpen}
              aria-label={versionRailOpen ? '折叠版本栏' : '展开版本栏'}
              className="fj-review-responsive-control"
              onClick={() => setVersionRailOpen((open) => !open)}
              title={versionRailOpen ? '折叠版本栏' : '展开版本栏'}
              type="button"
            >
              <History />
            </button>
            <button
              aria-controls="review-issue-drawer"
              aria-expanded={issueDrawerActive}
              aria-label={issueDrawerActive ? '关闭意见栏' : '打开意见栏'}
              className="fj-review-responsive-control"
              disabled={issueDrawerActive && issueSubmitting}
              onClick={() => (issueDrawerActive ? closeIssuePanel() : setIssuePanelOpen(true))}
              ref={issuePanelToggleRef}
              title={issueDrawerActive ? '关闭意见栏' : '打开意见栏'}
              type="button"
            >
              <MessageSquareText />
            </button>
          </div>
          <div className={`fj-review-workbench ${versionRailOpen ? 'is-version-rail-open' : ''}`}>
            <ReviewWorkspaceMainColumn ref={playerRef} controller={controller} />
            <div
              className={`fj-review-version-rail-region ${versionRailOpen ? 'is-open' : ''}`}
              id="review-version-rail-region"
            >
              <VersionRail
                versions={data.versions}
                currentVersionId={data.currentVersion.versionId}
                onSelect={controller.selectVersion}
              />
            </div>
            {issueDrawerActive ? (
              <button
                aria-label="关闭意见栏"
                className="fj-review-issue-drawer-scrim"
                disabled={issueSubmitting}
                onClick={closeIssuePanel}
                tabIndex={-1}
                type="button"
              />
            ) : null}
            <div
              aria-busy={issueSubmitting || undefined}
              aria-label={issueDrawerActive ? '意见反馈' : undefined}
              aria-modal={issueDrawerActive ? true : undefined}
              className={`fj-review-issue-drawer-region ${issueDrawerActive ? 'is-open' : ''}`}
              id="review-issue-drawer"
              ref={issuePanelDrawerRef}
              role={issueDrawerActive ? 'dialog' : undefined}
            >
              <button
                aria-label="关闭意见栏"
                className="fj-review-issue-drawer-close"
                disabled={issueSubmitting}
                onClick={closeIssuePanel}
                ref={issuePanelCloseRef}
                title="关闭意见栏"
                type="button"
              >
                <X />
              </button>
              <ReviewWorkspaceIssuePanel controller={controller} onSubmittingChange={handleIssueSubmittingChange} />
            </div>
          </div>
          <CapabilityGate entryMode={props.entryMode} capability="review.version.compare">
            <VersionComparePanel
              versions={data.versions}
              currentVersionId={data.item.currentVersionId}
              issues={controller.compareIssues}
            />
          </CapabilityGate>
        </div>
      </section>
    </AppShell>
  );
}
