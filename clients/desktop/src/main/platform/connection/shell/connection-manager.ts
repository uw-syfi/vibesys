/**
 * `ConnectionManager`: the only code that creates links and channels. It keeps one `Link` per host
 * (the host's one ssh master, so concurrent connections never race two sign-ins) and one shown
 * `HostConnection` per host, built from that host's current checkout. When the checkout changes,
 * the connection it replaces closes as soon as no run is attached through it (`hold`). Disposing
 * the manager (quitting) closes every connection, waits for their channels to end, then releases
 * every link, once. Runs are detached and keep going.
 */

import {type HostId, type HostKey, hostKey} from '@vibesys/entities';
import {DEFAULT_RECONNECT_DELAYS_MS, type ScheduleTimeout} from '@vibesys/transport';
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
  /** The link to `host`; asked once per host. */
  readonly linkFor: (host: HostId) => Link;
  readonly appVersion: string;
  readonly scheduleTimeout: ScheduleTimeout;
  readonly log: (message: string) => void;
  readonly config?: ConnectionConfig;
}

export class ConnectionManager implements AsyncDisposable {
  readonly #options: ConnectionManagerOptions;
  readonly #links = new Map<HostKey, Link>();
  /** Each host's shown connection: the one built from its current checkout. */
  readonly #shown = new Map<HostKey, {checkout: string; connection: HostConnection}>();
  /** Connections a changed checkout replaced, open while a run is attached through them. */
  readonly #replaced = new Set<HostConnection>();
  /** Closes under way, which the manager's own release waits for. */
  readonly #closing = new Set<Promise<void>>();
  readonly #created = new Set<(host: HostKey, connection: HostConnection) => void>();

  constructor(options: ConnectionManagerOptions) {
    this.#options = options;
  }

  /**
   * The one connection to `host` from `checkout`. A changed checkout gets a new connection; the one
   * it replaces keeps serving the runs attached through it, and closes once none is.
   */
  connection(host: HostId, checkout: string): HostConnection {
    const key = hostKey(host);
    const existing = this.#shown.get(key);
    if (existing?.checkout === checkout) return existing.connection;
    const connection = this.#build(host, checkout);
    this.#shown.set(key, {checkout, connection});
    for (const listener of this.#created) listener(key, connection);
    if (existing !== undefined) this.#replace(existing.connection);
    return connection;
  }

  /** The connection to `host`, if one was built. */
  existing(host: HostKey): HostConnection | null {
    return this.#shown.get(host)?.connection ?? null;
  }

  /**
   * A connection to `host` from `checkout` that is not kept: for checking a checkout before it is
   * saved. It shares the host's link; the caller owns it (`await using`).
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
    await this.#link(host).establish(true);
    this.#shown.get(hostKey(host))?.connection.dispatch({type: 'user-retry'});
  }

  /** The machine woke, or the network came back: every connection checks what it waits on. */
  wake(): void {
    for (const {connection} of this.#shown.values()) connection.dispatch({type: 'wake'});
    for (const connection of this.#replaced) connection.dispatch({type: 'wake'});
  }

  async [Symbol.asyncDispose](): Promise<void> {
    const shown = [...this.#shown.values()].map(entry => entry.connection);
    const connections = [...shown, ...this.#replaced];
    this.#shown.clear();
    this.#replaced.clear();
    for (const connection of connections) this.#close(connection);
    // Every channel rides its host's link: the links go last.
    await Promise.all(this.#closing);
    const links = [...this.#links.values()];
    this.#links.clear();
    await Promise.all(links.map(link => link.release().catch(() => {})));
  }

  /** `connection` is no longer shown: close it once no run is attached through it. */
  #replace(connection: HostConnection): void {
    this.#replaced.add(connection);
    const close = (): void => {
      stop();
      if (this.#replaced.delete(connection)) this.#close(connection);
    };
    const stop = connection.onUnheld(close);
    if (!connection.held) close();
  }

  #close(connection: HostConnection): void {
    // A connection's release settles every channel it started; it has nothing to report.
    const closing = connection[Symbol.asyncDispose]().catch(() => {});
    this.#closing.add(closing);
    void closing.then(() => this.#closing.delete(closing));
  }

  /** The one link to `host`. */
  #link(host: HostId): Link {
    const key = hostKey(host);
    let link = this.#links.get(key);
    if (link === undefined) {
      link = this.#options.linkFor(host);
      this.#links.set(key, link);
    }
    return link;
  }

  #build(host: HostId, checkout: string): HostConnection {
    const {appVersion, scheduleTimeout, log, config} = this.#options;
    return new HostConnection({
      link: this.#link(host),
      checkout,
      appVersion,
      config: config ?? DEFAULT_CONNECTION_CONFIG,
      scheduleTimeout,
      log,
    });
  }
}
