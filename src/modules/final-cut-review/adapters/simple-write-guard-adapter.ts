import type { Capability, EntryMode, ProjectRefId, ReviewItemId, VersionId } from '../contracts/types';
import { ReviewDomainError } from '../core/errors';
import type { ReviewPermissionAdapter, WriteGuardPort } from '../ports';

export class SimpleWriteGuardAdapter implements WriteGuardPort {
  constructor(private readonly permissions: ReviewPermissionAdapter) {}

  assertCapability(mode: EntryMode, capability: Capability): void {
    if (!this.permissions.can(mode, capability)) {
      throw new ReviewDomainError(`当前入口不允许执行 ${capability}`, 'CAPABILITY_DENIED');
    }
  }

  assertSameProject(expected: ProjectRefId, actual: ProjectRefId, label: string): void {
    if (expected !== actual) {
      throw new ReviewDomainError(`${label} 不属于当前项目`, 'PROJECT_SCOPE_MISMATCH');
    }
  }

  assertSameItem(expected: ReviewItemId, actual: ReviewItemId, label: string): void {
    if (expected !== actual) {
      throw new ReviewDomainError(`${label} 不属于当前成片`, 'ITEM_SCOPE_MISMATCH');
    }
  }

  assertSameVersion(expected: VersionId, actual: VersionId, label: string): void {
    if (expected !== actual) {
      throw new ReviewDomainError(`${label} 不属于当前版本`, 'VERSION_SCOPE_MISMATCH');
    }
  }
}
