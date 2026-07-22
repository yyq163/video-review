import type { EntryMode, ReviewIssue, ReviewVersion } from '../contracts/types';

export interface IssuePanelProps {
  entryMode: EntryMode;
  version: ReviewVersion;
  versions?: ReviewVersion[];
  issues: ReviewIssue[];
  historicalIssues: ReviewIssue[];
  selectedIssueId?: string;
  isCurrentVersion: boolean;
  playbackPending?: boolean;
  playbackError?: string | null;
  readonlyReason?: string;
  pending?: boolean;
  onSubmittingChange?(submitting: boolean): void;
  onCreateIssue(body: string): Promise<void>;
  onSelectIssue(issue: ReviewIssue): void;
  onEditIssue(issue: ReviewIssue, body: string): Promise<void>;
  onReplyIssue(issue: ReviewIssue, body: string): Promise<void>;
  onResolve(issue: ReviewIssue): void;
  onReopen(issue: ReviewIssue): void;
  onDeleteIssue(issue: ReviewIssue): void;
}

export interface IssueCardProps {
  issue: ReviewIssue;
  version: ReviewVersion;
  selected?: boolean;
  readonlyReason?: string;
  showReadonlyReason?: boolean;
  pending?: boolean;
  entryMode: EntryMode;
  onSubmittingChange?(submitting: boolean): void;
  onSelect(issue: ReviewIssue): void;
  onEdit(issue: ReviewIssue, body: string): Promise<void>;
  onReply(issue: ReviewIssue, body: string): Promise<void>;
  onResolve(issue: ReviewIssue): void;
  onReopen(issue: ReviewIssue): void;
  onDelete(issue: ReviewIssue): void;
}
