/**
 * `ConnectionManager`: the only code that creates links and channels. It keeps one `HostConnection`
 * per host, built from that host's checkout, and owns every connection it built and every host's
 * link (an ssh master, shared by every connection to that host, a checkout check included):
 * disposing the manager (quitting) closes every channel, then every link, once. Runs are detached
 * and keep going.
 */
import {DEFAULT_RECONNECT_DELAYS_MS, type ScheduleTimeout} from '@vibesys/backend-client';
import {type HostId, type HostKey, hostKey} from '../../../../common/hosts.js';
import type {ConnectionConfig} from '../state/host-connection.js';
import {HostConnection} from './host-connection.js';
import type {Link} from './link.js';

export const DEFAULT_CONNECTION_CONFIG: ConnectionConfig = {
  delaysMs: DEFAULT_RECONNECT_DELAYS_MS,
  // A helper answers in milliseconds; a start publishes its record before the engine imports. The
  // bound guards hangs and a half-open link the ssh keepalive has not noticed yet.
  deadlineMs: 60_000,
  heartbeatMs: 15_000,
};

export interface ConnectionManagerOptions {
  /** The link to `host` running VibeSys from `checkout`. */
  readonly linkFor: (host: HostId, checkout: string) => Link;
  readonly appVersion: string;
  readonly scheduleTimeout: ScheduleTimeout;
  readonly log: (message: string) => void;
  readonly config?: ConnectionConfig;
}

export class ConnectionManager implements AsyncDisposable {
  readonly #options: ConnectionManagerOptions;
  readonly #stack = new AsyncDisposableStack();
  readonly #connections = new Map<HostKey, {checkout: string; connection: HostConnection}>();
  readonly #created = new Set<(host: HostKey, connection: HostConnection) => void>();
  /** Hosts whose link release is registered: one per host, however many connections use it. */
  readonly #links = new Set<HostKey>();

  constructor(options: ConnectionManagerOptions) {
    this.#options = options;
  }

  /**
   * The one connection to `host` from `checkout`. A changed checkout gets a new connection; the one
   * it replaces keeps serving the runs attached through it until the app quits.
   */
  connection(host: HostId, checkout: string): HostConnection {
    const key = hostKey(host);
    const existing = this.#connections.get(key);
    if (existing?.checkout === checkout) return existing.connection;
    const connection = this.#stack.use(this.#build(host, checkout));
    this.#connections.set(key, {checkout, connection});
    for (const listener of this.#created) listener(key, connection);
    return connection;
  }

  /** The connection to `host`, if one was built. */
  existing(host: HostKey): HostConnection | null {
    return this.#connections.get(host)?.connection ?? null;
  }

  /**
   * A connection to `host` from `checkout` that is not kept: for checking a checkout before it is
   * saved. The caller owns it (`await using`).
   */
  candidate(host: HostId, checkout: string): HostConnection {
    return this.#build(host, checkout);
  }

  /** Hear every connection the manager builds; returns the unsubscribe. */
  onConnection(listener: (host: HostKey, connection: HostConnection) => void): () => void {
    this.#created.add(listener);
    return () => this.#created.delete(listener);
  }

  /**
   * Bring `host`'s link up now, prompting when the host asks: the user's own sign-in. A connection
   * stuck on it then tries again.
   */
  async signIn(host: HostId): Promise<void> {
    await this.#link(host, '~').establish(true);
    this.#connections.get(hostKey(host))?.connection.dispatch({type: 'user-retry'});
  }

  /** The machine woke, or the network came back: every connection checks what it waits on. */
  wake(): void {
    for (const {connection} of this.#connections.values()) connection.dispatch({type: 'wake'});
  }

  async [Symbol.asyncDispose](): Promise<void> {
    this.#connections.clear();
    await this.#stack.disposeAsync();
  }

  /** The link to `host` from `checkout`; the host's link is released once, when the manager is. */
  #link(host: HostId, checkout: string): Link {
    const link = this.#options.linkFor(host, checkout);
    const key = hostKey(host);
    if (!this.#links.has(key)) {
      this.#links.add(key);
      // Deferred before any connection that uses it is added, so it is released after them.
      this.#stack.defer(() => link.release().catch(() => {}));
    }
    return link;
  }

  #build(host: HostId, checkout: string): HostConnection {
    const {appVersion, scheduleTimeout, log, config} = this.#options;
    return new HostConnection({
      link: this.#link(host, checkout),
      appVersion,
      config: config ?? DEFAULT_CONNECTION_CONFIG,
      scheduleTimeout,
      log,
    });
  }
}
