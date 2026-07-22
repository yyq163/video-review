const unsafePathToken = /[\\/:*?"<>|]+/g;
const repeatedSeparators = /[\s._-]{2,}/g;

export function sanitizeFileSegment(value: string, fallback: string): string {
  const normalized = value
    .normalize('NFKC')
    .split('')
    .filter((char) => {
      const code = char.charCodeAt(0);
      return code > 31 && code !== 127;
    })
    .join('')
    .replace(unsafePathToken, '_')
    .replace(/\.\.+/g, '.')
    .replace(repeatedSeparators, '_')
    .trim()
    .replace(/^[\s._-]+|[\s._-]+$/g, '');

  if (!normalized || normalized === '.' || normalized === '..') {
    return fallback;
  }
  return normalized.slice(0, 120);
}

export function sanitizeDownloadFileName(value: string, fallback = 'download.bin'): string {
  const sanitized = sanitizeFileSegment(value, fallback);
  return sanitized.includes('.') ? sanitized : `${sanitized}.bin`;
}
