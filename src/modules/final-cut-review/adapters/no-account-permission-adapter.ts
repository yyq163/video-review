import { getCapabilities } from '../entry/capabilities';
import type { Capability, EntryMode, ExecutionContext } from '../contracts/types';
import { ReviewDomainError } from '../core/errors';
import { createUuid } from '../core/uuid';
import type { EntryPolicyPort, PrincipalAuthorizationPort, ReviewPermissionAdapter } from '../ports';

export class NoAccountPermissionAdapter implements ReviewPermissionAdapter {
  can(mode: EntryMode, capability: Capability): boolean {
    return getCapabilities(mode).has(capability);
  }

  list(mode: EntryMode): Capability[] {
    return [...getCapabilities(mode)];
  }
}

export class NoAccountEntryPolicyAdapter implements EntryPolicyPort {
  canEnter(mode: EntryMode): boolean {
    return mode === 'edit' || mode === 'review';
  }

  createContext(mode: EntryMode): ExecutionContext {
    if (!this.canEnter(mode)) {
      throw new ReviewDomainError('入口不存在', 'ENTRY_NOT_FOUND');
    }
    return {
      entryMode: mode,
      requestId: createUuid(),
      createdAt: new Date().toISOString(),
    };
  }
}

export class NoAccountPrincipalAuthorizationAdapter implements PrincipalAuthorizationPort {
  constructor(private readonly permissions: ReviewPermissionAdapter) {}

  assertAuthorized(context: ExecutionContext, capability: Capability): void {
    if (!this.permissions.can(context.entryMode, capability)) {
      throw new ReviewDomainError(`当前入口不允许执行 ${capability}`, 'CAPABILITY_DENIED');
    }
  }
}
