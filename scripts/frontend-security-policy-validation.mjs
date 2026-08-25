import { JSDOM } from 'jsdom';

const requiredPolicySources = new Map([
  ['default-src', ["'self'"]],
  ['base-uri', ["'self'"]],
  ['object-src', ["'none'"]],
  ['script-src', ["'self'", "'wasm-unsafe-eval'"]],
  ['form-action', ["'self'"]],
]);
const maxHtmlBytes = 1024 * 1024;
const forbiddenExecutableOverrides = new Set([
  'script-src-elem',
  'script-src-attr',
  'worker-src',
  'child-src',
]);

export function extractSingleCspMeta(html, label) {
  const dom = new JSDOM(html);
  try {
    const policies = [...dom.window.document.querySelectorAll('meta[http-equiv]')]
      .filter((meta) => meta.getAttribute('http-equiv')?.trim().toLowerCase() === 'content-security-policy');
    if (policies.length !== 1) {
      throw new Error(`${label} must contain exactly one CSP meta`);
    }
    const policy = policies[0].getAttribute('content');
    if (!policy) {
      throw new Error(`${label} CSP meta is missing content`);
    }
    return policy;
  } finally {
    dom.window.close();
  }
}

function assertExactSources(directives, directiveName, expectedSources, label) {
  const actualSources = directives.get(directiveName);
  if (!actualSources) {
    throw new Error(`${label} CSP is missing required directive: ${directiveName}`);
  }
  if (actualSources.length !== expectedSources.length
    || expectedSources.some((source) => !actualSources.includes(source))) {
    throw new Error(`${label} CSP ${directiveName} must contain only ${expectedSources.join(' ')}`);
  }
}

export function assertRequiredPolicy(policy, label, { requireFrameAncestors = false } = {}) {
  if (!policy) {
    throw new Error(`${label} is missing Content-Security-Policy`);
  }
  if (policy.includes(',')) {
    throw new Error(`${label} must contain exactly one enforced CSP policy`);
  }

  const directives = new Map();
  for (const rawDirective of policy.split(';')) {
    const parts = rawDirective.trim().split(/\s+/).filter(Boolean);
    if (parts.length === 0) {
      continue;
    }
    const directiveName = parts.shift().toLowerCase();
    if (directives.has(directiveName)) {
      throw new Error(`${label} CSP contains duplicate ${directiveName}`);
    }
    directives.set(directiveName, parts.map((source) => source.toLowerCase()));
  }

  for (const sources of directives.values()) {
    if (sources.includes("'unsafe-eval'")) {
      throw new Error(`${label} CSP must not enable general 'unsafe-eval'`);
    }
  }
  for (const directiveName of forbiddenExecutableOverrides) {
    if (directives.has(directiveName)) {
      throw new Error(`${label} CSP must not define executable override directive ${directiveName}`);
    }
  }
  for (const [directiveName, expectedSources] of requiredPolicySources) {
    assertExactSources(directives, directiveName, expectedSources, label);
  }
  if (requireFrameAncestors) {
    assertExactSources(directives, 'frame-ancestors', ["'none'"], label);
  }
  return directives;
}

export function resolveDeployedFrontendTarget(rawValue, configuredApiOrigin) {
  let target;
  try {
    target = new URL(rawValue);
  } catch {
    throw new Error('FCR_DEPLOYED_FRONTEND_URL must be the exact configured origin root');
  }
  if (!['http:', 'https:'].includes(target.protocol)
    || target.username
    || target.password
    || target.search
    || target.hash
    || target.pathname !== '/'
    || !configuredApiOrigin
    || target.origin !== configuredApiOrigin
    || target.href !== `${configuredApiOrigin}/`) {
    throw new Error('FCR_DEPLOYED_FRONTEND_URL must be the exact configured origin root');
  }
  return target;
}

export async function readBoundedHtml(response, label) {
  const contentType = response.headers.get('content-type')?.toLowerCase() ?? '';
  if (!/^text\/html(?:\s*;|$)/i.test(contentType)) {
    throw new Error(`${label} must return text/html`);
  }
  const declaredLength = response.headers.get('content-length');
  if (declaredLength) {
    if (!/^\d+$/.test(declaredLength) || Number(declaredLength) > maxHtmlBytes) {
      throw new Error(`${label} HTML exceeds 1 MiB`);
    }
  }
  if (!response.body) {
    return '';
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const chunks = [];
  let totalBytes = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      totalBytes += value.byteLength;
      if (totalBytes > maxHtmlBytes) {
        await reader.cancel();
        throw new Error(`${label} HTML exceeds 1 MiB`);
      }
      chunks.push(decoder.decode(value, { stream: true }));
    }
    chunks.push(decoder.decode());
    return chunks.join('');
  } finally {
    reader.releaseLock();
  }
}
