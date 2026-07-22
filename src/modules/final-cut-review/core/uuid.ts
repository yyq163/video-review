export function createUuid(): string {
  const webCrypto = globalThis.crypto;

  if (webCrypto && typeof webCrypto.randomUUID === 'function') {
    return webCrypto.randomUUID();
  }

  if (!webCrypto || typeof webCrypto.getRandomValues !== 'function') {
    throw new Error('安全随机数生成器不可用，无法生成 UUID');
  }

  const bytes = webCrypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;

  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
