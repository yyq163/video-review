import { randomId } from './http-review-transport';

const REVOKE_FINALIZATION_OPERATION_KEY_PREFIX =
  'fj-final-cut-review:revoke-finalization-operation:';
const STORAGE_PROBE_KEY = 'fj-final-cut-review:revoke-finalization-storage-probe';

export const REVOKE_FINALIZATION_RECONCILIATION_INTERVAL_MS = 3_000;
export const REVOKE_FINALIZATION_MAX_CONFIRMATION_ATTEMPTS = 4;
export const REVOKE_FINALIZATION_MAX_REPLAY_ATTEMPTS = 2;
const REVOKE_FINALIZATION_CONFIRMATIONS_PER_REPLAY = 2;

export interface PendingRevokeFinalizationOperation {
  commandId: string;
  lockVersion: number;
}

export interface RevokeFinalizationReconciliationState {
  confirmationAttempts: number;
  replayAttempts: number;
  exhausted: boolean;
}

export interface RevokeFinalizationReconciliationStep {
  state: RevokeFinalizationReconciliationState;
  shouldReplay: boolean;
}

export type RevokeFinalizationProtectionState =
  | 'clear'
  | 'required'
  | 'storage-unavailable';

export class RevokeFinalizationResultUncertainError extends Error {
  readonly cause: unknown;

  constructor(cause: unknown) {
    super('撤回结果不确定，正在持续查询权威状态；禁止创建新的撤回操作。');
    this.name = 'RevokeFinalizationResultUncertainError';
    this.cause = cause;
  }
}

export class RevokeFinalizationProtectionUnavailableError extends Error {
  constructor() {
    super('当前浏览器无法持久保存撤回幂等身份，已阻止撤回；请恢复会话存储后重试。');
    this.name = 'RevokeFinalizationProtectionUnavailableError';
  }
}

export function createRevokeFinalizationReconciliationState(): RevokeFinalizationReconciliationState {
  return {
    confirmationAttempts: 0,
    replayAttempts: 0,
    exhausted: false,
  };
}

export function nextRevokeFinalizationReconciliationStep(
  current: RevokeFinalizationReconciliationState,
  authoritativeFinalized: boolean,
): RevokeFinalizationReconciliationStep {
  if (current.exhausted) {
    return { state: current, shouldReplay: false };
  }
  const confirmationAttempts = current.confirmationAttempts + 1;
  const shouldReplay =
    authoritativeFinalized &&
    confirmationAttempts % REVOKE_FINALIZATION_CONFIRMATIONS_PER_REPLAY === 0 &&
    current.replayAttempts < REVOKE_FINALIZATION_MAX_REPLAY_ATTEMPTS;
  const replayAttempts = current.replayAttempts + (shouldReplay ? 1 : 0);
  return {
    shouldReplay,
    state: {
      confirmationAttempts,
      replayAttempts,
      exhausted:
        confirmationAttempts >= REVOKE_FINALIZATION_MAX_CONFIRMATION_ATTEMPTS,
    },
  };
}

function sessionStorageOrNull(): Storage | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

function operationKey(projectRefId: string, reviewItemId: string): string {
  return `${REVOKE_FINALIZATION_OPERATION_KEY_PREFIX}${encodeURIComponent(projectRefId)}:${encodeURIComponent(reviewItemId)}`;
}

function readOperation(
  projectRefId: string,
  reviewItemId: string,
): { state: 'available'; operation?: PendingRevokeFinalizationOperation } | { state: 'unavailable' } {
  const storage = sessionStorageOrNull();
  if (!storage) return { state: 'unavailable' };
  try {
    storage.setItem(STORAGE_PROBE_KEY, '1');
    storage.removeItem(STORAGE_PROBE_KEY);
    const value = storage.getItem(operationKey(projectRefId, reviewItemId));
    if (!value) return { state: 'available' };
    const parsed = JSON.parse(value) as Partial<PendingRevokeFinalizationOperation>;
    if (
      typeof parsed.commandId !== 'string' ||
      !parsed.commandId.startsWith('RevokeFinalization_') ||
      typeof parsed.lockVersion !== 'number' ||
      !Number.isInteger(parsed.lockVersion) ||
      parsed.lockVersion < 0
    ) {
      return { state: 'unavailable' };
    }
    return {
      state: 'available',
      operation: {
        commandId: parsed.commandId,
        lockVersion: parsed.lockVersion,
      },
    };
  } catch {
    return { state: 'unavailable' };
  }
}

function writeOperation(
  projectRefId: string,
  reviewItemId: string,
  operation: PendingRevokeFinalizationOperation,
): boolean {
  const storage = sessionStorageOrNull();
  if (!storage) return false;
  try {
    const key = operationKey(projectRefId, reviewItemId);
    const serialized = JSON.stringify(operation);
    storage.setItem(key, serialized);
    return storage.getItem(key) === serialized;
  } catch {
    return false;
  }
}

export function getRevokeFinalizationProtectionState(
  projectRefId: string,
  reviewItemId: string,
): RevokeFinalizationProtectionState {
  const result = readOperation(projectRefId, reviewItemId);
  if (result.state === 'unavailable') return 'storage-unavailable';
  return result.operation ? 'required' : 'clear';
}

export function beginRevokeFinalizationOperation(
  projectRefId: string,
  reviewItemId: string,
  lockVersion: number,
): PendingRevokeFinalizationOperation {
  const stored = readOperation(projectRefId, reviewItemId);
  if (stored.state === 'unavailable') {
    throw new RevokeFinalizationProtectionUnavailableError();
  }
  const operation = stored.operation ?? {
    commandId: randomId('RevokeFinalization'),
    lockVersion,
  };
  if (!writeOperation(projectRefId, reviewItemId, operation)) {
    throw new RevokeFinalizationProtectionUnavailableError();
  }
  return operation;
}

export function clearRevokeFinalizationOperation(
  projectRefId: string,
  reviewItemId: string,
): void {
  try {
    sessionStorageOrNull()?.removeItem(operationKey(projectRefId, reviewItemId));
  } catch {
    // An authoritative successful state must not be replaced with a storage cleanup error.
  }
}
