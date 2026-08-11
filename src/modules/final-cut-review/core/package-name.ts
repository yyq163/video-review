const WINDOWS_DRIVE_PREFIX = /^[A-Za-z]:/;

export function validateUnifiedPackageName(value: string): string | null {
  if (value.trim().length === 0) return '统一名称必填，且不能只包含空白。';
  if (value.includes('\0')) return '统一名称不能包含 NUL 字符。';
  if (value.includes('/') || value.includes('\\')) return '统一名称不能包含目录分隔符。';
  if (value === '.' || value === '..' || WINDOWS_DRIVE_PREFIX.test(value)) {
    return '统一名称不能是绝对路径或路径穿越名称。';
  }
  return null;
}

export function positiveEpisodeNumber(value: string): number | null {
  if (!/^\d+$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
}

export function originalFileExtension(fileName: string): string {
  if (!fileName || fileName.includes('\0') || fileName.includes('/') || fileName.includes('\\')) {
    throw new Error('定稿原片名称不安全，不能生成项目包。');
  }
  const finalDot = fileName.lastIndexOf('.');
  if (finalDot <= 0 || finalDot === fileName.length - 1) {
    throw new Error('定稿原片缺少有效原扩展名，不能生成项目包。');
  }
  return fileName.slice(finalDot);
}
