import type {
  FinalizationRecord,
  Project,
  ReviewIssue,
  ReviewItem,
  ReviewVersion,
} from '../contracts/types';

export interface InMemoryReviewRepositorySnapshot {
  projects: Project[];
  items: ReviewItem[];
  versions: ReviewVersion[];
  issues: ReviewIssue[];
  finalizations: FinalizationRecord[];
}

export interface InMemoryReviewRepositoryOptions {
  onChange?: () => void;
}
