import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';

import react from '@vitejs/plugin-react';
import { loadEnv } from 'vite';
import { defineConfig } from 'vitest/config';

import { createMetaPolicy } from './src/frontend-security-policy';

const cspPlaceholder = '__FCR_CONTENT_SECURITY_POLICY__';
const mediaInfoWasmPath = createRequire(import.meta.url).resolve('mediainfo.js/MediaInfoModule.wasm');

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
        name: 'self-hosted-mediainfo-wasm',
        apply: 'build',
        buildStart() {
          this.emitFile({
            type: 'asset',
            name: 'MediaInfoModule.wasm',
            source: readFileSync(mediaInfoWasmPath),
          });
        },
      },
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
