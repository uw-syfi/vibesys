/**
 * Optional tab-scoped persistence for gateway reconnect state: raw values only; what they are
 * worth is decided by the pure boot decision (`app/boot.ts`).
 *
 * Browsers may deny access to `sessionStorage` even for an otherwise valid page. Direct gateway
 * pages authenticate with their HttpOnly cookie, so a storage policy must not prevent them from
 * starting or leave a launch capability in the address bar.
 */

const REMEMBERED_GATEWAY_KEY = 'vibesys.gateway-session.v1';
const REMEMBERED_BROWSER_SESSION_KEY = 'vibesys.browser-session.v1';

export type GatewaySessionStorage = Pick<Storage, 'getItem' | 'setItem'>;

/** What a tab remembered from an earlier gateway (raw storage values; null when absent). */
export interface Remembered {
  readonly gateway: string | null;
  readonly browserSession: string | null;
}

export class GatewaySessionStore {
  readonly #storage: () => GatewaySessionStorage;

  constructor(storage: () => GatewaySessionStorage) {
    this.#storage = storage;
  }

  remembered(): Remembered {
    return {
      gateway: this.#read(REMEMBERED_GATEWAY_KEY),
      browserSession: this.#read(REMEMBERED_BROWSER_SESSION_KEY),
    };
  }

  rememberGateway(gatewayUrl: string): void {
    this.#write(REMEMBERED_GATEWAY_KEY, gatewayUrl);
  }

  /** Remember a serialized browser session (`serializeBrowserSession`). */
  rememberBrowserSession(serialized: string): void {
    this.#write(REMEMBERED_BROWSER_SESSION_KEY, serialized);
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
      // Storage is an optional reconnect aid. The HttpOnly cookie remains the direct gateway's
      // credential, and a harness can use its minted session for the current page lifetime even
      // when persistence is denied.
    }
  }
}
