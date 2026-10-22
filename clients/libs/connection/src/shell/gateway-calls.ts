/** Shared main-process ownership for the generic bridge: one session per opted-in host. */
import type {HostKey} from '@vibesys/hosts';
import {registerAbort, type ScheduleTimeout} from '@vibesys/transport';
import {GatewayConnection, type GatewayDialer} from './gateway.js';
import {failureDiagnostic} from './gateway-failures.js';

export class GatewayCalls {
  #closed = false;
  readonly #requests = new Set<AbortController>();
  readonly #generations = new Map<HostKey, number>();
  readonly #hosts = new Map<HostKey, {connection: GatewayConnection; users: number}>();
  constructor(
    private readonly enabled: (host: HostKey) => Promise<boolean>,
    private readonly dialer: (host: HostKey) => GatewayDialer,
    private readonly schedule: ScheduleTimeout,
    /** Told what the host wrote about a failed call, which no screen shows. */
    private readonly failed: (host: HostKey, diagnostic: string) => void = () => {},
  ) {}

  unary(
    host: HostKey,
    method: string,
    bytes: Uint8Array,
    signal: AbortSignal,
  ): Promise<Uint8Array> {
    return this.#use(host, signal, async (connection, owned) => {
      const reply = await connection.unary(method, bytes, owned);
      this.#observe(host, method, reply);
      return reply;
    });
  }

  stream(
    host: HostKey,
    method: string,
    bytes: Uint8Array,
    signal: AbortSignal,
    receive: (bytes: Uint8Array) => void,
  ): Promise<void> {
    return this.#use(host, signal, (connection, owned) =>
      connection.stream(method, bytes, owned, event => {
        this.#observe(host, method, event);
        receive(event);
      }),
    );
  }

  #observe(host: HostKey, method: string, reply: Uint8Array): void {
    const diagnostic = failureDiagnostic(method, reply);
    if (diagnostic !== null) this.failed(host, diagnostic);
  }

  close(): void {
    this.#closed = true;
    for (const abort of this.#requests) abort.abort();
    for (const host of this.#hosts.keys()) this.disable(host);
  }

  disable(host: HostKey): void {
    this.#generations.set(host, (this.#generations.get(host) ?? 0) + 1);
    this.#hosts.get(host)?.connection.close();
    this.#hosts.delete(host);
  }

  #enabled(host: HostKey, signal: AbortSignal): Promise<boolean> {
    return new Promise((resolve, reject) => {
      let settled = false;
      let detach = (): void => {};
      let cancel = (): void => {};
      const finish = (enabled: boolean, error?: unknown): void => {
        if (settled) return;
        settled = true;
        detach();
        cancel();
        if (error !== undefined) reject(error);
        else resolve(enabled);
      };
      const registration = registerAbort(signal, () =>
        finish(false, new Error('gateway caller cancelled')),
      );
      detach = registration.detach;
      if (registration.aborted) finish(false, new Error('gateway caller cancelled'));
      if (!settled) {
        cancel = this.schedule(() => finish(false, new Error('gateway setting timed out')), 15_000);
        void Promise.resolve()
          .then(() => (settled ? false : this.enabled(host)))
          .then(
            value => finish(value),
            error => finish(false, error),
          );
      }
    });
  }

  async #use<T>(
    host: HostKey,
    signal: AbortSignal,
    call: (connection: GatewayConnection, owned: AbortSignal) => Promise<T>,
  ): Promise<T> {
    if (this.#closed) throw new Error('gateway bridge closed');
    const abort = new AbortController();
    const registration = registerAbort(signal, () => abort.abort());
    if (registration.aborted) abort.abort();
    this.#requests.add(abort);
    try {
      const generation = this.#generations.get(host);
      if (!(await this.#enabled(host, abort.signal))) throw new Error('Run transport v2 is off');
      if (this.#closed || abort.signal.aborted || this.#generations.get(host) !== generation)
        throw new Error('gateway caller cancelled');
      let managed = this.#hosts.get(host);
      if (managed === undefined) {
        managed = {connection: new GatewayConnection(this.dialer(host), this.schedule), users: 0};
        this.#hosts.set(host, managed);
      }
      const owned = managed;
      owned.users += 1;
      try {
        return await call(owned.connection, abort.signal);
      } finally {
        owned.users -= 1;
        if (owned.users === 0) {
          owned.connection.close();
          if (this.#hosts.get(host) === owned) this.#hosts.delete(host);
        }
      }
    } finally {
      registration.detach();
      this.#requests.delete(abort);
    }
  }
}
