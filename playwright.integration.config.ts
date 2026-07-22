import { defineConfig, devices } from '@playwright/test';

const baseURL = process.env.FCR_E2E_BASE_URL;
const browserChannel = process.env.FCR_PLAYWRIGHT_CHANNEL ?? (process.platform === 'darwin' ? 'chrome' : undefined);
if (!baseURL) {
  throw new Error('FCR_E2E_BASE_URL is required for the real-stack E2E profile');
}
if (!process.env.FCR_E2E_API_BASE_URL) {
  throw new Error('FCR_E2E_API_BASE_URL is required for the real-stack E2E profile');
}

export default defineConfig({
  testDir: './tests/e2e',
  testMatch: '**/*.integration.spec.ts',
  timeout: 180_000,
  expect: {
    timeout: 30_000,
  },
  workers: 1,
  fullyParallel: false,
  use: {
    baseURL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'real-stack-chromium',
      use: {
        ...devices['Desktop Chrome'],
        ...(browserChannel ? { channel: browserChannel } : {}),
        viewport: { width: 1440, height: 900 },
      },
    },
  ],
});
