import { preview } from 'vite';

const requiredDirectives = [
  "default-src 'self'",
  "base-uri 'self'",
  "object-src 'none'",
  "script-src 'self'",
  "form-action 'self'",
];

const server = await preview({
  logLevel: 'silent',
  preview: {
    host: '127.0.0.1',
    port: 0,
    strictPort: true,
  },
});

try {
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

  if (!headerPolicy) {
    throw new Error('preview root is missing Content-Security-Policy');
  }
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

  for (const directive of requiredDirectives) {
    if (!headerPolicy.includes(directive)) {
      throw new Error(`CSP is missing required directive: ${directive}`);
    }
  }

  for (const directiveName of ['connect-src', 'media-src']) {
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
  }

  process.stdout.write('frontend security headers: PASS\n');
} finally {
  await server.close();
}
