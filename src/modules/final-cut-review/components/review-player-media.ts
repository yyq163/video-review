export function waitForEvent(
  target: HTMLVideoElement,
  eventName: keyof HTMLMediaElementEventMap,
  signal: AbortSignal,
): Promise<void> {
  if (signal.aborted) return Promise.reject(new DOMException('旧回放请求已取消', 'AbortError'));
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      target.removeEventListener(eventName, onEvent);
      signal.removeEventListener('abort', onAbort);
    };
    const onEvent = () => {
      cleanup();
      resolve();
    };
    const onAbort = () => {
      cleanup();
      reject(new DOMException('旧回放请求已取消', 'AbortError'));
    };
    target.addEventListener(eventName, onEvent, { once: true });
    signal.addEventListener('abort', onAbort, { once: true });
  });
}

export function waitForMetadata(video: HTMLVideoElement, signal: AbortSignal): Promise<void> {
  if (video.readyState >= 1) return Promise.resolve();
  return waitForEvent(video, 'loadedmetadata', signal);
}

export function waitForCanPlay(video: HTMLVideoElement, signal: AbortSignal): Promise<void> {
  if (video.readyState >= 2) return Promise.resolve();
  return waitForEvent(video, 'canplay', signal);
}

export function waitForVideoFrame(video: HTMLVideoElement, signal: AbortSignal): Promise<void> {
  const requestFrame = video.requestVideoFrameCallback?.bind(video);
  const cancelFrame = video.cancelVideoFrameCallback?.bind(video);
  if (!requestFrame) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const handle = requestFrame(() => {
      signal.removeEventListener('abort', onAbort);
      resolve();
    });
    const onAbort = () => {
      if (cancelFrame) cancelFrame(handle);
      reject(new DOMException('旧回放请求已取消', 'AbortError'));
    };
    signal.addEventListener('abort', onAbort, { once: true });
  });
}
