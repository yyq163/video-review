import type {
  FinalizationRecord,
  Project,
  ReviewIssue,
  ReviewItem,
  ReviewVersion,
  ResponsibleEditor,
} from '../contracts/types';

export interface InMemoryReviewRepositorySnapshot {
  projects: Project[];
  items: ReviewItem[];
  versions: ReviewVersion[];
  issues: ReviewIssue[];
  finalizations: FinalizationRecord[];
  responsibleEditors?: ResponsibleEditor[];
}

export interface InMemoryReviewRepositoryOptions {
  onChange?: () => void;
}
