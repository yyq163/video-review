import { beforeEach, describe, expect, it, vi } from 'vitest';

const mockResources = vi.hoisted(() => ({
  createSeedData: vi.fn(() => ({
    projects: [],
    items: [],
    versions: [],
    issues: [],
    finalizations: [],
    files: [],
  })),
  fileStorageConstructed: vi.fn(),
  packageAdapterConstructed: vi.fn(),
  repositoryConstructed: vi.fn(),
  mockApiConstructed: vi.fn(),
  revokeManagedUrls: vi.fn(),
}));

vi.mock('../core/seed', () => ({
  createSeedData: mockResources.createSeedData,
}));

vi.mock('../adapters/mock-file-storage-adapter', () => ({
  MockFileStorageAdapter: class {
    constructor() {
      mockResources.fileStorageConstructed();
    }

    seedOriginal(file: unknown) {
      return file;
    }

    whenReady() {
      return Promise.resolve();
    }

    getCachedOriginals() {
      return [];
    }

    revokeManagedUrls() {
      mockResources.revokeManagedUrls();
    }
  },
}));

vi.mock('../adapters/mock-finalized-package-adapter', () => ({
  MockFinalizedPackageAdapter: class {
    constructor() {
      mockResources.packageAdapterConstructed();
    }
  },
}));

vi.mock('../adapters/in-memory-review-repository', () => ({
  InMemoryReviewRepository: class {
    constructor() {
      mockResources.repositoryConstructed();
    }

    snapshot() {
      return { projects: [], items: [], versions: [], issues: [], finalizations: [] };
    }

    relinkOriginalFiles() {}
  },
}));

vi.mock('../adapters/mock-review-api-adapter', () => ({
  MockReviewApiAdapter: class {
    constructor() {
      mockResources.mockApiConstructed();
    }
  },
}));

import { createReviewRuntime, resolveReviewRuntimeConfiguration } from './runtime';

describe('resolveReviewRuntimeConfiguration', () => {
  it('trims a configured HTTP API base URL', () => {
    expect(resolveReviewRuntimeConfiguration('  https://review.example/api/  ', true)).toEqual({
      apiBaseUrl: 'https://review.example/api/',
      runtimeKind: 'http',
    });
  });

  it.each([undefined, '', '   \t\n'])('rejects a missing or blank production API base URL', (apiBaseUrl) => {
    expect(() => resolveReviewRuntimeConfiguration(apiBaseUrl, true)).toThrow(
      'VITE_FINAL_CUT_REVIEW_API_BASE_URL is required for production runtime',
    );
  });

  it('allows a blank API base URL only for the explicit development mock runtime', () => {
    expect(resolveReviewRuntimeConfiguration('   ', false)).toEqual({ runtimeKind: 'mock' });
  });
});

describe('createReviewRuntime resource isolation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('does not initialize or dispose mock resources in HTTP mode', () => {
    const addEventListener = vi.spyOn(window, 'addEventListener');
    const removeEventListener = vi.spyOn(window, 'removeEventListener');

    const runtime = createReviewRuntime({
      apiBaseUrl: 'https://review.example/',
      persistMockRuntime: true,
    });
    runtime.dispose();

    expect(mockResources.createSeedData).not.toHaveBeenCalled();
    expect(mockResources.fileStorageConstructed).not.toHaveBeenCalled();
    expect(mockResources.packageAdapterConstructed).not.toHaveBeenCalled();
    expect(mockResources.repositoryConstructed).not.toHaveBeenCalled();
    expect(mockResources.mockApiConstructed).not.toHaveBeenCalled();
    expect(mockResources.revokeManagedUrls).not.toHaveBeenCalled();
    expect(addEventListener).not.toHaveBeenCalledWith('beforeunload', expect.any(Function), expect.anything());
    expect(removeEventListener).not.toHaveBeenCalledWith('beforeunload', expect.any(Function));
  });

  it('initializes mock resources only when no API base URL is configured', () => {
    const runtime = createReviewRuntime();

    expect(mockResources.createSeedData).toHaveBeenCalledOnce();
    expect(mockResources.fileStorageConstructed).toHaveBeenCalledOnce();
    expect(mockResources.packageAdapterConstructed).toHaveBeenCalledOnce();
    expect(mockResources.repositoryConstructed).toHaveBeenCalledOnce();
    expect(mockResources.mockApiConstructed).toHaveBeenCalledTimes(2);

    runtime.dispose();
    expect(mockResources.revokeManagedUrls).toHaveBeenCalledOnce();
  });
});
