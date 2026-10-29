/**
 * `ConnectionManager`: the only code that creates links and channels. It keeps one `Link` per host
 * (the host's one ssh master, so concurrent connections never race two sign-ins) and one shown
 * `HostConnection` per host, built from that host's current checkout. Every caller owns a lease;
 * releasing the final shown lease retires its connection, and the link ends after its final
 * connection and direct operation. A changed checkout or retired host keeps serving an attached
 * run until that exact connection's hold ends. Disposing the manager closes everything once.
 */

import {type HostId, type HostKey, hostKey} from '@vibesys/hosts';
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

/** One caller's ownership of the exact connection it received. Release is idempotent. */
export interface ConnectionLease extends AsyncDisposable {
  readonly connection: HostConnection;
}

interface ManagedLink {
  readonly link: Link;
  readonly connections: Set<HostConnection>;
  operations: number;
}

class Lease implements ConnectionLease {
  readonly connection: HostConnection;
  readonly #release: () => Promise<void>;
  #released = false;

  constructor(connection: HostConnection, release: () => Promise<void>) {
    this.connection = connection;
    this.#release = release;
  }

  async [Symbol.asyncDispose](): Promise<void> {
    if (this.#released) return;
    this.#released = true;
    await this.#release();
  }
}

export class ConnectionManager implements AsyncDisposable {
  readonly #options: ConnectionManagerOptions;
  readonly #links = new Map<HostKey, ManagedLink>();
  /** Each host's shown connection: the one built from its current checkout. */
  readonly #shown = new Map<HostKey, {checkout: string; connection: HostConnection}>();
  /** Manager leases on each exact connection; `HostConnection.hold` also protects direct holds. */
  readonly #leases = new Map<HostConnection, number>();
  /** Connections a changed checkout replaced, open while a run is attached through them. */
  readonly #replaced = new Set<HostConnection>();
  /** The one close task per connection, which the manager's own release waits for. */
  readonly #closing = new Map<HostConnection, Promise<void>>();
  /** Link releases under way after their final connection or operation ended. */
  readonly #releasing = new Set<Promise<void>>();
  /** A host cannot be reopened until its prior ssh control master has finished exiting. */
  readonly #retiring = new Map<HostKey, Promise<void>>();
  readonly #disposalListeners = new Set<() => void>();
  readonly #created = new Set<(host: HostKey, connection: HostConnection) => void>();
  #disposed = false;
  #disposing: Promise<void> | null = null;

  constructor(options: ConnectionManagerOptions) {
    this.#options = options;
  }

  /** Retire any additional transport owners before this manager releases its host links. */
  onDisposing(listener: () => void): void {
    if (this.#disposed) listener();
    else this.#disposalListeners.add(listener);
  }

  /**
   * Lease the one shown connection to `host` from `checkout`. A changed checkout gets a new
   * connection; the one it replaces stays alive until every lease and attached-run hold ends.
   */
  async acquire(host: HostId, checkout: string): Promise<ConnectionLease> {
    const key = hostKey(host);
    for (
      let retirement = this.#retiring.get(key);
      retirement !== undefined;
      retirement = this.#retiring.get(key)
    )
      await retirement;
    this.#active();
    const existing = this.#shown.get(key);
    let connection: HostConnection;
    let created = false;
    if (existing?.checkout === checkout) {
      connection = existing.connection;
    } else {
      connection = this.#build(host, checkout);
      this.#shown.set(key, {checkout, connection});
      if (existing !== undefined) this.#replace(existing.connection);
      created = true;
    }
    this.#leases.set(connection, (this.#leases.get(connection) ?? 0) + 1);
    const releaseHold = connection.hold();
    const lease = new Lease(connection, () => this.#releaseLease(key, connection, releaseHold));
    if (created) this.#notifyCreated(key, connection);
    return lease;
  }

  /** The connection to `host`, if one was built. */
  existing(host: HostKey): HostConnection | null {
    return this.#shown.get(host)?.connection ?? null;
  }

  /**
   * A connection to `host` from `checkout` that is not kept: for checking a checkout before it is
   * saved. It shares the host's link; the caller owns it (`await using`).
   */
  async candidate(host: HostId, checkout: string): Promise<HostConnection> {
    const key = hostKey(host);
    for (
      let retirement = this.#retiring.get(key);
      retirement !== undefined;
      retirement = this.#retiring.get(key)
    )
      await retirement;
    this.#active();
    return this.#build(host, checkout);
  }

  /** Hear every connection the manager builds; failures are reported and results are not awaited. */
  onConnection(listener: (host: HostKey, connection: HostConnection) => void): () => void {
    this.#created.add(listener);
    return () => this.#created.delete(listener);
  }

  /** Notify after ownership is complete; a non-owning observer cannot interrupt acquisition. */
  #notifyCreated(host: HostKey, connection: HostConnection): void {
    for (const listener of [...this.#created]) {
      try {
        const notified = listener(host, connection);
        void Promise.resolve(notified).catch(error => {
          this.#log(
            `${connection.where}: could not notify a connection listener: ${errorMessage(error)}`,
          );
        });
      } catch (error) {
        this.#log(
          `${connection.where}: could not notify a connection listener: ${errorMessage(error)}`,
        );
      }
    }
  }

