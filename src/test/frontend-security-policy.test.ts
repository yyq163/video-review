import { describe, expect, it } from 'vitest';

import { createMetaPolicy } from '../frontend-security-policy';

describe('frontend media metadata CSP', () => {
  it('permits only self-hosted WASM compilation without opening script or network sources', () => {
    const policy = createMetaPolicy('https://api.example.test', false);
    const directives = new Map(policy.split('; ').map((directive) => {
      const [name, ...sources] = directive.split(' ');
      return [name, sources];
    }));

    expect(directives.get('script-src')).toEqual(["'self'", "'wasm-unsafe-eval'"]);
    expect(directives.get('script-src')).not.toContain("'unsafe-eval'");
    expect(directives.get('connect-src')).toEqual(["'self'", 'https://api.example.test']);
  });
});
