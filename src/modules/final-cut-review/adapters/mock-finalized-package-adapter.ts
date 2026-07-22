import type { FinalizedPackagePort } from '../ports';
import type { PackageResult } from '../contracts/types';
import { sanitizeDownloadFileName, sanitizeFileSegment } from '../core/file-names';
import { ReviewDomainError } from '../core/errors';
import { createUuid } from '../core/uuid';

const ZIP32_MAX = 0xffffffff;
const ZIP_UTF8_FLAG = 0x0800;
const ZIP_STORE_METHOD = 0;

interface ZipEntrySource {
  name: string;
  blob: Blob;
}

interface ZipEntry {
  nameBytes: Uint8Array;
  blob: Blob;
  size: number;
  crc32: number;
}

function writeUint16(view: DataView, offset: number, value: number): void {
  view.setUint16(offset, value, true);
}

function writeUint32(view: DataView, offset: number, value: number): void {
  view.setUint32(offset, value >>> 0, true);
}

function bytesBlobPart(bytes: Uint8Array): ArrayBuffer {
  const copy = new ArrayBuffer(bytes.byteLength);
  new Uint8Array(copy).set(bytes);
  return copy;
}

function createCrc32Table(): Uint32Array {
  const table = new Uint32Array(256);
  for (let index = 0; index < 256; index += 1) {
    let value = index;
    for (let bit = 0; bit < 8; bit += 1) {
      value = value & 1 ? 0xedb88320 ^ (value >>> 1) : value >>> 1;
    }
    table[index] = value >>> 0;
  }
  return table;
}

const crc32Table = createCrc32Table();

function updateCrc32(crc: number, bytes: Uint8Array): number {
  let next = crc;
  for (const byte of bytes) {
    next = crc32Table[(next ^ byte) & 0xff] ^ (next >>> 8);
  }
  return next >>> 0;
}

async function crc32Blob(blob: Blob): Promise<number> {
  let crc = 0xffffffff;
  if (typeof blob.stream === 'function') {
    const reader = blob.stream().getReader();
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        crc = updateCrc32(crc, value);
      }
    } catch {
      throw new ReviewDomainError('原片文件无法读取，不能生成定稿包', 'PACKAGE_SOURCE_UNREADABLE');
    } finally {
      reader.releaseLock();
    }
    return (crc ^ 0xffffffff) >>> 0;
  }

  let buffer: ArrayBuffer;
  if (typeof blob.arrayBuffer === 'function') {
    buffer = await blob.arrayBuffer();
  } else if (typeof FileReader !== 'undefined') {
    buffer = await new Promise<ArrayBuffer>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result as ArrayBuffer);
      reader.onerror = () => reject(reader.error);
      reader.readAsArrayBuffer(blob);
    });
  } else {
    throw new ReviewDomainError('当前运行环境无法读取原片，不能生成定稿包', 'PACKAGE_SOURCE_UNREADABLE');
  }
  const bytes = new Uint8Array(buffer);
  return (updateCrc32(crc, bytes) ^ 0xffffffff) >>> 0;
}

function dosDateTime(date: Date): { time: number; date: number } {
  const year = Math.max(date.getFullYear(), 1980);
  return {
    time: (date.getHours() << 11) | (date.getMinutes() << 5) | Math.floor(date.getSeconds() / 2),
    date: ((year - 1980) << 9) | ((date.getMonth() + 1) << 5) | date.getDate(),
  };
}

function assertZip32Size(value: number, label: string): void {
  if (!Number.isFinite(value) || value < 0 || value > ZIP32_MAX) {
    throw new ReviewDomainError(`${label} 超出浏览器 mock ZIP32 打包上限`, 'PACKAGE_SIZE_LIMIT_EXCEEDED');
  }
}

function localHeader(nameBytes: Uint8Array, size: number, crc32: number, dateTime: { time: number; date: number }): Uint8Array {
  const header = new Uint8Array(30 + nameBytes.length);
  const view = new DataView(header.buffer);
  writeUint32(view, 0, 0x04034b50);
  writeUint16(view, 4, 20);
  writeUint16(view, 6, ZIP_UTF8_FLAG);
  writeUint16(view, 8, ZIP_STORE_METHOD);
  writeUint16(view, 10, dateTime.time);
  writeUint16(view, 12, dateTime.date);
  writeUint32(view, 14, crc32);
  writeUint32(view, 18, size);
  writeUint32(view, 22, size);
  writeUint16(view, 26, nameBytes.length);
  writeUint16(view, 28, 0);
  header.set(nameBytes, 30);
  return header;
}

function centralDirectoryHeader(
  nameBytes: Uint8Array,
  size: number,
  crc32: number,
  localOffset: number,
  dateTime: { time: number; date: number },
): Uint8Array {
  const header = new Uint8Array(46 + nameBytes.length);
  const view = new DataView(header.buffer);
  writeUint32(view, 0, 0x02014b50);
  writeUint16(view, 4, 20);
  writeUint16(view, 6, 20);
  writeUint16(view, 8, ZIP_UTF8_FLAG);
  writeUint16(view, 10, ZIP_STORE_METHOD);
  writeUint16(view, 12, dateTime.time);
  writeUint16(view, 14, dateTime.date);
  writeUint32(view, 16, crc32);
  writeUint32(view, 20, size);
  writeUint32(view, 24, size);
  writeUint16(view, 28, nameBytes.length);
  writeUint16(view, 30, 0);
  writeUint16(view, 32, 0);
  writeUint16(view, 34, 0);
  writeUint16(view, 36, 0);
  writeUint32(view, 38, 0);
  writeUint32(view, 42, localOffset);
  header.set(nameBytes, 46);
  return header;
}

