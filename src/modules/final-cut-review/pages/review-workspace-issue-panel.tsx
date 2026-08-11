import { IssuePanel } from '../components/IssuePanel';
import type { ReviewWorkspaceController } from './review-workspace-controller';

export function ReviewWorkspaceIssuePanel({
  controller,
  onSubmittingChange,
}: {
  controller: ReviewWorkspaceController;
  onSubmittingChange?(submitting: boolean): void;
}) {
  const { actions, data, playback, props } = controller;
  return (
    <IssuePanel
      entryMode={props.entryMode}
      version={controller.authoritativeCurrentVersion}
      versions={data.versions}
      issues={controller.currentIssues}
      historicalIssues={controller.historicalIssues}
      selectedIssueId={controller.selectedIssueId}
      isCurrentVersion={controller.isSelectedCurrent}
      pending={controller.pending}
      onSubmittingChange={onSubmittingChange}
      playbackPending={playback.playbackPending}
      playbackError={playback.playbackError}
      readonlyReason={controller.issuePanelReadonlyReason}
      statusReadonlyReason={controller.issueStatusReadonlyReason}
      onSelectIssue={playback.selectIssue}
      onCreateIssue={actions.createIssue}
      onEditIssue={actions.editIssue}
      onReplyIssue={actions.replyIssue}
      onResolve={actions.resolveIssue}
      onReopen={actions.reopenIssue}
      onDeleteIssue={actions.deleteIssue}
    />
  );
}
