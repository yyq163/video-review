import { expect, test, type Page } from '@playwright/test';

// Supply an existing disposable local-stack item; this regression writes no business data.
const reviewUrl = process.env.FCR_E2E_REVIEW_URL;
if (reviewUrl && !['127.0.0.1', 'localhost', '[::1]'].includes(new URL(reviewUrl).hostname)) {
  throw new Error('FCR_E2E_REVIEW_URL must point to a local test stack');
}
test.skip(!reviewUrl, 'FCR_E2E_REVIEW_URL is required for the real portrait-media regression');

async function playerGeometry(page: Page) {
  return page.locator('.fj-review-player-stage').evaluate((stage) => {
    const frame = stage.querySelector('.fj-review-video-frame');
    const video = stage.querySelector('video');
    const annotation = stage.querySelector('[data-testid="saved-annotation-layer"]');
    if (!frame || !video || !annotation) throw new Error('missing real player elements');
    const rect = (node: Element) => {
      const { left, top, right, bottom, width, height } = node.getBoundingClientRect();
      return { left, top, right, bottom, width, height };
    };
    return {
      stage: rect(stage),
      frame: rect(frame),
      video: rect(video),
      annotation: rect(annotation),
      intrinsicWidth: video.videoWidth,
      intrinsicHeight: video.videoHeight,
      readyState: video.readyState,
      currentTime: video.currentTime,
      poster: video.getAttribute('poster'),
      paused: video.paused,
      mediaError: video.error?.code ?? null,
    };
  });
}

function assertContained(metrics: Awaited<ReturnType<typeof playerGeometry>>) {
  expect(metrics.stage.height).toBeGreaterThan(100);
  for (const edge of ['left', 'top', 'right', 'bottom'] as const) {
    expect(Math.abs(metrics.frame[edge] - metrics.stage[edge]), `frame ${edge}`).toBeLessThan(1);
    expect(Math.abs(metrics.annotation[edge] - metrics.video[edge]), `annotation ${edge}`).toBeLessThan(1);
  }
  expect(metrics.video.left).toBeGreaterThanOrEqual(metrics.stage.left - 1);
  expect(metrics.video.top).toBeGreaterThanOrEqual(metrics.stage.top - 1);
  expect(metrics.video.right).toBeLessThanOrEqual(metrics.stage.right + 1);
  expect(metrics.video.bottom).toBeLessThanOrEqual(metrics.stage.bottom + 1);
  expect(metrics.intrinsicHeight).toBeGreaterThan(metrics.intrinsicWidth);
  expect(metrics.video.width / metrics.video.height).toBeCloseTo(
    metrics.intrinsicWidth / metrics.intrinsicHeight, 2,
  );
  expect(metrics.mediaError).toBeNull();
}

for (const viewport of [{ width: 1755, height: 1337 }, { width: 1366, height: 768 }]) {
  test(`portrait video fits the visible stage before and after playback at ${viewport.width}x${viewport.height}`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    const thumbnailResponse = page.waitForResponse((response) => new URL(response.url()).pathname.endsWith('/thumbnail'));
    await page.goto(reviewUrl!);
    const video = page.locator('.fj-review-video-frame video');
    await expect(video).toBeVisible();
    await expect.poll(() => video.evaluate((node) => node.videoHeight)).toBeGreaterThan(0);
    const before = await playerGeometry(page);
    const posterResponse = await thumbnailResponse;
    await testInfo.attach('before-playback', {
      body: JSON.stringify({ ...before, posterStatus: posterResponse.status() }, null, 2),
      contentType: 'application/json',
    });
    await page.screenshot({ path: testInfo.outputPath('before-playback.png') });

    await page.getByRole('button', { name: '播放', exact: true }).click();
    await expect.poll(() => video.evaluate((node) => node.currentTime)).toBeGreaterThan(0.25);
    await page.getByRole('button', { name: '暂停', exact: true }).click();
    const after = await playerGeometry(page);
    await testInfo.attach('after-playback', { body: JSON.stringify(after, null, 2), contentType: 'application/json' });
    await page.screenshot({ path: testInfo.outputPath('after-playback.png') });

    assertContained(before);
    assertContained(after);
    expect(before.poster).toBeTruthy();
    expect(posterResponse.ok()).toBe(true);
    expect(before.paused).toBe(true);
    expect(before.currentTime).toBe(0);
    expect(after.readyState).toBeGreaterThanOrEqual(2); // HAVE_CURRENT_DATA
  });
}
