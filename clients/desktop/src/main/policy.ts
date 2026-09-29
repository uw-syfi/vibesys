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

/** The capability URL; dev rejects a home outside the configured Vite proxy before exposing it. */
export function windowUrl(
  home: {readonly origin: string; readonly token: string},
  devOrigin: string | null,
  port?: string,
): string {
  const proxy = originOf(`http://127.0.0.1:${port || DEV_PROXY_PORT}`);
  if (devOrigin !== null && originOf(home.origin) !== proxy) {
    throw new Error(
      `The home server at ${originOf(home.origin)} does not match the Vite API proxy at ${proxy}. Restart the home server on the proxy port, or restart desktop dev with VIBESYS_HOME_PORT set to the running home server's port.`,
    );
  }
  const url = new URL('/', devOrigin ?? home.origin);
  url.searchParams.set('token', home.token);
  return url.href;
}

/** The origin `electron-vite dev` serves clients/web on (electron.vite.config.ts). */
const VITE_ORIGIN = 'http://127.0.0.1:5173';

/**
 * The dev origin: `ELECTRON_RENDERER_URL` when it is exactly the Vite origin in a dev build,
 * else null. A stray value from a shell must not become an app origin that receives the token.
 */
export function devOrigin(rendererUrl: string | undefined, dev: boolean): string | null {
  return dev && rendererUrl === VITE_ORIGIN ? VITE_ORIGIN : null;
}

/** running: no quit yet; stopping: the home server is being stopped; stopped: it settled. */
export type QuitState = 'running' | 'stopping' | 'stopped';

/**
 * What a quit request does: `hold` defers the exit, `startStop` begins stopping the home server.
 * Every request is held until the stop settles, so a repeated quit cannot orphan the server.
 */
export function quitRequest(state: QuitState): {
  readonly hold: boolean;
  readonly startStop: boolean;
} {
  return {hold: state !== 'stopped', startStop: state === 'running'};
}
