import type { StoredOriginalFile } from '../contracts/types';
import { createSeedData } from '../core/seed';
import { InMemoryReviewFinalizations } from './in-memory-review-finalizations';
import { InMemoryReviewIssues } from './in-memory-review-issues';
import { InMemoryReviewItems } from './in-memory-review-items';
import { InMemoryReviewProjects } from './in-memory-review-projects';
import { InMemoryReviewQueries } from './in-memory-review-queries';
import { InMemoryReviewStore } from './in-memory-review-store';
import type {
  InMemoryReviewRepositoryOptions,
  InMemoryReviewRepositorySnapshot,
} from './in-memory-review-types';

export type {
  InMemoryReviewRepositoryOptions,
  InMemoryReviewRepositorySnapshot,
} from './in-memory-review-types';

export class InMemoryReviewRepository {
  readonly snapshot: InMemoryReviewStore['snapshot'];
  readonly relinkOriginalFiles: InMemoryReviewStore['relinkOriginalFiles'];
  readonly listProjects: InMemoryReviewQueries['listProjects'];
  readonly getProjectDetail: InMemoryReviewQueries['getProjectDetail'];
  readonly getWorkspace: InMemoryReviewQueries['getWorkspace'];
  readonly getActiveFinalizations: InMemoryReviewQueries['getActiveFinalizations'];
  readonly getAllProjectVersions: InMemoryReviewQueries['getAllProjectVersions'];
  readonly getAllProjectItems: InMemoryReviewQueries['getAllProjectItems'];
  readonly createProject: InMemoryReviewProjects['createProject'];
  readonly updateProject: InMemoryReviewProjects['updateProject'];
  readonly archiveProject: InMemoryReviewProjects['archiveProject'];
  readonly restoreProject: InMemoryReviewProjects['restoreProject'];
  readonly deleteProject: InMemoryReviewProjects['deleteProject'];
  readonly ensureProjectWritable: InMemoryReviewProjects['ensureProjectWritable'];
  readonly ensureProjectNotDeleted: InMemoryReviewProjects['ensureProjectNotDeleted'];
  readonly createReviewItemWithVersion: InMemoryReviewItems['createReviewItemWithVersion'];
  readonly updateReviewItem: InMemoryReviewItems['updateReviewItem'];
  readonly deleteReviewItem: InMemoryReviewItems['deleteReviewItem'];
  readonly appendVersion: InMemoryReviewItems['appendVersion'];
  readonly startReview: InMemoryReviewItems['startReview'];
  readonly ensureAppendVersionWritable: InMemoryReviewItems['ensureAppendVersionWritable'];
  readonly createIssue: InMemoryReviewIssues['createIssue'];
  readonly editIssue: InMemoryReviewIssues['editIssue'];
  readonly replyToIssue: InMemoryReviewIssues['replyToIssue'];
  readonly setIssueStatus: InMemoryReviewIssues['setIssueStatus'];
  readonly deleteIssue: InMemoryReviewIssues['deleteIssue'];
  readonly requestChanges: InMemoryReviewFinalizations['requestChanges'];
  readonly finalizeCurrentVersion: InMemoryReviewFinalizations['finalizeCurrentVersion'];

  constructor(
    seed: Partial<InMemoryReviewRepositorySnapshot> = createSeedData(),
    options: InMemoryReviewRepositoryOptions = {},
  ) {
    const store = new InMemoryReviewStore(seed, options);
    const queries = new InMemoryReviewQueries(store);
    const projects = new InMemoryReviewProjects(store);
    const items = new InMemoryReviewItems(store);
    const issues = new InMemoryReviewIssues(store);
    const finalizations = new InMemoryReviewFinalizations(store);

    this.snapshot = store.snapshot;
    this.relinkOriginalFiles = store.relinkOriginalFiles;
    this.listProjects = queries.listProjects;
    this.getProjectDetail = queries.getProjectDetail;
    this.getWorkspace = queries.getWorkspace;
    this.getActiveFinalizations = queries.getActiveFinalizations;
    this.getAllProjectVersions = queries.getAllProjectVersions;
    this.getAllProjectItems = queries.getAllProjectItems;
    this.createProject = projects.createProject;
    this.updateProject = projects.updateProject;
    this.archiveProject = projects.archiveProject;
    this.restoreProject = projects.restoreProject;
    this.deleteProject = projects.deleteProject;
    this.ensureProjectWritable = projects.ensureProjectWritable;
    this.ensureProjectNotDeleted = projects.ensureProjectNotDeleted;
    this.createReviewItemWithVersion = items.createReviewItemWithVersion;
    this.updateReviewItem = items.updateReviewItem;
    this.deleteReviewItem = items.deleteReviewItem;
    this.appendVersion = items.appendVersion;
    this.startReview = items.startReview;
    this.ensureAppendVersionWritable = items.ensureAppendVersionWritable;
    this.createIssue = issues.createIssue;
    this.editIssue = issues.editIssue;
    this.replyToIssue = issues.replyToIssue;
    this.setIssueStatus = issues.setIssueStatus;
    this.deleteIssue = issues.deleteIssue;
    this.requestChanges = finalizations.requestChanges;
    this.finalizeCurrentVersion = finalizations.finalizeCurrentVersion;
  }

  static seededFiles(): StoredOriginalFile[] {
    return createSeedData().files;
  }
}
