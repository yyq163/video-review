import { defineConfig, devices } from '@playwright/test';

// Local macOS delivery workstations already provide Chrome; using it avoids an
// implicit Playwright browser download while CI can keep its bundled browser.
const browserChannel = process.env.FCR_PLAYWRIGHT_CHANNEL ?? (process.platform === 'darwin' ? 'chrome' : undefined);
const requestedTestPort = process.env.FCR_E2E_PORT ?? '5188';
if (!/^[0-9]+$/.test(requestedTestPort)) {
  throw new Error('FCR_E2E_PORT must be a decimal TCP port');
}
const parsedTestPort = Number.parseInt(requestedTestPort, 10);
if (parsedTestPort < 1024 || parsedTestPort > 65_535) {
  throw new Error('FCR_E2E_PORT must be between 1024 and 65535');
}
const testPort = String(parsedTestPort);
const testBaseUrl = `http://127.0.0.1:${testPort}`;

export default defineConfig({
  testDir: './tests/e2e',
  testIgnore: '**/*.integration.spec.ts',
  timeout: 45_000,
  expect: {
    timeout: 10_000,
  },
  use: {
    baseURL: testBaseUrl,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: `VITE_FINAL_CUT_REVIEW_API_BASE_URL= npm run dev -- --host 127.0.0.1 --port ${testPort} --strictPort`,
    url: testBaseUrl,
    reuseExistingServer: false,
    timeout: 120_000,
  },
  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        ...(browserChannel ? { channel: browserChannel } : {}),
        viewport: { width: 1366, height: 768 },
      },
    },
  ],
});
