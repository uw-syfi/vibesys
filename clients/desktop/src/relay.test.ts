import {describe, expect, test} from 'bun:test';
import type {MainToPage} from './bridge-protocol.js';
import {HostError} from './host.js';
import {type RelayPort, relay} from './relay.js';
import {FakeHost} from './testing/fake-host.js';
import type {ConnectionHandler} from './testing/fake-network.js';

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
): Promise<{port: FakePort; done: Promise<void>; host: FakeHost}> {
  const host = new FakeHost({server: () => server});
  const {endpoint} = await host.startServer([]);
  const port = new FakePort();
  const done = relay(port, () => host.dial(endpoint));
  await port.until(posted => posted.length > 0);
  return {port, done, host};
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
          {type: 'open'},
          ...lines.map(data => ({type: 'message' as const, data})),
          {type: 'close'},
        ],
      });
      expect(port.closedByMain).toBe(true);
    }
  });

  test('each page message reaches the server as one line', async () => {
    // The server answers each line it reads with that line, so the page sees exactly what arrived.
    const {port, host} = await connected(connection => connection.pipe(connection));
    port.send({type: 'send', data: '{"type":"query.snapshot"}'});
    port.send({type: 'send', data: '{"type":"subscribe"}'});
    await port.until(posted => frames(posted).length === 2);
    expect(frames(port.posted)).toEqual(['{"type":"query.snapshot"}', '{"type":"subscribe"}']);
    await host.close();
  });

  test('a failed dial reports close without open', async () => {
    const port = new FakePort();
    await relay(port, () => Promise.reject(new HostError('unreachable', 'nothing there')));
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
      expect(port.posted).toEqual([{type: 'open'}, {type: 'close'}]);
    });
  }

  test('a send before open ends the connection', async () => {
    const host = new FakeHost({server: () => () => {}});
    const {endpoint} = await host.startServer([]);
    const port = new FakePort();
    const done = relay(port, () => host.dial(endpoint));
    port.send({type: 'send', data: '{}'});
    await done;
    expect(port.posted).toEqual([{type: 'close'}]);
    await host.close();
  });

  test('the page going away destroys the stream', async () => {
    let serverClosed: Promise<void> = Promise.resolve();
    const {port, done} = await connected(connection => {
      serverClosed = new Promise(resolve => connection.once('close', () => resolve()));
      connection.resume();
    });
    port.disconnect();
    await done;
    await serverClosed;
    expect(frames(port.posted)).toEqual([]);
  });

  test('a server that stops delimiting frames ends the connection', async () => {
    const {port, done} = await connected(connection => {
      connection.write('x'.repeat(5 * 1024 * 1024));
    });
    await done;
    expect(port.posted).toEqual([{type: 'open'}, {type: 'close'}]);
  });
});
