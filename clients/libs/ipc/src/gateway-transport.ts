/** Adapter between the Electron callback bridge and the neutral skeleton transport. */
import type {HostKey} from '@vibesys/hosts';
import type {RunTransport} from '@vibesys/run-skeleton';
import {registerAbort} from '@vibesys/transport';
import type {GatewayBridge} from './gateway.js';

class ByteQueue {
  readonly #queue: Uint8Array[] = [];
  #buffered = 0;
  #ended = false;
  #error: string | undefined;
  #status: number | undefined;
  #wake: (() => void) | undefined;
  readonly finish = (error?: string, status?: number): void => {
    this.#ended = true;
    this.#error = error;
    this.#status = status;
    this.#wake?.();
  };
  readonly receive = (bytes: Uint8Array): void => {
    if (this.#ended) return;
    this.#buffered += bytes.length;
    if (this.#buffered > 16 * 1024 * 1024) {
      this.finish('Stream buffer exceeded');
      return;
    }
    this.#queue.push(bytes);
    this.#wake?.();
  };
  async next(): Promise<IteratorResult<Uint8Array>> {
    const bytes = this.#queue.shift();
    if (bytes) {
      this.#buffered -= bytes.length;
      return {done: false, value: bytes};
    }
    if (this.#ended) {
      if (this.#error !== undefined)
        throw Object.assign(new Error(this.#error), {status: this.#status});
      return {done: true, value: undefined};
    }
    await new Promise<void>(resolve => {
      this.#wake = resolve;
    });
    this.#wake = undefined;
    return this.next();
  }
}

export function gatewayTransport(bridge: GatewayBridge, host: HostKey): RunTransport {
  return {
    async unary(method, request, signal) {
      if (signal.aborted) throw new Error('Cancelled');
      const call = bridge.unary(host, method, request);
      const registration = registerAbort(signal, () => call.cancel());
      if (registration.aborted) call.cancel();
      try {
        return await call.result;
      } finally {
        registration.detach();
      }
    },
    async *stream(method, request, signal) {
      if (signal.aborted) return;
      const queue = new ByteQueue();
      const call = bridge.stream(host, method, request, queue.receive, queue.finish);
      const registration = registerAbort(signal, () => {
        queue.finish();
        call.cancel();
      });
      if (registration.aborted) queue.finish();
      try {
        while (!signal.aborted) {
          const item = await queue.next();
          if (item.done) return;
          yield item.value;
        }
      } finally {
        registration.detach();
        call.cancel();
      }
    },
  };
}
