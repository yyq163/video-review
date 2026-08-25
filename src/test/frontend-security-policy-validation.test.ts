import { describe, expect, it } from 'vitest';

import {
  assertRequiredPolicy,
  extractSingleCspMeta,
  readBoundedHtml,
  resolveDeployedFrontendTarget,
} from '../../scripts/frontend-security-policy-validation.mjs';

const validPolicy = [
  "default-src 'self'",
  "base-uri 'self'",
  "object-src 'none'",
  "script-src 'self' 'wasm-unsafe-eval'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "font-src 'self' data:",
  "connect-src 'self' http://192.168.100.217:18171",
  "media-src 'self' blob: http://192.168.100.217:18171",
  "form-action 'self'",
  "frame-ancestors 'none'",
].join('; ');

describe('deployed frontend CSP validation', () => {
  it('accepts exactly one narrow WebAssembly policy', () => {
    expect(() => assertRequiredPolicy(validPolicy, 'deployed root')).not.toThrow();
  });

  it('rejects multiple enforced policies when one blocks WebAssembly', () => {
    const combined = `${validPolicy}, default-src 'self'; script-src 'self'`;

    expect(() => assertRequiredPolicy(combined, 'deployed root'))
      .toThrow(/exactly one enforced CSP policy/i);
  });

  it('rejects duplicate script-src directives in one policy', () => {
    const duplicate = `${validPolicy}; script-src 'self'`;

    expect(() => assertRequiredPolicy(duplicate, 'deployed root'))
      .toThrow(/duplicate script-src/i);
  });

  it('rejects case-insensitive general eval and extra script sources', () => {
    const unsafe = validPolicy.replace("'wasm-unsafe-eval'", "'WASM-UNSAFE-EVAL' 'UNSAFE-EVAL'");
    const widened = validPolicy.replace("'wasm-unsafe-eval'", "'wasm-unsafe-eval' https: data:");

    expect(() => assertRequiredPolicy(unsafe, 'deployed root')).toThrow(/general 'unsafe-eval'/i);
    expect(() => assertRequiredPolicy(widened, 'deployed root')).toThrow(/script-src.*only/i);
  });

  it('rejects script and worker override directives', () => {
    for (const directive of [
      'script-src-elem https://evil.example',
      'script-src-attr https://evil.example',
      'worker-src https://evil.example',
      'child-src https://evil.example',
    ]) {
      expect(() => assertRequiredPolicy(`${validPolicy}; ${directive}`, 'deployed root'))
        .toThrow(/must not define.*(?:script|worker|child)/i);
    }
  });

  it('requires exactly one CSP meta regardless of attribute order', () => {
    const one = `<!doctype html><meta http-equiv="Content-Security-Policy" content="${validPolicy}" />`;
    const two = `${one}<meta content="default-src 'self'; script-src 'self'" http-equiv='content-security-policy'>`;

    expect(extractSingleCspMeta(one, 'deployed root')).toBe(validPolicy);
    expect(() => extractSingleCspMeta(two, 'deployed root')).toThrow(/exactly one CSP meta/i);
  });
});

describe('deployed frontend URL boundary', () => {
  it('accepts only the configured origin root without credentials, query, or fragment', () => {
    expect(resolveDeployedFrontendTarget(
      'http://192.168.100.217:18171/',
      'http://192.168.100.217:18171',
    ).href).toBe('http://192.168.100.217:18171/');

    for (const value of [
      'http://127.0.0.1:18171/',
      'http://192.168.100.217:18171/review/projects',
      'http://192.168.100.217:18171/?token=secret',
      'http://192.168.100.217:18171/?',
      'http://user:password@192.168.100.217:18171/',
      'http://192.168.100.217:18171/#fragment',
      'http://192.168.100.217:18171/#',
    ]) {
      expect(() => resolveDeployedFrontendTarget(value, 'http://192.168.100.217:18171'))
        .toThrow(/exact configured origin root/i);
    }
  });
});

describe('deployed frontend response bounds', () => {
  it('requires HTML and refuses declared or streamed bodies above 1 MiB', async () => {
    const oversized = 'x'.repeat(1024 * 1024 + 1);

    await expect(readBoundedHtml(new Response('{}', {
      headers: { 'content-type': 'application/json' },
    }), 'deployed root')).rejects.toThrow(/text\/html/i);
    for (const contentType of ['text/htmlx', 'text/html-evil', 'text/html, application/json']) {
      await expect(readBoundedHtml(new Response('<!doctype html>', {
        headers: { 'content-type': contentType },
      }), 'deployed root')).rejects.toThrow(/text\/html/i);
    }
    await expect(readBoundedHtml(new Response('<!doctype html>', {
      headers: {
        'content-type': 'text/html; charset=utf-8',
        'content-length': String(1024 * 1024 + 1),
      },
    }), 'deployed root')).rejects.toThrow(/exceeds 1 MiB/i);
    await expect(readBoundedHtml(new Response(oversized, {
      headers: { 'content-type': 'text/html; charset=utf-8' },
    }), 'deployed root')).rejects.toThrow(/exceeds 1 MiB/i);
  });
});
