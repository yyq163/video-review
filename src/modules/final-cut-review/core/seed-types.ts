import type {
  FinalizationRecord,
  Project,
  ReviewIssue,
  ReviewItem,
  ReviewVersion,
  StoredOriginalFile,
} from '../contracts/types';

export interface SeedData {
  projects: Project[];
  items: ReviewItem[];
  versions: ReviewVersion[];
  issues: ReviewIssue[];
  finalizations: FinalizationRecord[];
  files: StoredOriginalFile[];
}
