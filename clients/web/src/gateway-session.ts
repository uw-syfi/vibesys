const REMEMBERED_GATEWAY_KEY = 'vibesys.gateway-session.v1';
const REMEMBERED_BROWSER_SESSION_KEY = 'vibesys.browser-session.v1';
const BROWSER_SESSION_HEADER = 'X-VibeSys-Browser-Session';

export interface GatewayTarget {
  /** Capability-free HTTP authority retained in browser state and URLs. */
  readonly gatewayUrl: string;
  /** Capability-free page URL that replaces the incoming bootstrap URL. */
  readonly cleanPageUrl: string;
  /** Capability-free endpoint used by every browser transport connection. */
  readonly webSocketUrl: string;
  /** Transient launch URL exchanged for a browser-session credential. */
  readonly bootstrapUrl: string | null;
}

export type GatewayFetch = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

export type GatewaySessionStorage = Pick<Storage, 'getItem' | 'setItem'>;

/**
 * Optional tab-scoped persistence for gateway reconnect state.
 *
 * Browsers may deny access to `sessionStorage` even for an otherwise valid
 * page. Direct gateway pages authenticate with their HttpOnly cookie, so a
 * storage policy must not prevent them from starting or leave a launch
 * capability in the address bar.
 */
export class GatewaySessionStore {
  readonly #storage: () => GatewaySessionStorage;

  constructor(storage: () => GatewaySessionStorage) {
    this.#storage = storage;
  }

  rememberedGateway(): string | null {
    return this.#read(REMEMBERED_GATEWAY_KEY);
  }

  browserSession(gatewayUrl: string): string | null {
    return browserSessionForGateway(this.#read(REMEMBERED_BROWSER_SESSION_KEY), gatewayUrl);
  }

  rememberGateway(gatewayUrl: string): void {
    this.#write(REMEMBERED_GATEWAY_KEY, gatewayUrl);
  }

  rememberBrowserSession(gatewayUrl: string, token: string): void {
    this.#write(REMEMBERED_BROWSER_SESSION_KEY, serializeBrowserSession(gatewayUrl, token));
  }

  #read(key: string): string | null {
    try {
      return this.#storage().getItem(key);
    } catch {
      return null;
    }
  }

  #write(key: string, value: string): void {
    try {
      this.#storage().setItem(key, value);
    } catch {
      // Storage is an optional reconnect aid. The HttpOnly cookie remains the
      // direct gateway's credential, and a harness can use its minted session
      // for the current page lifetime even when persistence is denied.
    }
  }
}

/** Remove every launch capability from a browser-visible URL without validating it. */
export function scrubLaunchCapability(pageHref: string): string {
  const page = new URL(pageHref);
  page.searchParams.delete('token');
  const nested = page.searchParams.get('gateway');
  if (nested === null) return page.toString();
  try {
    const gateway = new URL(nested, page.origin);
    if (!['http:', 'https:'].includes(gateway.protocol)) {
      page.searchParams.delete('gateway');
    } else {
      page.searchParams.set('gateway', new URL('/', gateway.origin).toString());
    }
  } catch {
    page.searchParams.delete('gateway');
  }
  return page.toString();
}

export function resolveGatewayTarget(
  pageHref: string,
  rememberedGateway: string | null,
): GatewayTarget | null {
  const page = new URL(pageHref);
  const nested = page.searchParams.get('gateway');
  if (nested !== null) return targetForGateway(page, new URL(nested, page.origin), true);
  if (page.searchParams.has('token')) return targetForGateway(page, page, false);
  if (rememberedGateway === null) return null;
  let remembered: URL;
  try {
    remembered = new URL(rememberedGateway);
  } catch {
    return null;
  }
  return remembered.origin === page.origin ? targetForGateway(page, remembered, false) : null;
}

export function targetFromCapability(pageHref: string, capabilityUrl: string): GatewayTarget {
  const page = new URL(pageHref);
  return targetForGateway(page, new URL(capabilityUrl.trim(), page.origin), true, true);
}

export async function bootstrapGateway(
  capabilityUrl: string,
  fetcher: GatewayFetch = fetch,
): Promise<string> {
  const response = await fetcher(capabilityUrl, {
    cache: 'no-store',
    credentials: 'include',
    mode: 'cors',
  });
  if (!response.ok) throw new Error(`Gateway rejected the capability (HTTP ${response.status})`);
  const browserSession = response.headers.get(BROWSER_SESSION_HEADER);
  if (browserSession === null || browserSession === '') {
    throw new Error('Gateway did not establish a browser session');
  }
  return browserSession;
}

export function serializeBrowserSession(gatewayUrl: string, token: string): string {
  return JSON.stringify({version: 1, gatewayUrl, token});
}

export function browserSessionForGateway(raw: string | null, gatewayUrl: string): string | null {
  if (raw === null) return null;
  try {
    const value: unknown = JSON.parse(raw);
    if (
      typeof value !== 'object' ||
      value === null ||
      Object.keys(value).sort().join(',') !== 'gatewayUrl,token,version'
    ) {
      return null;
    }
    const session = value as Record<string, unknown>;
    return session['version'] === 1 &&
      session['gatewayUrl'] === gatewayUrl &&
      typeof session['token'] === 'string' &&
      session['token'] !== ''
      ? session['token']
      : null;
  } catch {
    return null;
  }
}

export function webSocketUrlWithSession(webSocketUrl: string, session: string | null): string {
  if (session === null) return webSocketUrl;
  const url = new URL(webSocketUrl);
  url.searchParams.set('session', session);
  return url.toString();
}

function targetForGateway(
  page: URL,
  gateway: URL,
  nested: boolean,
  requireCapability = false,
): GatewayTarget {
  if (!['http:', 'https:'].includes(gateway.protocol)) {
    throw new Error('Use an http:// or https:// gateway URL');
  }
  if (page.protocol === 'https:' && gateway.protocol !== 'https:') {
    throw new Error('An HTTPS browser page requires an HTTPS gateway URL');
  }
  const token = gateway.searchParams.get('token');
  if (requireCapability && !token) {
    throw new Error('The gateway URL must include its capability token');
  }
  const bootstrapUrl = nested && token ? gateway.toString() : null;
  const cleanGateway = new URL('/', gateway.origin);
  const cleanPage = new URL(page);
  cleanPage.searchParams.delete('token');
  if (nested) cleanPage.searchParams.set('gateway', cleanGateway.toString());
  const socket = new URL('/ws', cleanGateway);
  socket.protocol = socket.protocol === 'https:' ? 'wss:' : 'ws:';
  return {
    gatewayUrl: cleanGateway.toString(),
    cleanPageUrl: cleanPage.toString(),
    webSocketUrl: socket.toString(),
    bootstrapUrl,
  };
}
