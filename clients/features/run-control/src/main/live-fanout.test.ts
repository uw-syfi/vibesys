import {describe, expect, test} from 'bun:test';
import {Duplex, PassThrough} from 'node:stream';
import {BRIDGE_CREDIT_WINDOW_BYTES, type MainToPage} from '@vibesys/ipc';
import {LiveFanout} from './live-fanout.js';
import {type RelayPort, relay} from './relay.js';

class PagePort implements RelayPort {
  readonly posted: MainToPage[] = [];
  #message: (data: unknown) => void = () => undefined;
  #close: () => void = () => undefined;
  #waiters: Array<() => void> = [];

  post(message: MainToPage): void {
    this.posted.push(message);
    for (const wake of this.#waiters.splice(0)) wake();
  }
  onMessage(listener: (data: unknown) => void): void {
    this.#message = listener;
  }
  onClose(listener: () => void): void {
    this.#close = listener;
  }
  close(): void {}
  send(data: string): void {
    this.#message({type: 'send', data: data.trim()});
  }
  grant(): void {
    this.#message({type: 'credit', bytes: BRIDGE_CREDIT_WINDOW_BYTES});
  }
  disconnect(): void {
    this.#close();
  }
  frames(): string[] {
    return this.posted.flatMap(message => (message.type === 'message' ? [message.data] : []));
  }
  async until(predicate: () => boolean): Promise<void> {
    while (!predicate()) await new Promise<void>(resolve => this.#waiters.push(resolve));
  }
}

interface Host {
  readonly stream: Duplex;
  readonly requests: string[];
  send(message: object): void;
  finishRaw(data: string): void;
}

function host(): Host {
  const requests: string[] = [];
  const input = new PassThrough();
  const output = new PassThrough();
  input.on('data', chunk => requests.push(String(chunk)));
  return {
    stream: Duplex.from({writable: input, readable: output}),
    requests,
    send: message => output.write(`${JSON.stringify(message)}\n`),
    finishRaw: data => output.end(data),
  };
}

function collect(stream: Duplex): string[] {
  const frames: string[] = [];
  stream.setEncoding('utf8');
  stream.on('data', (chunk: string) => {
    for (const line of chunk.trim().split('\n')) if (line) frames.push(line);
  });
  return frames;
}

function subscribe(id: string, after = 0): string {
  return `${JSON.stringify({type: 'subscribe', request_id: id, client_id: id, after_sequence: after})}\n`;
}

async function settle(): Promise<void> {
  await Bun.sleep(10);
}

describe('live subscription fanout', () => {
  test('malformed scalar and object frames close the joined port without throwing in main', async () => {
    for (const malformed of ['null\n', '[]\n', '{"type":12}\n']) {
      const server = host();
      const fanout = new LiveFanout();
      const page = new PagePort();
      const done = relay(page, signal => fanout.open('run', async () => server.stream, signal));
      await page.until(() => page.posted.some(message => message.type === 'open'));
      page.grant();
      page.send(subscribe('page'));
      await settle();
      server.send({type: 'subscribed', request_id: 'page', run_id: 'run', latest_sequence: 0});
      await page.until(() => page.frames().length === 1);
      server.finishRaw(malformed);
      await done;
      expect(page.posted.at(-1)).toEqual({type: 'close'});
    }
  });

  test('a truncated host frame reports invalid_frame to every joined port before close', async () => {
    const primary = host();
    const unused = host();
    let dials = 0;
    const fanout = new LiveFanout();
    const dial = async (): Promise<Duplex> => (++dials === 1 ? primary.stream : unused.stream);
    const first = new PagePort();
    const firstDone = relay(first, signal => fanout.open('run', dial, signal));
    await first.until(() => first.posted.some(message => message.type === 'open'));
    first.grant();
    first.send(subscribe('first'));
    await settle();
    primary.send({type: 'subscribed', request_id: 'first', run_id: 'run', latest_sequence: 1});
    primary.send({type: 'event_batch', events: [{sequence: 1}], through_sequence: 1});
    await first.until(() => first.frames().length === 2);
    const second = new PagePort();
    const secondDone = relay(second, signal => fanout.open('run', dial, signal));
    await second.until(() => second.posted.some(message => message.type === 'open'));
    second.grant();
    second.send(subscribe('second'));
    await second.until(() => second.frames().length === 2);

    primary.finishRaw('{"type":"event_batch"');
    await Promise.all([firstDone, secondDone]);
    for (const page of [first, second]) {
      expect(page.frames().map(frame => JSON.parse(frame)['type'])).toEqual([
        'subscribed',
        'event_batch',
        'protocol_error',
      ]);
      expect(JSON.parse(page.frames()[2] ?? '{}')).toEqual({
        type: 'protocol_error',
        code: 'invalid_frame',
        message: 'Server stream ended with an incomplete newline-delimited frame',
      });
      expect(page.posted.at(-1)).toEqual({type: 'close'});
    }
  });

  test('two desktop message ports share the replay and both keep receiving live updates', async () => {
    const primary = host();
    const unused = host();
    let dials = 0;
    const fanout = new LiveFanout();
    const dial = async (): Promise<Duplex> => (++dials === 1 ? primary.stream : unused.stream);
    const first = new PagePort();
    const firstDone = relay(first, signal => fanout.open('run', dial, signal));
    await first.until(() => first.posted.some(message => message.type === 'open'));
    first.grant();
    first.send(subscribe('first'));
    await settle();
    primary.send({type: 'subscribed', request_id: 'first', run_id: 'run', latest_sequence: 1});
    primary.send({type: 'event_batch', events: [{sequence: 1}], through_sequence: 1});
    await first.until(() => first.frames().length === 2);

    const second = new PagePort();
    const secondDone = relay(second, signal => fanout.open('run', dial, signal));
    await second.until(() => second.posted.some(message => message.type === 'open'));
    second.grant();
    second.send(subscribe('second'));
    await second.until(() => second.frames().length === 2);
    expect(dials).toBe(2);
    expect(unused.requests).toEqual([]);
    expect(JSON.parse(second.frames()[0] ?? '{}')['request_id']).toBe('second');
    first.disconnect();
    await firstDone;
    primary.send({type: 'event_batch', events: [{sequence: 2}], through_sequence: 2});
    await second.until(() => second.frames().length === 3);
    expect(JSON.parse(second.frames()[2] ?? '{}')['through_sequence']).toBe(2);
    second.disconnect();
    await secondDone;
  });

  test('a second window gets a complete replay and later live batches from one host subscription', async () => {
    const server = host();
    const extra = host();
    let dials = 0;
    const fanout = new LiveFanout();
    const dial = async (): Promise<Duplex> => {
      dials++;
      return dials === 1 ? server.stream : extra.stream;
    };
    const first = await fanout.open('run', dial, new AbortController().signal);
    const firstFrames = collect(first);
    first.write(subscribe('first'));
    await settle();
    expect(dials).toBe(1);
    expect(server.requests).toEqual([subscribe('first')]);
    server.send({
      type: 'subscribed',
      request_id: 'first',
      client_id: 'first',
      run_id: 'run',
      latest_sequence: 1,
    });
    server.send({type: 'event_batch', events: [{sequence: 1}], through_sequence: 1, store_id: 'a'});
    await settle();

    const second = await fanout.open('run', dial, new AbortController().signal);
    const secondFrames = collect(second);
    second.write(subscribe('second'));
    await settle();
    expect(dials).toBe(2);
    expect(extra.requests).toEqual([]);
    expect(secondFrames.map(frame => JSON.parse(frame)['type'])).toEqual([
      'subscribed',
      'event_batch',
    ]);
    expect(JSON.parse(secondFrames[0] ?? '{}')['request_id']).toBe('second');
    server.send({type: 'event_batch', events: [{sequence: 2}], through_sequence: 2, store_id: 'a'});
    await settle();
    expect(firstFrames).toHaveLength(3);
    expect(secondFrames).toHaveLength(3);
    first.destroy();
    server.send({type: 'event_batch', events: [{sequence: 3}], through_sequence: 3, store_id: 'a'});
    await settle();
    expect(secondFrames).toHaveLength(4);
    second.destroy();
  });

  test('a cursor resume uses the server directly instead of replaying another window’s bootstrap', async () => {
    const servers = [host(), host()];
    let dials = 0;
    const fanout = new LiveFanout();
    const dial = async (): Promise<Duplex> => servers[dials++]?.stream ?? Promise.reject();
    const first = await fanout.open('run', dial, new AbortController().signal);
    collect(first);
    first.write(subscribe('first'));
    await settle();
    const resumed = await fanout.open('run', dial, new AbortController().signal);
    collect(resumed);
    resumed.write(subscribe('resume', 42));
    await settle();
    expect(dials).toBe(2);
    expect(servers[1]?.requests).toEqual([subscribe('resume', 42)]);
    first.destroy();
    resumed.destroy();
  });

  test('a new window joins during handshake and receives a fresh bootstrap after a store replacement', async () => {
    const servers = [host(), host(), host()];
    let dials = 0;
    const fanout = new LiveFanout();
    const dial = async (): Promise<Duplex> => servers[dials++]?.stream ?? Promise.reject();
    const first = await fanout.open('run', dial, new AbortController().signal);
    const firstFrames = collect(first);
    first.write(subscribe('first'));
    const second = await fanout.open('run', dial, new AbortController().signal);
    const secondFrames = collect(second);
    second.write(subscribe('second'));
    await settle();
    servers[0]?.send({type: 'subscribed', request_id: 'first', run_id: 'run', latest_sequence: 1});
    servers[0]?.send({
      type: 'event_batch',
      events: [{sequence: 1}],
      through_sequence: 1,
      store_id: 'old',
    });
    await settle();
    expect(secondFrames.map(frame => JSON.parse(frame)['type'])).toEqual([
      'subscribed',
      'event_batch',
    ]);
    servers[0]?.send({
      type: 'event_batch',
      events: [{sequence: 1}],
      through_sequence: 1,
      store_id: 'new',
      rebootstrap: true,
    });
    await settle();
    const third = await fanout.open('run', dial, new AbortController().signal);
    const thirdFrames = collect(third);
    third.write(subscribe('third'));
    await settle();
    expect(dials).toBe(3);
    expect(servers[2]?.requests).toEqual([]);
    expect(firstFrames.map(frame => JSON.parse(frame)['rebootstrap']).at(-1)).toBe(true);
    expect(thirdFrames.map(frame => JSON.parse(frame)['store_id'] ?? null)).toEqual([null, 'new']);
    expect(JSON.parse(thirdFrames[1] ?? '{}')['rebootstrap']).toBe(false);
    first.destroy();
    second.destroy();
    third.destroy();
  });

  test('a bounded replay stops admitting new shared windows without interrupting existing ones', async () => {
    const servers = [host(), host()];
    let dials = 0;
    const fanout = new LiveFanout(80);
    const dial = async (): Promise<Duplex> => servers[dials++]?.stream ?? Promise.reject();
    const first = await fanout.open('run', dial, new AbortController().signal);
    const firstFrames = collect(first);
    first.write(subscribe('first'));
    await settle();
    servers[0]?.send({type: 'subscribed', request_id: 'first', run_id: 'run', latest_sequence: 0});
    servers[0]?.send({type: 'event_batch', events: [{text: 'x'.repeat(100)}], through_sequence: 1});
    await settle();
    const second = await fanout.open('run', dial, new AbortController().signal);
    collect(second);
    second.write(subscribe('second'));
    await settle();
    expect(dials).toBe(2);
    expect(servers[1]?.requests).toEqual([subscribe('second')]);
    servers[0]?.send({type: 'event_batch', events: [], through_sequence: 2});
    await settle();
    expect(firstFrames).toHaveLength(3);
    first.destroy();
    second.destroy();
  });
});
