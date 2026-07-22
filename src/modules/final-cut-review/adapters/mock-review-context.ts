import type { EntryMode, ExecutionContext } from '../contracts/types';
import { ReviewDomainError } from '../core/errors';
import type {
  FileStoragePort,
  FinalizedPackagePort,
  PrincipalAuthorizationPort,
  ReviewHostBridge,
  WriteGuardPort,
} from '../ports';
import type { InMemoryReviewRepository } from './in-memory-review-repository';

export class MockReviewContext {
  constructor(
    readonly mode: EntryMode,
    readonly repository: InMemoryReviewRepository,
    readonly fileStorage: FileStoragePort,
    readonly packageAdapter: FinalizedPackagePort,
    private readonly writeGuard: WriteGuardPort,
    private readonly authorization: PrincipalAuthorizationPort,
    readonly hostBridge: ReviewHostBridge,
    private readonly beforeOperation: (() => Promise<void>) | undefined,
  ) {}

  async ready(): Promise<void> {
    await this.beforeOperation?.();
  }

  throwIfAborted(signal?: AbortSignal): void {
    if (signal?.aborted) {
      throw new DOMException('旧请求已取消', 'AbortError');
    }
  }

  assertContext(
    context: ExecutionContext,
    capability: Parameters<PrincipalAuthorizationPort['assertAuthorized']>[1],
  ): void {
    if (context.entryMode !== this.mode) {
      throw new ReviewDomainError('执行上下文与入口不匹配', 'EXECUTION_CONTEXT_MISMATCH');
    }
    this.writeGuard.assertCapability(this.mode, capability);
    this.authorization.assertAuthorized(context, capability);
  }
}
