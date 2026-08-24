export function createMetaPolicy(apiOrigin: string | undefined, isDevelopmentServer: boolean): string {
  const scriptSources = isDevelopmentServer
    ? "'self' 'unsafe-inline' 'wasm-unsafe-eval'"
    : "'self' 'wasm-unsafe-eval'";
  const developmentSocketSources = isDevelopmentServer ? ' ws://127.0.0.1:* ws://localhost:*' : '';
  const apiSource = apiOrigin && apiOrigin !== 'null' ? ` ${apiOrigin}` : '';

  return [
    "default-src 'self'",
    "base-uri 'self'",
    "object-src 'none'",
    `script-src ${scriptSources}`,
    "style-src 'self' 'unsafe-inline'",
    `img-src 'self' data: blob:${apiSource}`,
    "font-src 'self' data:",
    `connect-src 'self'${apiSource}${developmentSocketSources}`,
    `media-src 'self' blob:${apiSource}`,
    "form-action 'self'",
  ].join('; ');
}
