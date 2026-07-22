import { defineConfig, devices } from '@playwright/test';

// Local macOS delivery workstations already provide Chrome; using it avoids an
// implicit Playwright browser download while CI can keep its bundled browser.
const browserChannel = process.env.FCR_PLAYWRIGHT_CHANNEL ?? (process.platform === 'darwin' ? 'chrome' : undefined);

export default defineConfig({
  testDir: './tests/e2e',
  testIgnore: '**/*.integration.spec.ts',
  timeout: 45_000,
  expect: {
    timeout: 10_000,
  },
  use: {
    baseURL: 'http://127.0.0.1:5188',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: 'VITE_FINAL_CUT_REVIEW_API_BASE_URL= npm run dev -- --host 127.0.0.1 --port 5188 --strictPort',
    url: 'http://127.0.0.1:5188',
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
