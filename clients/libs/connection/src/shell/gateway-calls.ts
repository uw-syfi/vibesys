/** Shared main-process ownership for the generic bridge: one session per host. */
import type {HostKey, ListingStep} from '@vibesys/hosts';
import {grpcCode, registerAbort, type ScheduleTimeout} from '@vibesys/transport';
import {GatewayConnection, type GatewayDialer, READ_METHOD} from './gateway.js';
import {failureDiagnostic} from './gateway-failures.js';

/** One wire read that every caller asking for the same bytes waits on. */
interface SharedRead {
  readonly abort: AbortController;
  readonly reply: Promise<Uint8Array>;
  callers: number;
}

export class GatewayCalls {
  #closed = false;
  readonly #requests = new Set<AbortController>();
  readonly #hosts = new Map<HostKey, {connection: GatewayConnection; users: number}>();
  readonly #reads = new Map<string, SharedRead>();
  readonly #holds = new Set<() => void>();
  constructor(
    private readonly dialer: (host: HostKey) => GatewayDialer,
    private readonly schedule: ScheduleTimeout,
    /** Told what a failed call left behind: the host's text, or the status the call ended with. */
    private readonly failed: (host: HostKey, diagnostic: string) => void = () => {},
  ) {}

  /**
   * Start the session to `host` now, ahead of its first call, so that what the host needs to
   * start (a gateway process that must be exec'd, a handshake) runs beside whatever the caller is
   * still doing instead of after it. A call that comes within `holdMs` finds the session ready or
   * on its way; a session nobody has used by then is closed, as one with no call in flight is.
   */
  warm(host: HostKey, holdMs: number): void {
    if (this.#closed) return;
    const managed = this.#managed(host);
    managed.users += 1;
    let cancel = (): void => {};
    const release = (): void => {
      cancel();
      this.#holds.delete(release);
      this.#leave(host, managed);
    };
    this.#holds.add(release);
    cancel = this.schedule(release, holdMs);
  }

  unary(
    host: HostKey,
    method: string,
    bytes: Uint8Array,
    signal: AbortSignal,
  ): Promise<Uint8Array> {
    if (method === READ_METHOD) return this.#sharedRead(host, bytes, signal);
    return this.#unary(host, method, bytes, signal);
  }

  /**
   * A read is idempotent, so callers that ask a host for the same bytes while one such read is in
   * flight wait for its one answer instead of each starting a host process. Nothing outlives the
   * answer: the next caller after it reads again. The read is cancelled when its last caller
   * cancels, and a cancelled caller never cancels another.
   */
  #sharedRead(host: HostKey, bytes: Uint8Array, signal: AbortSignal): Promise<Uint8Array> {
    if (signal.aborted)
      return Promise.reject(signal.reason ?? new Error('gateway caller cancelled'));
    const encoded = Buffer.from(bytes.buffer, bytes.byteOffset, bytes.byteLength).toString(
      'base64',
    );
    const key = `${host}\0${encoded}`;
    const shared = this.#reads.get(key) ?? this.#startRead(key, host, bytes);
    shared.callers += 1;
    return new Promise<Uint8Array>((resolve, reject) => {
      let waiting = true;
      const registration = registerAbort(signal, () => {
        if (!waiting) return;
        waiting = false;
        shared.callers -= 1;
        if (shared.callers === 0) {
          this.#forgetRead(key, shared);
          shared.abort.abort();
        }
        reject(signal.reason ?? new Error('gateway caller cancelled'));
      });
      shared.reply.then(
        reply => {
          registration.detach();
          // Each caller owns its bytes: a decoder that keeps or edits them cannot show another.
          // (`slice` of a Node Buffer is a view of the same memory, so copy by element.)
          if (waiting) resolve(new Uint8Array(reply));
          waiting = false;
        },
        (error: unknown) => {
          registration.detach();
          if (waiting) reject(error);
          waiting = false;
        },
      );
    });
  }

  #startRead(key: string, host: HostKey, bytes: Uint8Array): SharedRead {
    const abort = new AbortController();
    const reply = this.#unary(host, READ_METHOD, bytes, abort.signal);
    const shared: SharedRead = {abort, reply, callers: 0};
    this.#reads.set(key, shared);
    const forget = (): void => this.#forgetRead(key, shared);
    reply.then(forget, forget);
    return shared;
  }

  #forgetRead(key: string, shared: SharedRead): void {
    if (this.#reads.get(key) === shared) this.#reads.delete(key);
  }

  #unary(
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
    /** Told each step the host's session reaches on the way to this stream. */
    reached?: (step: ListingStep) => void,
  ): Promise<void> {
    return this.#use(host, method, signal, (connection, owned) =>
      connection.stream(
        method,
        bytes,
        owned,
        event => {
          this.#observe(host, method, event);
          receive(event);
        },
        reached,
      ),
    );
  }

  #observe(host: HostKey, method: string, reply: Uint8Array): void {
    const diagnostic = failureDiagnostic(method, reply);
    if (diagnostic !== null) this.failed(host, diagnostic);
  }

  close(): void {
    this.#closed = true;
    for (const release of [...this.#holds]) release();
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
      const owned = this.#managed(host);
      owned.users += 1;
      try {
        return await call(owned.connection, abort.signal);
      } finally {
        this.#leave(host, owned);
      }
    } finally {
      registration.detach();
      this.#requests.delete(abort);
    }
  }

  /** The host's session, opened (and dialing) when there is none or the last one ended. */
  #managed(host: HostKey): {connection: GatewayConnection; users: number} {
    let managed = this.#hosts.get(host);
    if (managed === undefined || managed.connection.ended) {
      managed = {connection: new GatewayConnection(this.dialer(host), this.schedule), users: 0};
      this.#hosts.set(host, managed);
    }
    return managed;
  }

  /** One user of the session is done; the last one out closes it. */
  #leave(host: HostKey, managed: {connection: GatewayConnection; users: number}): void {
    managed.users -= 1;
    if (managed.users === 0) {
      managed.connection.close();
      if (this.#hosts.get(host) === managed) this.#hosts.delete(host);
    }
  }
}
