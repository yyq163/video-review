import { useRef } from 'react';
import { useParams, useSearchParams } from 'react-router-dom';
import type { EntryMode } from '../contracts/types';
import { entryLinksFor } from '../entry/entry-links';
import { useWorkspace } from '../entry/use-review-queries';
import { AppShell, ErrorView, LoadingBlock } from '../components/shared';
import type { ReviewPlayerHandle } from '../components/ReviewPlayer';
import { ReviewWorkspaceContent } from './review-workspace-content';
import { useReviewWorkspaceController, type ReviewWorkspaceLoadedProps } from './review-workspace-controller';
import { useWorkspaceScrollRegion } from './review-workspace-scroll';

export function ReviewWorkspacePage(props: { entryMode: EntryMode }) {
  const { projectRefId = '', reviewItemId = '' } = useParams();
  const [searchParams] = useSearchParams();
  const selectedVersionId = searchParams.get('version') ?? undefined;
  const workspace = useWorkspace(props.entryMode, { projectRefId, reviewItemId, versionId: selectedVersionId });

  if (workspace.isLoading) {
    return (
      <AppShell
        entryMode={props.entryMode}
        homeHref={`/${props.entryMode}/projects`}
        entryLinks={entryLinksFor(props.entryMode)}
      >
        <LoadingBlock label="载入审阅工作台" />
      </AppShell>
    );
  }

  if (!workspace.data) {
    return (
      <AppShell
        entryMode={props.entryMode}
        homeHref={`/${props.entryMode}/projects`}
        entryLinks={entryLinksFor(props.entryMode)}
      >
        <ErrorView error={workspace.error ?? new Error('工作台不存在')} />
      </AppShell>
    );
  }

  return (
    <ReviewWorkspaceLoaded
      key={`${projectRefId}:${reviewItemId}`}
      entryMode={props.entryMode}
      projectRefId={projectRefId}
      reviewItemId={reviewItemId}
      data={workspace.data}
      refetchWorkspace={() => workspace.refetch({ throwOnError: true })}
    />
  );
}

function ReviewWorkspaceLoaded(props: ReviewWorkspaceLoadedProps) {
  const playerRef = useRef<ReviewPlayerHandle | null>(null);
  const { workspaceScrollRef, handleWorkspaceKeyDown } = useWorkspaceScrollRegion();
  const controller = useReviewWorkspaceController(props, playerRef);
  return (
    <ReviewWorkspaceContent
      controller={controller}
      playerRef={playerRef}
      workspaceScrollRef={workspaceScrollRef}
      onWorkspaceKeyDown={handleWorkspaceKeyDown}
    />
  );
}
