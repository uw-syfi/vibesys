/** Shared main-process ownership for the generic bridge: one session per host. */
import type {HostKey} from '@vibesys/hosts';
import {grpcCode, registerAbort, type ScheduleTimeout} from '@vibesys/transport';
import {GatewayConnection, type GatewayDialer} from './gateway.js';
import {failureDiagnostic} from './gateway-failures.js';

export class GatewayCalls {
  #closed = false;
  readonly #requests = new Set<AbortController>();
  readonly #hosts = new Map<HostKey, {connection: GatewayConnection; users: number}>();
  constructor(
    private readonly dialer: (host: HostKey) => GatewayDialer,
    private readonly schedule: ScheduleTimeout,
    /** Told what a failed call left behind: the host's text, or the status the call ended with. */
    private readonly failed: (host: HostKey, diagnostic: string) => void = () => {},
  ) {}

  unary(
    host: HostKey,
    method: string,
    bytes: Uint8Array,
    signal: AbortSignal,
  ): Promise<Uint8Array> {
    return this.#use(host, method, signal, async (connection, owned) => {
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
    return this.#use(host, method, signal, (connection, owned) =>
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
    this.#hosts.get(host)?.connection.close();
    this.#hosts.delete(host);
  }

  /** A call its caller did not cancel and that fails is named once: method and status, no bytes. */
  async #use<T>(
    host: HostKey,
    method: string,
    signal: AbortSignal,
    call: (connection: GatewayConnection, owned: AbortSignal) => Promise<T>,
  ): Promise<T> {
    try {
      return await this.#owned(host, signal, call);
    } catch (error) {
      if (!signal.aborted) {
        const code = grpcCode(error);
        const status = code === undefined ? 'no status' : `code ${code}`;
        this.failed(host, `${method} (${status}): ${String(error).slice(0, 512)}`);
      }
      throw error;
    }
  }

  async #owned<T>(
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
      if (this.#closed || abort.signal.aborted) throw new Error('gateway caller cancelled');
      let managed = this.#hosts.get(host);
      if (managed === undefined || managed.connection.ended) {
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
