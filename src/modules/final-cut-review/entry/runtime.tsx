import { createContext, useContext, type ReactNode } from 'react';
import type { EntryMode } from '../contracts/types';
import type { ReviewApiPort, ReviewPermissionAdapter } from '../ports';
import { createSeedData } from '../core/seed';
import { InMemoryReviewRepository, type InMemoryReviewRepositorySnapshot } from '../adapters/in-memory-review-repository';
import { MockFileStorageAdapter } from '../adapters/mock-file-storage-adapter';
import { MockFinalizedPackageAdapter } from '../adapters/mock-finalized-package-adapter';
import {
  NoAccountEntryPolicyAdapter,
  NoAccountPermissionAdapter,
  NoAccountPrincipalAuthorizationAdapter,
} from '../adapters/no-account-permission-adapter';
import { SimpleWriteGuardAdapter } from '../adapters/simple-write-guard-adapter';
import { MockReviewApiAdapter } from '../adapters/mock-review-api-adapter';
import { HttpReviewApiAdapter } from '../adapters/http-review-api-adapter';
import { BrowserReviewHostBridge } from '../host/review-host-bridge';
import { FinalCutReviewClient } from '../contracts-generated/backend-contract';

export interface ReviewRuntime {
  getApi(mode: EntryMode): ReviewApiPort;
  permissions: ReviewPermissionAdapter;
  dispose(): void;
}

const ReviewRuntimeContext = createContext<ReviewRuntime | null>(null);

export interface ReviewRuntimeOptions {
  apiBaseUrl?: string;
  persistMockRuntime?: boolean;
}

export interface ReviewRuntimeConfiguration {
  apiBaseUrl?: string;
  runtimeKind: 'http' | 'mock';
}

const MOCK_RUNTIME_STORAGE_KEY = 'fj-final-cut-review:mock-runtime:v1';

function normalizeBaseUrl(baseUrl: string): string {
  return baseUrl.replace(/\/+$/, '');
}

export function resolveReviewRuntimeConfiguration(
  apiBaseUrl: string | undefined,
  isProduction: boolean,
): ReviewRuntimeConfiguration {
  const normalizedApiBaseUrl = apiBaseUrl?.trim() ?? '';
  if (!normalizedApiBaseUrl && isProduction) {
    throw new Error('VITE_FINAL_CUT_REVIEW_API_BASE_URL is required for production runtime');
  }
  return normalizedApiBaseUrl
    ? { apiBaseUrl: normalizedApiBaseUrl, runtimeKind: 'http' }
    : { runtimeKind: 'mock' };
}

function canUseBrowserStorage(): boolean {
  return typeof window !== 'undefined' && typeof window.localStorage !== 'undefined';
}

function loadMockSnapshot(): InMemoryReviewRepositorySnapshot | null {
  if (!canUseBrowserStorage()) return null;
  try {
    const raw = window.localStorage.getItem(MOCK_RUNTIME_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as InMemoryReviewRepositorySnapshot;
    if (!Array.isArray(parsed.projects) || !Array.isArray(parsed.items) || !Array.isArray(parsed.versions)) {
      return null;
    }
    return {
      projects: parsed.projects,
      items: parsed.items,
      versions: parsed.versions,
      issues: Array.isArray(parsed.issues) ? parsed.issues : [],
      finalizations: Array.isArray(parsed.finalizations) ? parsed.finalizations : [],
    };
  } catch {
    return null;
  }
}

function saveMockSnapshot(repository: InMemoryReviewRepository): void {
  if (!canUseBrowserStorage()) return;
  try {
    window.localStorage.setItem(MOCK_RUNTIME_STORAGE_KEY, JSON.stringify(repository.snapshot()));
  } catch (error) {
    console.warn('Failed to persist mock review runtime state.', error);
  }
}

export function createReviewRuntime(options: ReviewRuntimeOptions = {}): ReviewRuntime {
  const permissions = new NoAccountPermissionAdapter();
  const entryPolicy = new NoAccountEntryPolicyAdapter();
  const authorization = new NoAccountPrincipalAuthorizationAdapter(permissions);
  const apis = new Map<EntryMode, ReviewApiPort>();
  const apiBaseUrl = options.apiBaseUrl?.trim();
  let disposeModeResources = () => {};

  if (apiBaseUrl) {
    const baseUrl = normalizeBaseUrl(apiBaseUrl);
    const client = new FinalCutReviewClient(baseUrl);
    for (const mode of ['edit', 'review'] as const) {
      apis.set(
        mode,
        new HttpReviewApiAdapter(mode, client, entryPolicy, baseUrl, new BrowserReviewHostBridge(mode, authorization)),
      );
    }
  } else {
    const seed = createSeedData();
    const persistMockRuntime = Boolean(options.persistMockRuntime);
    const fileStorage = new MockFileStorageAdapter({ persistInBrowser: persistMockRuntime });
    const packageAdapter = new MockFinalizedPackageAdapter();
    const writeGuard = new SimpleWriteGuardAdapter(permissions);
    const seededFiles = seed.files.map((file) => fileStorage.seedOriginal(file));
    const playbackByFileId = new Map(seededFiles.map((file) => [file.originalFileId, file.playbackUrl]));
    for (const version of seed.versions) {
      version.playbackUrl = playbackByFileId.get(version.originalFileId) ?? '';
    }
    const snapshot = persistMockRuntime ? loadMockSnapshot() : null;
    const repository = new InMemoryReviewRepository(snapshot ?? seed, {
      onChange: persistMockRuntime ? () => saveMockSnapshot(repository) : undefined,
    });
    const hydrateMockRuntime = persistMockRuntime
      ? fileStorage.whenReady().then(() => {
          repository.relinkOriginalFiles(fileStorage.getCachedOriginals());
        })
      : Promise.resolve();

    for (const mode of ['edit', 'review'] as const) {
      apis.set(
        mode,
        new MockReviewApiAdapter(
          mode,
          repository,
          fileStorage,
          packageAdapter,
          writeGuard,
          authorization,
          entryPolicy,
          new BrowserReviewHostBridge(mode, authorization),
          () => hydrateMockRuntime,
        ),
      );
    }

    const revokeMockUrls = () => fileStorage.revokeManagedUrls();
    if (typeof window !== 'undefined') {
      window.addEventListener('beforeunload', revokeMockUrls, { once: true });
    }
    disposeModeResources = () => {
      if (typeof window !== 'undefined') {
        window.removeEventListener('beforeunload', revokeMockUrls);
      }
      fileStorage.revokeManagedUrls();
    };
  }

  return {
    permissions,
    getApi(mode) {
      const api = apis.get(mode);
      if (!api) throw new Error(`Unknown entry mode ${mode}`);
      return api;
    },
    dispose() {
      disposeModeResources();
    },
  };
}

export function ReviewRuntimeProvider(props: { runtime: ReviewRuntime; children: ReactNode }) {
  return <ReviewRuntimeContext.Provider value={props.runtime}>{props.children}</ReviewRuntimeContext.Provider>;
}

export function useReviewRuntime(): ReviewRuntime {
  const runtime = useContext(ReviewRuntimeContext);
  if (!runtime) throw new Error('ReviewRuntimeProvider is missing');
  return runtime;
}

export function useReviewApi(mode: EntryMode): ReviewApiPort {
  return useReviewRuntime().getApi(mode);
}
