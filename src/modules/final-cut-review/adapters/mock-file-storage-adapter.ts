import type { FileStoragePort } from '../ports';
import type { StoredOriginalFile } from '../contracts/types';
import { sha256Hex } from '../core/sha256';
import { ReviewDomainError } from '../core/errors';
import { sanitizeDownloadFileName } from '../core/file-names';
import { createUuid } from '../core/uuid';

const DB_NAME = 'fj-final-cut-review-mock-runtime';
const DB_VERSION = 1;
const ORIGINALS_STORE = 'originals';

interface PersistedOriginalFileRecord {
  originalFileId: string;
  fileName: string;
  mimeType: string;
  size: number;
  sha256: string;
  durationMs: number;
  width: number;
  height: number;
  fpsNum: number;
  fpsDen: number;
  blob: Blob;
}

export interface MockFileStorageAdapterOptions {
  persistInBrowser?: boolean;
}

function createPlaybackUrl(blob: Blob): string {
  if (typeof URL.createObjectURL === 'function') {
    return URL.createObjectURL(blob);
  }
  return `blob:mock-${createUuid()}`;
}

export class MockFileStorageAdapter implements FileStoragePort {
  private readonly originals = new Map<string, StoredOriginalFile>();
  private readonly managedUrls = new Set<string>();
  private readonly readyPromise: Promise<void>;

  constructor(private readonly options: MockFileStorageAdapterOptions = {}) {
    this.readyPromise = options.persistInBrowser ? this.hydratePersistedOriginals() : Promise.resolve();
  }

  whenReady(): Promise<void> {
    return this.readyPromise;
  }

  getCachedOriginals(): StoredOriginalFile[] {
    return [...this.originals.values()].map((record) => ({ ...record }));
  }

  async storeOriginal(file: File): Promise<StoredOriginalFile> {
    await this.readyPromise;
    const blob = file;
    const safeName = sanitizeDownloadFileName(file.name || 'untitled.mp4', 'untitled.mp4');
    const sha256 = await sha256Hex(`${safeName}:${file.type || 'video/mp4'}:${file.size}:${file.lastModified}`);
    const record: StoredOriginalFile = {
      originalFileId: `file_${createUuid()}`,
      fileName: safeName,
      mimeType: file.type || 'video/mp4',
      size: file.size,
      sha256,
      durationMs: 480,
      width: 1280,
      height: 720,
      fpsNum: 25,
      fpsDen: 1,
      playbackUrl: this.createManagedPlaybackUrl(blob),
      blob,
    };
    this.originals.set(record.originalFileId, record);
    await this.persistOriginal(record);
    return record;
  }

  async getOriginal(originalFileId: string): Promise<StoredOriginalFile> {
    await this.readyPromise;
    const record = this.originals.get(originalFileId);
    if (!record) {
      throw new ReviewDomainError('原片文件不存在或已越界', 'FILE_NOT_FOUND');
    }
    return record;
  }

  seedOriginal(record: StoredOriginalFile): StoredOriginalFile {
    if (!record.blob) throw new ReviewDomainError('原片文件内容缺失', 'FILE_NOT_FOUND');
    const playbackUrl = record.playbackUrl || this.createManagedPlaybackUrl(record.blob);
    const seeded = { ...record, playbackUrl };
    this.originals.set(record.originalFileId, seeded);
    return seeded;
  }

  revokeManagedUrls(): void {
    for (const url of this.managedUrls) {
      URL.revokeObjectURL(url);
    }
    this.managedUrls.clear();
  }

  private createManagedPlaybackUrl(blob: Blob): string {
    const playbackUrl = createPlaybackUrl(blob);
    if (playbackUrl.startsWith('blob:')) {
      this.managedUrls.add(playbackUrl);
    }
    return playbackUrl;
  }

  private async hydratePersistedOriginals(): Promise<void> {
    if (!this.canUseIndexedDb()) return;
    try {
      const db = await this.openDatabase();
      const records = await this.readPersistedRecords(db);
      for (const persisted of records) {
        this.originals.set(persisted.originalFileId, {
          ...persisted,
          playbackUrl: this.createManagedPlaybackUrl(persisted.blob),
        });
      }
    } catch (error) {
      console.warn('Failed to hydrate mock originals from IndexedDB.', error);
    }
  }

  private async persistOriginal(record: StoredOriginalFile): Promise<void> {
    if (!this.options.persistInBrowser || !this.canUseIndexedDb()) return;
    if (!record.blob) throw new ReviewDomainError('原片文件内容缺失', 'FILE_NOT_FOUND');
    try {
      const db = await this.openDatabase();
      const persisted: PersistedOriginalFileRecord = {
        originalFileId: record.originalFileId,
        fileName: record.fileName,
        mimeType: record.mimeType,
        size: record.size,
        sha256: record.sha256,
        durationMs: record.durationMs,
        width: record.width,
        height: record.height,
        fpsNum: record.fpsNum,
        fpsDen: record.fpsDen,
        blob: record.blob,
      };
      await this.putPersistedRecord(db, persisted);
    } catch {
      throw new ReviewDomainError('浏览器无法持久化原片文件', 'FILE_PERSISTENCE_FAILED');
    }
  }

  private canUseIndexedDb(): boolean {
    return typeof indexedDB !== 'undefined';
  }

  private openDatabase(): Promise<IDBDatabase> {
    return new Promise((resolve, reject) => {
      const request = indexedDB.open(DB_NAME, DB_VERSION);
      request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains(ORIGINALS_STORE)) {
          db.createObjectStore(ORIGINALS_STORE, { keyPath: 'originalFileId' });
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  }

  private readPersistedRecords(db: IDBDatabase): Promise<PersistedOriginalFileRecord[]> {
    return new Promise((resolve, reject) => {
      const transaction = db.transaction(ORIGINALS_STORE, 'readonly');
      const request = transaction.objectStore(ORIGINALS_STORE).getAll();
      request.onsuccess = () => resolve(request.result as PersistedOriginalFileRecord[]);
      request.onerror = () => reject(request.error);
      transaction.onerror = () => reject(transaction.error);
    });
  }

  private putPersistedRecord(db: IDBDatabase, record: PersistedOriginalFileRecord): Promise<void> {
    return new Promise((resolve, reject) => {
      const transaction = db.transaction(ORIGINALS_STORE, 'readwrite');
      const request = transaction.objectStore(ORIGINALS_STORE).put(record);
      request.onerror = () => reject(request.error);
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(transaction.error);
    });
  }
}
