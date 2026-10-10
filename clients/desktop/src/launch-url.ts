/**
 * The window's launch policy, as pure functions of URLs.
 *
 * The shell opens exactly one loopback gateway: the capability URL `vibesys web live` prints,
 * `http://127.0.0.1:<port>/?token=<token>`. Everything the page may reach is that origin and its
 * WebSocket twin. Messages here never quote the URL, because its query carries the capability.
 */

const LOOPBACK = '127.0.0.1';

export interface LaunchTarget {
  /** The capability URL to load; never log it. */
  readonly url: string;
  /** `http://127.0.0.1:<port>`: the only origin the window may navigate to or request. */
  readonly origin: string;
}

/** Validate the gateway capability URL; the error names the defect, never the token. */
export function parseLaunchUrl(raw: string | undefined): LaunchTarget {
  if (raw === undefined || raw === '') {
    throw new Error('VIBESYS_DESKTOP_URL is not set; start the app with scripts/run-desktop.sh');
  }
  const parsed = URL.parse(raw);
  if (parsed === null) throw new Error('VIBESYS_DESKTOP_URL is not a URL');
  if (parsed.protocol !== 'http:' || parsed.hostname !== LOOPBACK) {
    throw new Error(
      `VIBESYS_DESKTOP_URL must be an http://${LOOPBACK}:<port> gateway URL, not ${parsed.protocol}//${parsed.host}`,
    );
  }
  if (!parsed.searchParams.get('token')) {
    throw new Error('VIBESYS_DESKTOP_URL is missing its capability token');
  }
  return {url: parsed.href, origin: parsed.origin};
}

/** True when the window may navigate to `url`: its origin is exactly the gateway's. */
export function isAppUrl(url: string, origin: string): boolean {
  return URL.parse(url)?.origin === origin;
}

/** True when the page may send this request: HTTP or WebSocket to the gateway's host and port. */
export function isAllowedRequest(url: string, origin: string): boolean {
  const parsed = URL.parse(url);
  if (parsed === null) return false;
  if (parsed.protocol === 'ws:') return `http://${parsed.host}` === origin;
  return parsed.origin === origin;
}

/** The loggable part of a URL: the query can carry a capability token. */
export function originOf(url: string): string {
  return URL.parse(url)?.origin ?? 'an unparseable URL';
}