  /** Diagnostics are fire-and-forget and cannot change resource ownership or its public result. */
  #log(message: string): void {
    try {
      const logged = this.#options.log(message);
      void Promise.resolve(logged).catch(() => {
        // The diagnostic has no later owner; consume its rejected thenable here.
      });
    } catch {
      // Diagnostics cannot revoke ownership or replace the failure being reported.
    }
  }

  /**
   * Bring `host`'s link up now, prompting when the host asks: the user's own sign-in. A connection
   * stuck on it then tries again, including one retained for an attached run after its checkout
   * was replaced.
   */
  async signIn(host: HostId): Promise<void> {
    const key = hostKey(host);
    for (
      let retirement = this.#retiring.get(key);
      retirement !== undefined;
      retirement = this.#retiring.get(key)
    )
      await retirement;
    this.#active();
    const managed = this.#managedLink(host);
    managed.operations += 1;
    try {
      await managed.link.establish(true);
      // A checkout-replaced connection can still own an attached run. It shares this link and the
      // user's authentication result, so retry every live connection for the host rather than
      // only the one currently shown in the welcome view.
      for (const connection of managed.connections) connection.dispatch({type: 'user-retry'});
    } finally {
      managed.operations -= 1;
      await this.#releaseUnused(key, managed);
    }
  }

  /** Hold the existing host link while a second transport owns a channel on it. */
  async leaseLink(host: HostId): Promise<{readonly link: Link; release(): Promise<void>}> {
    const key = hostKey(host);
    for (
      let retiring = this.#retiring.get(key);
      retiring !== undefined;
      retiring = this.#retiring.get(key)
    ) {
      await retiring;
    }
    this.#active();
    const managed = this.#managedLink(host);
    managed.operations += 1;
    let released = false;
    return {
      link: managed.link,
      release: async () => {
        if (released) return;
        released = true;
        managed.operations -= 1;
        await this.#releaseUnused(key, managed);
      },
    };
  }

