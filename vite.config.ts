import react from '@vitejs/plugin-react';
import { loadEnv } from 'vite';
import { defineConfig } from 'vitest/config';

const cspPlaceholder = '__FCR_CONTENT_SECURITY_POLICY__';

function resolveApiOrigin(apiBaseUrl: string | undefined, required: boolean): string | undefined {
  const normalizedApiBaseUrl = apiBaseUrl?.trim() ?? '';
  if (!normalizedApiBaseUrl) {
    if (required) {
      throw new Error('VITE_FINAL_CUT_REVIEW_API_BASE_URL is required for production build and preview');
    }
    return undefined;
  }

  const parsed = new URL(normalizedApiBaseUrl);
  if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password) {
    throw new Error('VITE_FINAL_CUT_REVIEW_API_BASE_URL must be an absolute credential-free HTTP(S) URL');
  }
  return parsed.origin;
}

function createMetaPolicy(apiOrigin: string | undefined, isDevelopmentServer: boolean): string {
  const scriptSources = isDevelopmentServer ? "'self' 'unsafe-inline'" : "'self'";
  const developmentSocketSources = isDevelopmentServer ? ' ws://127.0.0.1:* ws://localhost:*' : '';
  const apiSource = apiOrigin && apiOrigin !== 'null' ? ` ${apiOrigin}` : '';

  return [
    "default-src 'self'",
    "base-uri 'self'",
    "object-src 'none'",
    `script-src ${scriptSources}`,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    `connect-src 'self'${apiSource}${developmentSocketSources}`,
    `media-src 'self' blob:${apiSource}`,
    "form-action 'self'",
  ].join('; ');
}

export default defineConfig(({ command, isPreview, mode }) => {
  const isDevelopmentServer = command === 'serve' && !isPreview;
  const loadedEnvironment = loadEnv(mode, '.', '');
  const configuredApiBaseUrl =
    process.env.VITE_FINAL_CUT_REVIEW_API_BASE_URL ?? loadedEnvironment.VITE_FINAL_CUT_REVIEW_API_BASE_URL;
  const apiOrigin = resolveApiOrigin(configuredApiBaseUrl, command === 'build' || Boolean(isPreview));
  const metaPolicy = createMetaPolicy(apiOrigin, isDevelopmentServer);
  const headerPolicy = `${metaPolicy}; frame-ancestors 'none'`;
  const securityHeaders = {
    'Content-Security-Policy': headerPolicy,
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
  };

  return {
    plugins: [
      react(),
      {
        name: 'frontend-security-policy',
        transformIndexHtml(html: string) {
          if (!html.includes(cspPlaceholder)) {
            throw new Error('index.html is missing the frontend CSP placeholder');
          }
          return html.replace(cspPlaceholder, metaPolicy);
        },
      },
    ],
    server: {
      headers: securityHeaders,
      port: 5188,
      strictPort: true,
    },
    preview: {
      headers: securityHeaders,
    },
    test: {
      environment: 'jsdom',
      globals: true,
      testTimeout: 15_000,
      include: ['src/**/*.test.{ts,tsx}'],
      exclude: ['tests/e2e/**', 'node_modules/**', 'dist/**'],
      setupFiles: './src/test/setup.ts',
    },
  };
});
