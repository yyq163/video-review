import type {
  FinalizationRecord,
  Project,
  ReviewIssue,
  ReviewItem,
  ReviewVersion,
  ResponsibleEditor,
} from '../contracts/types';
import type { TechnicalQcProgress } from '../ports';

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
  technicalQcProgressByProject?: Record<string, TechnicalQcProgress>;
}
