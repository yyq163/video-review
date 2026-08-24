import type {
  FinalizationRecord,
  Project,
  ReviewIssue,
  ReviewItem,
  ReviewVersion,
  ResponsibleEditor,
  StoredOriginalFile,
} from '../contracts/types';

export interface SeedData {
  projects: Project[];
  items: ReviewItem[];
  versions: ReviewVersion[];
  issues: ReviewIssue[];
  finalizations: FinalizationRecord[];
  responsibleEditors: ResponsibleEditor[];
  files: StoredOriginalFile[];
}