function endOfCentralDirectory(entryCount: number, centralSize: number, centralOffset: number): Uint8Array {
  const header = new Uint8Array(22);
  const view = new DataView(header.buffer);
  writeUint32(view, 0, 0x06054b50);
  writeUint16(view, 4, 0);
  writeUint16(view, 6, 0);
  writeUint16(view, 8, entryCount);
  writeUint16(view, 10, entryCount);
  writeUint32(view, 12, centralSize);
  writeUint32(view, 16, centralOffset);
  writeUint16(view, 20, 0);
  return header;
}

async function buildZipEntries(sources: ZipEntrySource[]): Promise<ZipEntry[]> {
  const encoder = new TextEncoder();
  const entries: ZipEntry[] = [];
  for (const source of sources) {
    const nameBytes = encoder.encode(source.name);
    const size = source.blob.size;
    assertZip32Size(size, source.name);
    entries.push({
      nameBytes,
      blob: source.blob,
      size,
      crc32: await crc32Blob(source.blob),
    });
  }
  return entries;
}

async function createStoredZipBlob(sources: ZipEntrySource[]): Promise<Blob> {
  const zipEntries = await buildZipEntries(sources);
  const dateTime = dosDateTime(new Date());
  const parts: BlobPart[] = [];
  const centralParts: Uint8Array[] = [];
  let offset = 0;

  for (const entry of zipEntries) {
    assertZip32Size(offset, 'ZIP local header offset');
    const header = localHeader(entry.nameBytes, entry.size, entry.crc32, dateTime);
    parts.push(bytesBlobPart(header), entry.blob);
    centralParts.push(centralDirectoryHeader(entry.nameBytes, entry.size, entry.crc32, offset, dateTime));
    offset += header.byteLength + entry.size;
    assertZip32Size(offset, 'ZIP data offset');
  }

  const centralOffset = offset;
  let centralSize = 0;
  for (const centralPart of centralParts) {
    parts.push(bytesBlobPart(centralPart));
    centralSize += centralPart.byteLength;
  }
  assertZip32Size(centralSize, 'ZIP central directory size');
  assertZip32Size(centralOffset + centralSize, 'ZIP total size');
  parts.push(bytesBlobPart(endOfCentralDirectory(zipEntries.length, centralSize, centralOffset)));
  return new Blob(parts, { type: 'application/zip' });
}

export class MockFinalizedPackageAdapter implements FinalizedPackagePort {
  async createProjectPackage(input: Parameters<FinalizedPackagePort['createProjectPackage']>[0]): Promise<PackageResult> {
    const entries: PackageResult['entries'] = [];
    const sources: ZipEntrySource[] = [];
    const versionsById = new Map(input.versions.map((version) => [version.versionId, version]));
    const itemsById = new Map(input.items.map((item) => [item.reviewItemId, item]));

    for (const finalization of input.finalizations) {
      const item = itemsById.get(finalization.reviewItemId);
      const version = versionsById.get(finalization.versionId);
      if (!item || !version || item.activeFinalizationId !== finalization.finalizationId) {
        continue;
      }
      const original = await input.fileStorage.getOriginal(finalization.originalFileId);
      const projectSegment = sanitizeFileSegment(input.project.code, 'project');
      const itemSegment = sanitizeFileSegment(`${item.episode}-${item.title}`, 'item');
      const fileSegment = sanitizeDownloadFileName(`${version.label}-${original.fileName}`, `${version.label}-original.mp4`);
      const entryName = `${projectSegment}/${itemSegment}/${fileSegment}`;
      if (original.sha256 !== finalization.sha256) {
        throw new ReviewDomainError('定稿原片快照与当前原片记录不一致，不能生成定稿包', 'PACKAGE_SOURCE_HASH_MISMATCH');
      }
      if (!original.blob) {
        throw new ReviewDomainError('定稿原片内容不可用，不能生成定稿包', 'PACKAGE_SOURCE_UNREADABLE');
      }
      sources.push({ name: entryName, blob: original.blob });
      entries.push({
        projectRefId: finalization.projectRefId,
        reviewItemId: finalization.reviewItemId,
        versionId: finalization.versionId,
        finalizationId: finalization.finalizationId,
        originalFileId: finalization.originalFileId,
        sha256: finalization.sha256,
        fileName: entryName,
      });
    }

    const projectPackageName = sanitizeFileSegment(input.project.code, 'project');
    const blob = await createStoredZipBlob(sources);
    return {
      packageId: `pkg_${createUuid()}`,
      projectRefId: input.project.projectRefId,
      packageFilename: `${projectPackageName}-finalized-originals.zip`,
      createdAt: new Date().toISOString(),
      fileName: `${projectPackageName}-finalized-originals.zip`,
      blob,
      entries,
    };
  }
}
