import {describe, expect, test} from 'bun:test';
import {Duplex} from 'node:stream';
import {BRIDGE_CREDIT_WINDOW_BYTES, type MainToPage} from '@vibesys/ipc';
import type {ScheduleTimeout} from '@vibesys/transport';
import {FakeClock} from '@vibesys/transport/testing';
import {type RelayPort, relay} from './relay.js';

/** The server's end of one connection. */
type ConnectionHandler = (connection: Duplex) => void;

/** One end of an in-memory connection: what it writes, its peer reads; ending one ends the other. */
class LinkedEnd extends Duplex {
  peer: LinkedEnd | null = null;

  constructor() {
    super({allowHalfOpen: false});
  }

  override _read(): void {}

  override _write(chunk: Buffer, _encoding: BufferEncoding, callback: () => void): void {
    this.peer?.push(chunk);
    callback();
  }

  override _final(callback: () => void): void {
    this.peer?.push(null);
    callback();
  }

  override _destroy(error: Error | null, callback: (error: Error | null) => void): void {
    const peer = this.peer;
    this.peer = null;
    if (peer !== null && !peer.readableEnded) peer.push(null);
    callback(error);
  }
}

/** A server stream that owns written bytes until the test explicitly accepts them. */
class HeldWriteStream extends Duplex {
  readonly writes: Buffer[] = [];
  readonly #completions: Array<() => void> = [];

  override _read(): void {}

  override _write(chunk: Buffer, _encoding: BufferEncoding, callback: () => void): void {
    this.writes.push(Buffer.from(chunk));
    this.#completions.push(callback);
  }

  acceptNext(): void {
    const complete = this.#completions.shift();
    if (complete === undefined) throw new Error('no server write is pending');
    complete();
  }
}

/** A host stream whose terminal event the test owns. */
class ControlledReadStream extends Duplex {
  override _read(): void {}

  override _write(_chunk: Buffer, _encoding: BufferEncoding, callback: () => void): void {
    callback();
  }

  line(data: string): void {
    this.emit('data', `${data}\n`);
  }

  terminate(kind: 'close' | 'error'): void {
    if (kind === 'error') this.emit('error', new Error('host stream failed'));
    else this.emit('close');
  }
}

function duplexPair(): [Duplex, Duplex] {
  const left = new LinkedEnd();
  const right = new LinkedEnd();
  left.peer = right;
  right.peer = left;
  return [left, right];
}

/** The page end of a connection, as the main process sees it through a message port. */
class FakePort implements RelayPort {
  readonly posted: MainToPage[] = [];
  closedByMain = false;
  #onMessage: (data: unknown) => void = () => {};
  #onClose: () => void = () => {};
  #waiters: Array<() => void> = [];