  /** The machine woke, or the network came back: every connection checks what it waits on. */
  wake(): void {
    // One link can serve several shown and replaced connections. Retire its stale attempt once,
    // before any connection starts the fresh operation that all of them may share.
    for (const {link} of this.#links.values()) link.networkChanged();
    for (const {connection} of this.#shown.values()) connection.dispatch({type: 'wake'});
    for (const connection of this.#replaced) connection.dispatch({type: 'wake'});
  }

  async [Symbol.asyncDispose](): Promise<void> {
    this.#disposing ??= this.#dispose();
    await this.#disposing;
  }

  /** `connection` is no longer shown: close it once no run is attached through it. */
  #replace(connection: HostConnection): Promise<void> {
    if (this.#replaced.has(connection)) return Promise.resolve();
    this.#replaced.add(connection);
    let closing = Promise.resolve();
    const close = (): void => {
      stop();
      if (this.#replaced.delete(connection)) closing = this.#close(connection);
    };
    const stop = connection.onUnheld(close);
    if (!connection.held) close();
    return closing;
  }

  #close(connection: HostConnection): Promise<void> {
    const existing = this.#closing.get(connection);
    if (existing !== undefined) return existing;
    const closing = connection[Symbol.asyncDispose]();
    this.#closing.set(connection, closing);
    void closing.then(
      () => this.#closing.delete(connection),
      () => this.#closing.delete(connection),
    );
    return closing;
  }

  #releaseLease(key: HostKey, connection: HostConnection, releaseHold: () => void): Promise<void> {
    releaseHold();
    const leases = (this.#leases.get(connection) ?? 1) - 1;
    if (leases > 0) {
      this.#leases.set(connection, leases);
      return Promise.resolve();
    }
    this.#leases.delete(connection);
    if (this.#shown.get(key)?.connection !== connection) return Promise.resolve();
    this.#shown.delete(key);
    return this.#replace(connection);
  }

  /** The one managed link to `host`. */
  #managedLink(host: HostId): ManagedLink {
    const key = hostKey(host);
    let managed = this.#links.get(key);
    if (managed === undefined) {
      managed = {link: this.#options.linkFor(host), connections: new Set(), operations: 0};
      this.#links.set(key, managed);
    }
    return managed;
  }

  #build(host: HostId, checkout: string): HostConnection {
    const key = hostKey(host);
    const managed = this.#managedLink(host);
    const {appVersion, scheduleTimeout, config} = this.#options;
    let connection: HostConnection;
    connection = new HostConnection({
      link: managed.link,
      checkout,
      appVersion,
      config: config ?? DEFAULT_CONNECTION_CONFIG,
      scheduleTimeout,
      log: message => this.#log(message),
      releaseStarted: release => this.#admissionBarrier(key, release),
      released: failure => {
        if (failure === null) {
          managed.connections.delete(connection);
          void this.#releaseUnused(key, managed);
          return;
        }
        this.#releaseFailed(key, connection, failure);
      },
    });
    managed.connections.add(connection);
    return connection;
  }

  #releaseUnused(key: HostKey, managed: ManagedLink): Promise<void> {
    if (
      this.#disposed ||
      this.#links.get(key) !== managed ||
      managed.connections.size > 0 ||
      managed.operations > 0
    ) {
      return Promise.resolve();
    }
    this.#links.delete(key);
    const releasing = managed.link.release();
    this.#admissionBarrier(key, releasing);
    this.#releasing.add(releasing);
    void releasing.then(
      () => {
        this.#releasing.delete(releasing);
      },
      error => {
        // A failed release means the old control master may still own this host's control path.
        // Keep the rejected promise as a terminal admission barrier: replacing the link could run
        // two masters against the same path. A later host operation and manager disposal surface
        // the original failure, while this observer prevents an unhandled background rejection.
        this.#log(`could not retire ${key}: ${errorMessage(error)}`);
      },
    );
    return releasing;
  }

  /** Join `next` to every earlier close/release that must settle before this host is admitted. */
  #admissionBarrier(key: HostKey, next: Promise<void>): void {
    const previous = this.#retiring.get(key);
    const barrier =
      previous === undefined ? next : Promise.all([previous, next]).then(() => undefined);
    this.#retiring.set(key, barrier);
    void barrier.then(
      () => {
        if (this.#retiring.get(key) === barrier) this.#retiring.delete(key);
      },
      () => {
        // A rejected barrier is retained: a new link must not overlap an unretired resource.
      },
    );
  }

  #releaseFailed(key: HostKey, connection: HostConnection, failure: Error): void {
    this.#log(`could not retire ${connection.where}: ${failure.message}`);
    // `releaseStarted` already published the rejecting release as this host's barrier. Keeping the
    // connection in its managed link also prevents that link from being retired underneath it.
    if (!this.#retiring.has(key)) {
      const rejected = Promise.reject(failure);
      void rejected.catch(() => {});
      this.#admissionBarrier(key, rejected);
    }
  }

  async #dispose(): Promise<void> {
    this.#disposed = true;
    for (const listener of this.#disposalListeners) {
      try {
        listener();
      } catch (error) {
        this.#log(`could not retire a transport: ${errorMessage(error)}`);
      }
    }
    this.#disposalListeners.clear();
    this.#shown.clear();
    this.#replaced.clear();
    this.#leases.clear();
    const managed = [...this.#links.values()];
    const connections = new Set(managed.flatMap(entry => [...entry.connections]));
    const closing = [...connections].map(connection => this.#close(connection));
    const closed = await Promise.allSettled(closing);
    const releases = [
      ...managed.filter(entry => entry.connections.size === 0).map(entry => entry.link.release()),
      ...this.#releasing,
    ];
    const retired = await Promise.allSettled(releases);
    this.#links.clear();
    this.#retiring.clear();
    this.#releasing.clear();
    this.#closing.clear();
    const failed = [...closed, ...retired].find(
      (outcome): outcome is PromiseRejectedResult => outcome.status === 'rejected',
    );
    if (failed !== undefined) throw failed.reason;
  }

  #active(): void {
    if (this.#disposed) throw new Error('the connection manager was released');
  }
}

function errorMessage(error: unknown): string {
  try {
    return error instanceof Error ? String(error.message) : String(error);
  } catch {
    return 'an unprintable failure';
  }
}
