/** The shell's security and launch rules, as pure functions of URLs and settings. */

/** `clients/web/vite.config.ts` proxies `/api` here unless `VIBESYS_HOME_PORT` is set. */
const DEV_PROXY_PORT = '8764';
/** The one browser permission the app uses ("Copy run ID"). */
const ALLOWED_PERMISSIONS: ReadonlySet<string> = new Set(['clipboard-sanitized-write']);

/** True when the window may navigate to `url`: its origin is exactly an app origin. */
export function isAppUrl(url: string, origins: readonly string[]): boolean {
  const parsed = URL.parse(url);
  return parsed !== null && origins.includes(parsed.origin);
}

/**
 * True when the page may send this request: HTTP(S) to an app origin, or a WebSocket to a
 * loopback run gateway (the home server's CSP `connect-src ws://127.0.0.1:*`). In dev, where
 * Vite sends no CSP, this is the only such rule.
 */
export function isAllowedRequest(url: string, origins: readonly string[]): boolean {
  const parsed = URL.parse(url);
  if (parsed === null) return false;
  if (parsed.protocol === 'ws:') return parsed.hostname === '127.0.0.1';
  return origins.includes(parsed.origin);
}

export function allowPermission(permission: string): boolean {
  return ALLOWED_PERMISSIONS.has(permission);
}

/** The loggable part of a URL: the query can carry a capability token. */
export function originOf(url: string): string {
  return URL.parse(url)?.origin ?? 'an unparseable URL';
}

/** Flags for `vibesys web home`: the port to listen on, and in dev the Vite origin to accept. */
export function homeArguments(port: string | undefined, devOrigin: string | null): string[] {
  // An empty VIBESYS_HOME_PORT counts as unset.
  const listen = port || (devOrigin === null ? undefined : DEV_PROXY_PORT);
  return [
    ...(listen === undefined ? [] : ['--port', listen]),
    ...(devOrigin === null ? [] : ['--dev-origin', devOrigin]),
  ];
}

/** The app URL with the home capability, on the home origin or (dev) the Vite origin. */
export function windowUrl(
  home: {readonly origin: string; readonly token: string},
  devOrigin: string | null,
): string {
  const url = new URL('/', devOrigin ?? home.origin);
  url.searchParams.set('token', home.token);
  return url.href;
}