  post(message: MainToPage): void {
    if (this.closedByMain) throw new Error('posted on a closed port');
    this.posted.push(message);
    for (const wake of this.#waiters.splice(0)) wake();
  }
  onMessage(listener: (data: unknown) => void): void {
    this.#onMessage = listener;
  }
  onClose(listener: () => void): void {
    this.#onClose = listener;
  }
  close(): void {
    this.closedByMain = true;
  }

  /** The page sends a message. */
  send(data: unknown): void {
    this.#onMessage(data);
  }
  /** The page goes away. */
  disconnect(): void {
    this.#onClose();
  }
  /** Resolve once the posted messages satisfy `done`. */
  async until(done: (posted: readonly MainToPage[]) => boolean): Promise<void> {
    while (!done(this.posted)) await new Promise<void>(resolve => this.#waiters.push(resolve));
  }
}

function frames(posted: readonly MainToPage[]): string[] {
  return posted.flatMap(message => (message.type === 'message' ? [message.data] : []));
}

/** A deterministic generator (mulberry32), so a failing seed reproduces. */
function random(seed: number): () => number {
  let state = seed;
  return () => {
    state = (state + 0x6d2b79f5) | 0;
    let value = Math.imul(state ^ (state >>> 15), 1 | state);
    value = (value + Math.imul(value ^ (value >>> 7), 61 | value)) ^ value;
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
  };
}

async function connected(
  server: ConnectionHandler,
  grantOutput = true,
  scheduleTimeout?: ScheduleTimeout,
): Promise<{port: FakePort; done: Promise<void>; stream: Duplex; host: Duplex}> {
  const [stream, end] = duplexPair();
  server(end);
  const port = new FakePort();
  const done = relay(port, async () => stream, scheduleTimeout);
  await port.until(posted => posted.length > 0);
  if (grantOutput) port.send({type: 'credit', bytes: BRIDGE_CREDIT_WINDOW_BYTES});
  return {port, done, stream, host: end};
}

function frameBytes(data: string): number {
  return Buffer.byteLength(data, 'utf8') + 1;
}

describe('relay', () => {
  test('server lines reach the page as whole messages, however the bytes are chunked', async () => {
    for (let seed = 1; seed <= 50; seed += 1) {
      const next = random(seed);
      const lines = Array.from({length: 1 + Math.floor(next() * 6)}, (_, index) =>
        JSON.stringify({index, text: 'é✓'.repeat(Math.floor(next() * 5)), seed}),
      );
      const bytes = Buffer.from(lines.map(line => `${line}\n`).join(''), 'utf8');
      const {port, done} = await connected(connection => {
        // Split anywhere, including inside a multi-byte character.
        let offset = 0;
        while (offset < bytes.length) {
          const size = 1 + Math.floor(next() * 7);
          connection.write(bytes.subarray(offset, offset + size));
          offset += size;
        }
        connection.end();
      });
      await done;
      expect({seed, posted: port.posted}).toEqual({
        seed,
        posted: [
          {type: 'open', creditBytes: BRIDGE_CREDIT_WINDOW_BYTES},
          ...lines.map(data => ({type: 'message' as const, data, bytes: frameBytes(data)})),
          {type: 'close'},
        ],
      });
      expect(port.closedByMain).toBe(true);
    }
  });

  test('each page message reaches the server as one line', async () => {
    // The server answers each line it reads with that line, so the page sees exactly what arrived.
    const {port, stream} = await connected(connection => connection.pipe(connection));
    port.send({type: 'send', data: '{"type":"query.snapshot"}'});
    port.send({type: 'send', data: '{"type":"subscribe"}'});
    await port.until(posted => frames(posted).length === 2);
    expect(frames(port.posted)).toEqual(['{"type":"query.snapshot"}', '{"type":"subscribe"}']);
    stream.destroy();
  });

  test('server output spends exact UTF-8 credit and resumes contiguously', async () => {
    const {port, done} = await connected(connection => {
      connection.write('é\nok\n');
    }, false);
    port.send({type: 'credit', bytes: frameBytes('é')});
    await port.until(posted => frames(posted).length === 1);
    expect(frames(port.posted)).toEqual(['é']);

    port.send({type: 'credit', bytes: frameBytes('ok')});
    await port.until(posted => frames(posted).length === 2);
    expect(frames(port.posted)).toEqual(['é', 'ok']);
    port.disconnect();
    await done;
  });

  test('server EOF waits for credited complete frames before closing the page', async () => {
    const {port, done} = await connected(connection => connection.end('first\nsecond\n'), false);
    port.send({type: 'credit', bytes: frameBytes('first')});
    await port.until(posted => frames(posted).length === 1);
    expect(port.posted).toEqual([
      {type: 'open', creditBytes: BRIDGE_CREDIT_WINDOW_BYTES},
      {type: 'message', data: 'first', bytes: frameBytes('first')},
    ]);

    port.send({type: 'credit', bytes: frameBytes('second')});
    await done;
    expect(port.posted).toEqual([
      {type: 'open', creditBytes: BRIDGE_CREDIT_WINDOW_BYTES},
      {type: 'message', data: 'first', bytes: frameBytes('first')},
      {type: 'message', data: 'second', bytes: frameBytes('second')},
      {type: 'close'},
    ]);
  });

  test('a clean host EOF whose page credit never arrives ends the relay at a bounded deadline', async () => {
    const clock = new FakeClock();
    const {port, done} = await connected(
      connection => connection.end('first\nsecond\n'),
      false,
      clock.scheduleTimeout,
    );
    port.send({type: 'credit', bytes: frameBytes('first')});
    await port.until(posted => frames(posted).length === 1);
    expect(port.posted).toEqual([
      {type: 'open', creditBytes: BRIDGE_CREDIT_WINDOW_BYTES},
      {type: 'message', data: 'first', bytes: frameBytes('first')},
    ]);

    // The page never grants the credit "second" needs; nothing but a deadline ends this.
    expect(port.closedByMain).toBe(false);
    expect(clock.pendingDelays()).toEqual([40_000]);
    clock.advanceBy(39_999);
    expect(port.closedByMain).toBe(false);

    // Some progress, even though it cannot deliver the next frame yet, restarts the no-progress
    // deadline rather than charging time elapsed since EOF.
    port.send({type: 'credit', bytes: 1});
    expect(clock.pendingDelays()).toEqual([40_000]);
    clock.advanceBy(39_999);
    expect(port.closedByMain).toBe(false);
    clock.advanceBy(1);
    await done;
    expect(port.posted).toEqual([
      {type: 'open', creditBytes: BRIDGE_CREDIT_WINDOW_BYTES},
      {type: 'message', data: 'first', bytes: frameBytes('first')},
      {type: 'close'},
    ]);
    expect(port.closedByMain).toBe(true);
  });

  test('a clean EOF buffered after credit is exhausted still reaches bounded cleanup', async () => {
    const clock = new FakeClock();
    const {port, done, host} = await connected(() => {}, false, clock.scheduleTimeout);
    port.send({type: 'credit', bytes: frameBytes('first')});

    // Exhaust credit only after the relay is listening. The old relay paused here, so the later
    // tail and EOF remained in Node's readable buffer and its EOF-only deadline never armed.
    host.write('first\n');
    await port.until(posted => frames(posted).length === 1);
    host.end('second\n');
    await Promise.resolve();

    expect(port.closedByMain).toBe(false);
    expect(clock.pendingDelays()).toEqual([40_000]);
    clock.advanceBy(39_999);
    expect(port.closedByMain).toBe(false);
    clock.advanceBy(1);
    expect(port.closedByMain).toBe(true);
    await done;
    expect(port.posted).toEqual([
      {type: 'open', creditBytes: BRIDGE_CREDIT_WINDOW_BYTES},
      {type: 'message', data: 'first', bytes: frameBytes('first')},
      {type: 'close'},
    ]);
  });

  for (const terminal of ['error', 'close'] as const) {
    test(`server ${terminal} closes instead of waiting for page credit`, async () => {
      const port = new FakePort();
      const stream = new ControlledReadStream();
      let settled = false;
      const done = relay(port, async () => stream).then(() => {
        settled = true;
      });
      await port.until(posted => posted.some(message => message.type === 'open'));

      port.send({type: 'credit', bytes: 1});
      stream.line('larger-than-credit');
      stream.terminate(terminal);
      await Promise.resolve();

      expect(port.posted.at(-1)).toEqual({type: 'close'});
      expect(port.closedByMain).toBe(true);
      expect(settled).toBe(true);
      await done;
    });
  }

  test('page input is bounded and returns credit only after the server accepts bytes', async () => {
    const port = new FakePort();
    const stream = new HeldWriteStream();
    const done = relay(port, async () => stream);
    await port.until(posted => posted.some(message => message.type === 'open'));

    const fillsWindow = 'x'.repeat(BRIDGE_CREDIT_WINDOW_BYTES - 1);
    port.send({type: 'send', data: fillsWindow});
    expect(stream.writableLength).toBe(BRIDGE_CREDIT_WINDOW_BYTES);
    expect(port.posted.some(message => message.type === 'credit')).toBe(false);

    stream.acceptNext();
    await port.until(posted => posted.some(message => message.type === 'credit'));
    expect(port.posted.find(message => message.type === 'credit')).toEqual({
      type: 'credit',
      bytes: BRIDGE_CREDIT_WINDOW_BYTES,
    });
    port.disconnect();
    await done;
  });

  test('page input beyond its outstanding byte credit closes before another write', async () => {
    const port = new FakePort();
    const stream = new HeldWriteStream();
    const done = relay(port, async () => stream);
    await port.until(posted => posted.some(message => message.type === 'open'));

    port.send({type: 'send', data: 'x'.repeat(BRIDGE_CREDIT_WINDOW_BYTES - 1)});
    port.send({type: 'send', data: 'overflow'});
    await done;
    expect(stream.writes).toHaveLength(1);
    expect(port.posted.at(-1)).toEqual({type: 'close'});
  });

  test('a failed dial reports close without open', async () => {
    const port = new FakePort();
    await relay(port, () => Promise.reject(new Error('run_gone: nothing there')));
    expect(port.posted).toEqual([{type: 'close'}]);
    expect(port.closedByMain).toBe(true);
  });

  for (const [name, message] of [
    ['a message with a newline', {type: 'send', data: '{"a":1}\n{"b":2}'}],
    ['a non-string payload', {type: 'send', data: {a: 1}}],
    ['an unknown message', {type: 'nope'}],
    ['a close', {type: 'close'}],
  ] as const) {
    test(`${name} from the page ends the connection without reaching the server`, async () => {
      let serverClosed: Promise<void> = Promise.resolve();
      const {port, done} = await connected(connection => {
        serverClosed = new Promise(resolve => connection.once('close', () => resolve()));
        connection.pipe(connection);
      });
      port.send(message);
      await done;
      await serverClosed;
      expect(port.posted).toEqual([
        {type: 'open', creditBytes: BRIDGE_CREDIT_WINDOW_BYTES},
        {type: 'close'},
      ]);
    });
  }

  test('a send before open ends the connection', async () => {
    const port = new FakePort();
    // A dial that has not connected yet (the host is reconnecting).
    const done = relay(
      port,
      signal =>
        new Promise<Duplex>((_resolve, reject) => {
          signal.addEventListener('abort', () => reject(signal.reason), {once: true});
        }),
    );
    port.send({type: 'send', data: '{}'});
    await done;
    expect(port.posted).toEqual([{type: 'close'}]);
  });

  test('the page going away cancels its pending dial and disposes a late stream', async () => {
    const port = new FakePort();
    const dialSignals: AbortSignal[] = [];
    let open = (_stream: Duplex): void => {};
    const done = relay(port, signal => {
      dialSignals.push(signal);
      return new Promise<Duplex>(resolve => {
        open = resolve;
      });
    });
    let settled = false;
    void done.then(() => {
      settled = true;
    });

    expect(dialSignals).toHaveLength(1);
    expect(dialSignals[0]?.aborted).toBe(false);
    port.disconnect();
    await Promise.resolve();
    expect(dialSignals[0]?.aborted).toBe(true);
    expect(dialSignals[0]?.reason).toBeInstanceOf(DOMException);
    expect(settled).toBe(false);

    const late = new ControlledReadStream();
    open(late);
    await done;
    expect(late.destroyed).toBe(true);
    expect(settled).toBe(true);
    expect(port.posted).toEqual([{type: 'close'}]);
  });

  test('the page going away destroys the stream', async () => {
    const [stream, host] = duplexPair();
    const port = new FakePort();
    const dialSignals: AbortSignal[] = [];
    const done = relay(port, async signal => {
      dialSignals.push(signal);
      return stream;
    });
    const serverClosed = new Promise<void>(resolve => host.once('close', () => resolve()));
    host.resume();
    await port.until(posted => posted.some(message => message.type === 'open'));
    expect(dialSignals).toHaveLength(1);
    expect(dialSignals[0]?.aborted).toBe(false);

    port.disconnect();
    await done;
    await serverClosed;
    expect(dialSignals[0]?.aborted).toBe(true);
    expect(frames(port.posted)).toEqual([]);
  });

  test('a server that stops delimiting frames ends the connection', async () => {
    const {port, done} = await connected(connection => {
      connection.write('x'.repeat(5 * 1024 * 1024));
    });
    await done;
    expect(port.posted).toEqual([
      {type: 'open', creditBytes: BRIDGE_CREDIT_WINDOW_BYTES},
      {type: 'close'},
    ]);
  });
});
