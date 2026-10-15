import { preview } from 'vite';
import { readdir, stat } from 'node:fs/promises';

import {
  assertRequiredPolicy,
  extractSingleCspMeta,
  readBoundedHtml,
  resolveDeployedFrontendTarget,
} from './frontend-security-policy-validation.mjs';

const server = await preview({
  logLevel: 'silent',
  preview: {
    host: '127.0.0.1',
    port: 0,
    strictPort: true,
  },
});

try {
  const assetDirectory = new URL('../dist/assets/', import.meta.url);
  const wasmAssets = (await readdir(assetDirectory))
    .filter((entry) => /^MediaInfoModule-[A-Za-z0-9_-]+\.wasm$/.test(entry));
  if (wasmAssets.length !== 1) {
    throw new Error('production build must contain exactly one self-hosted MediaInfoModule WASM asset');
  }
  if ((await stat(new URL(wasmAssets[0], assetDirectory))).size <= 0) {
    throw new Error('MediaInfoModule WASM asset is empty');
  }

  const address = server.httpServer.address();
  if (!address || typeof address === 'string') {
    throw new Error('preview server did not expose a TCP address');
  }

  const response = await fetch(`http://127.0.0.1:${address.port}/`);
  if (!response.ok) {
    throw new Error(`preview root returned HTTP ${response.status}`);
  }

  const html = await response.text();
  const headerPolicy = response.headers.get('content-security-policy');
  const contentTypeOptions = response.headers.get('x-content-type-options');
  const frameOptions = response.headers.get('x-frame-options');
  const metaPolicy = extractSingleCspMeta(html, 'built index');

  assertRequiredPolicy(headerPolicy, 'preview root', { requireFrameAncestors: true });
  if (contentTypeOptions !== 'nosniff') {
    throw new Error('preview root is missing X-Content-Type-Options: nosniff');
  }
  if (frameOptions !== 'DENY') {
    throw new Error('preview root is missing X-Frame-Options: DENY');
  }
  const headerOnlyFramePolicy = "; frame-ancestors 'none'";
  if (!headerPolicy.endsWith(headerOnlyFramePolicy)) {
    throw new Error("CSP response header is missing frame-ancestors 'none'");
  }
  if (metaPolicy !== headerPolicy.slice(0, -headerOnlyFramePolicy.length)) {
    throw new Error('CSP response header and supported meta fallback directives differ');
  }

  const configuredApiBaseUrl = process.env.VITE_FINAL_CUT_REVIEW_API_BASE_URL?.trim();
  const useSameOrigin = configuredApiBaseUrl === 'same-origin';
  const configuredApiOrigin = configuredApiBaseUrl && !useSameOrigin
    ? new URL(configuredApiBaseUrl).origin
    : null;

  for (const directiveName of ['connect-src', 'media-src', 'img-src']) {
    const directive = headerPolicy
      .split(';')
      .map((value) => value.trim())
      .find((value) => value.startsWith(`${directiveName} `));
    if (!directive) {
      throw new Error(`CSP is missing ${directiveName}`);
    }
    const sources = directive.split(/\s+/).slice(1);
    if (sources.includes('https:') || sources.includes('http:') || sources.some((source) => source.includes('*'))) {
      throw new Error(`${directiveName} contains a scheme-wide or wildcard source`);
    }
    if (configuredApiOrigin && !sources.includes(configuredApiOrigin)) {
      throw new Error(`${directiveName} is missing the configured API origin`);
    }
  }

  const deployedFrontendUrl = process.env.FCR_DEPLOYED_FRONTEND_URL?.trim();
  if (deployedFrontendUrl) {
    const deployedTarget = resolveDeployedFrontendTarget(
      deployedFrontendUrl,
      useSameOrigin ? new URL(deployedFrontendUrl).origin : configuredApiOrigin,
    );
    const deployedResponse = await fetch(deployedTarget, {
      redirect: 'error',
      signal: AbortSignal.timeout(10_000),
    });
    if (!deployedResponse.ok) {
      throw new Error(`deployed root returned HTTP ${deployedResponse.status}`);
    }
    const deployedHtml = await readBoundedHtml(deployedResponse, 'deployed root');
    const deployedHeaderPolicy = deployedResponse.headers.get('content-security-policy');
    const deployedMetaPolicy = extractSingleCspMeta(deployedHtml, 'deployed root');
    assertRequiredPolicy(deployedHeaderPolicy, 'deployed root', { requireFrameAncestors: true });
    assertRequiredPolicy(deployedMetaPolicy, 'deployed meta fallback');
    if (deployedResponse.headers.get('x-content-type-options') !== 'nosniff') {
      throw new Error('deployed root is missing X-Content-Type-Options: nosniff');
    }
    if (deployedResponse.headers.get('x-frame-options') !== 'DENY') {
      throw new Error('deployed root is missing X-Frame-Options: DENY');
    }
  }

  process.stdout.write('frontend security headers: PASS\n');
} finally {
  await server.close();
}
