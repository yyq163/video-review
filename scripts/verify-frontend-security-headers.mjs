import { preview } from 'vite';
import { readdir, stat } from 'node:fs/promises';

const requiredDirectives = [
  "default-src 'self'",
  "base-uri 'self'",
  "object-src 'none'",
  "script-src 'self' 'wasm-unsafe-eval'",
  "form-action 'self'",
];

function assertRequiredPolicy(policy, label) {
  if (!policy) {
    throw new Error(`${label} is missing Content-Security-Policy`);
  }
  for (const directive of requiredDirectives) {
    if (!policy.includes(directive)) {
      throw new Error(`${label} CSP is missing required directive: ${directive}`);
    }
  }
  if (policy.includes("'unsafe-eval'")) {
    throw new Error(`${label} CSP must not enable general 'unsafe-eval'`);
  }
}

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
  const metaMatch = html.match(
    /<meta\s+http-equiv="Content-Security-Policy"\s+content="([^"]+)"\s*\/>/i,
  );

  assertRequiredPolicy(headerPolicy, 'preview root');
  if (contentTypeOptions !== 'nosniff') {
    throw new Error('preview root is missing X-Content-Type-Options: nosniff');
  }
  if (frameOptions !== 'DENY') {
    throw new Error('preview root is missing X-Frame-Options: DENY');
  }
  if (!metaMatch) {
    throw new Error('built index is missing the CSP meta fallback');
  }
  const headerOnlyFramePolicy = "; frame-ancestors 'none'";
  if (!headerPolicy.endsWith(headerOnlyFramePolicy)) {
    throw new Error("CSP response header is missing frame-ancestors 'none'");
  }
  if (metaMatch[1] !== headerPolicy.slice(0, -headerOnlyFramePolicy.length)) {
    throw new Error('CSP response header and supported meta fallback directives differ');
  }

  const configuredApiBaseUrl = process.env.VITE_FINAL_CUT_REVIEW_API_BASE_URL?.trim();
  const configuredApiOrigin = configuredApiBaseUrl
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
    const deployedTarget = new URL(deployedFrontendUrl);
    if (!['http:', 'https:'].includes(deployedTarget.protocol)
      || deployedTarget.username
      || deployedTarget.password
      || deployedTarget.hash) {
      throw new Error('FCR_DEPLOYED_FRONTEND_URL must be a credential-free HTTP(S) URL without a fragment');
    }
    const deployedResponse = await fetch(deployedTarget, { redirect: 'error' });
    if (!deployedResponse.ok) {
      throw new Error(`deployed root returned HTTP ${deployedResponse.status}`);
    }
    const deployedHtml = await deployedResponse.text();
    const deployedHeaderPolicy = deployedResponse.headers.get('content-security-policy');
    const deployedMetaMatch = deployedHtml.match(
      /<meta\s+http-equiv="Content-Security-Policy"\s+content="([^"]+)"\s*\/>/i,
    );
    assertRequiredPolicy(deployedHeaderPolicy, 'deployed root');
    if (!deployedMetaMatch) {
      throw new Error('deployed root is missing the CSP meta fallback');
    }
    assertRequiredPolicy(deployedMetaMatch[1], 'deployed meta fallback');
    if (!deployedHeaderPolicy.includes("frame-ancestors 'none'")) {
      throw new Error("deployed root CSP is missing frame-ancestors 'none'");
    }
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
