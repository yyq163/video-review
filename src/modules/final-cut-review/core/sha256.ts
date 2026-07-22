async function blobToArrayBuffer(blob: Blob): Promise<ArrayBuffer> {
  if (typeof blob.arrayBuffer === 'function') {
    return blob.arrayBuffer();
  }
  return new Promise<ArrayBuffer>((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error('Blob 读取失败'));
    reader.onload = () => resolve(reader.result as ArrayBuffer);
    reader.readAsArrayBuffer(blob);
  });
}

export async function sha256Hex(source: Blob | ArrayBuffer | string): Promise<string> {
  const data =
    typeof source === 'string'
      ? new TextEncoder().encode(source)
      : source instanceof Blob
        ? new Uint8Array(await blobToArrayBuffer(source))
        : new Uint8Array(source);
  const digest = await crypto.subtle.digest('SHA-256', data);
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, '0')).join('');
}
