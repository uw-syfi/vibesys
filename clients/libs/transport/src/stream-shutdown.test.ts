import {describe, it} from 'node:test';
import {closeStreamAndTransport, settleAll} from './stream-shutdown.js';
import {expect} from './test-support/expect.js';

class DeferredClose {
  calls = 0;
  readonly #promise: Promise<void>;
  #resolve: () => void = () => undefined;
  #reject: (error: unknown) => void = () => undefined;

  constructor(
    private readonly label: string,
    private readonly trace: string[],
  ) {
    this.#promise = new Promise((resolve, reject) => {
      this.#resolve = resolve;
      this.#reject = reject;
    });
  }

  close(): Promise<void> {
    this.calls += 1;
    this.trace.push(this.label);
    return this.#promise;
  }

  resolve(): void {
    this.#resolve();
  }

  reject(error: unknown): void {
    this.#reject(error);
  }
}

describe('closeStreamAndTransport', () => {
  it('fences the stream before starting transport cancellation and awaits both', async () => {
    const trace: string[] = [];
    const stream = new DeferredClose('stream', trace);
    const transport = new DeferredClose('transport', trace);
    let settled = false;

    const closing = closeStreamAndTransport(stream, transport).then(() => {
      settled = true;
    });

    expect(trace).toEqual(['stream', 'transport']);
    expect(stream.calls).toBe(1);
    expect(transport.calls).toBe(1);
    transport.resolve();
    await Promise.resolve();
    expect(settled).toBe(false);
    stream.resolve();
    await closing;
    expect(settled).toBe(true);
  });

  it('settles both resources and preserves one close failure', async () => {
    const failure = new Error('stream close failed');
    const stream = new DeferredClose('stream', []);
    const transport = new DeferredClose('transport', []);
    const closing = closeStreamAndTransport(stream, transport);

    stream.reject(failure);
    transport.resolve();

    await expect(closing).rejects.toBe(failure);
    expect(stream.calls).toBe(1);
    expect(transport.calls).toBe(1);
  });

  it('starts the peer after a synchronous throw and reports both failures in owner order', async () => {
    const streamFailure = new Error('stream close threw');
    const transportFailure = new Error('transport close failed');
    const trace: string[] = [];
    const stream = {
      close() {
        trace.push('stream');
        throw streamFailure;
      },
    };
    const transport = {
      close() {
        trace.push('transport');
        return Promise.reject(transportFailure);
      },
    };

    const failure = await closeStreamAndTransport(stream, transport).catch(
      (error: unknown) => error,
    );

    expect(trace).toEqual(['stream', 'transport']);
    expect(failure).toBeInstanceOf(AggregateError);
    expect(failure).toMatchObject({
      message: 'could not close event stream and transport',
      errors: [streamFailure, transportFailure],
    });
  });
});

describe('settleAll', () => {
  it('reports shared nested failures once in stable leaf ownership order', async () => {
    const observerFailure = new Error('observer failed');
    const socketFailure = new Error('socket failed');
    const closing = settleAll(
      [
        Promise.reject(new AggregateError([observerFailure, socketFailure], 'child close failed')),
        Promise.reject(socketFailure),
      ],
      'parent close failed',
    );

    const failure = await closing.catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(AggregateError);
    expect(failure).toMatchObject({
      message: 'parent close failed',
      errors: [observerFailure, socketFailure],
    });
  });
});
