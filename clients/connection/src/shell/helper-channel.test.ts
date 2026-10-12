import {describe, expect, test} from 'bun:test';
import type {PassThrough} from 'node:stream';
import {FakeClock} from '@vibesys/transport/testing';
import {fakeProcess} from '../testing/fake-process.js';
import {type ChannelOptions, ConnectionError, HelperChannel} from './helper-channel.js';

type JsonLine = Record<string, unknown>;

function channelOptions(clock = new FakeClock()): ChannelOptions {
  return {
    where: 'test checkout on this host',
    appVersion: 'test',
    deadlineMs: 60_000,
    scheduleTimeout: clock.scheduleTimeout,
    signal: new AbortController().signal,
    heard: () => undefined,
    explain: () => new ConnectionError('link', 'the test helper ended'),
  };
}

function jsonLines(stream: PassThrough): () => Promise<JsonLine> {
  const queued: JsonLine[] = [];
  const waiting: Array<(line: JsonLine) => void> = [];
  let buffer = '';
  stream.setEncoding('utf8');
  stream.on('data', (chunk: string) => {
    buffer += chunk;
    for (let newline = buffer.indexOf('\n'); newline >= 0; newline = buffer.indexOf('\n')) {
      const line = JSON.parse(buffer.slice(0, newline)) as JsonLine;
      buffer = buffer.slice(newline + 1);
      const resolve = waiting.shift();
      if (resolve === undefined) queued.push(line);
      else resolve(line);
    }
  });
  return () => {
    const line = queued.shift();
    return line === undefined
      ? new Promise<JsonLine>(resolve => waiting.push(resolve))
      : Promise.resolve(line);
  };
}

async function rejection(promise: Promise<unknown>): Promise<Error> {
  try {
    await promise;
  } catch (error) {
    return error as Error;
  }
  throw new Error('resolved instead of rejecting');
}

function connectionFailure(error: Error): ConnectionError {
  expect(error).toBeInstanceOf(ConnectionError);
  if (!(error instanceof ConnectionError)) throw error;
  return error;
}

function reply(process: ReturnType<typeof fakeProcess>, message: JsonLine): void {
  process.stdout.write(`${JSON.stringify(message)}\n`);
}

async function openChannel(
  process: ReturnType<typeof fakeProcess>,
  nextLine: () => Promise<JsonLine>,
  options = channelOptions(),
): Promise<HelperChannel> {
  const opening = HelperChannel.open(process.process, options);
  const request = await nextLine();
  reply(process, {
    type: 'hello',
    request_id: request['request_id'],
    protocol_version: 1,
    features: [],
    vibesys_version: 'test',
    checkout: null,
  });
  return opening;
}

describe('HelperChannel reply correlation', () => {
  test('rejects a valid non-hello reply to the initial hello and terminates the helper', async () => {
    const killed: Array<NodeJS.Signals | number> = [];
    const process = fakeProcess(signal => killed.push(signal));
    const nextLine = jsonLines(process.input);
    const opening = HelperChannel.open(process.process, channelOptions());
    const request = await nextLine();

    reply(process, {type: 'ping', request_id: request['request_id']});

    const error = connectionFailure(await rejection(opening));
    expect(error.reason).toBe('incompatible');
    expect(error.message).toContain('answered hello with ping');
    expect(killed).toEqual(['SIGTERM']);
    expect(await process.process.exit).toBeNull();
  });

  test('one mismatched reply fails every pending request consistently and terminates the helper', async () => {
    const clock = new FakeClock();
    const killed: Array<NodeJS.Signals | number> = [];
    const process = fakeProcess(signal => killed.push(signal));
    const nextLine = jsonLines(process.input);
    const channel = await openChannel(process, nextLine, channelOptions(clock));
    const ping = channel.request({type: 'ping'});
    const listing = channel.request({type: 'instances.list'});
    const pingRequest = await nextLine();
    const listingRequest = await nextLine();
    expect([pingRequest['type'], listingRequest['type']]).toEqual(['ping', 'instances.list']);

    reply(process, {
      type: 'instances.list',
      request_id: pingRequest['request_id'],
      listing: {version: 4, instances: [], ended: [], unreadable: [], unverified: []},
    });

    const [caught, listingError] = await Promise.all([rejection(ping), rejection(listing)]);
    const pingError = connectionFailure(caught);
    expect(pingError.reason).toBe('incompatible');
    expect(pingError.message).toContain('answered ping with instances.list');
    expect(listingError).toBe(pingError);
    expect(await channel.ended).toBe(pingError);
    expect(clock.pending).toBe(0);
    expect(killed).toEqual(['SIGTERM']);
  });

  test('a mismatch stops delivery of later frames already buffered in the same chunk', async () => {
    const process = fakeProcess();
    const nextLine = jsonLines(process.input);
    const channel = await openChannel(process, nextLine);
    const pushes: string[] = [];
    channel.onPush(push => pushes.push(push.watch_id));
    const listing = {version: 4, instances: [], ended: [], unreadable: [], unverified: []};

    process.stdout.write(
      `${JSON.stringify({type: 'instances.changed', watch_id: 'first', listing})}\n${JSON.stringify({type: 'instances.changed', watch_id: 'second', listing})}\n`,
    );
    expect(pushes).toEqual(['first', 'second']);

    const pending = channel.request({type: 'ping'});
    const request = await nextLine();
    process.stdout.write(
      `${JSON.stringify({type: 'instances.list', request_id: request['request_id'], listing})}\n${JSON.stringify({type: 'instances.changed', watch_id: 'after-failure', listing})}\n`,
    );

    const error = connectionFailure(await rejection(pending));
    expect(error.reason).toBe('incompatible');
    expect(pushes).toEqual(['first', 'second']);
    expect(await channel.ended).toBe(error);
  });
});

describe('HelperChannel line buffer bound', () => {
  test('a line that never gets a newline ends the channel instead of growing without end', async () => {
    const killed: Array<NodeJS.Signals | number> = [];
    const process = fakeProcess(signal => killed.push(signal));
    const nextLine = jsonLines(process.input);
    const channel = await openChannel(process, nextLine);
    const pending = channel.request({type: 'ping'});

    process.stdout.write('x'.repeat(5 * 1024 * 1024));

    const error = connectionFailure(await rejection(pending));
    expect(error.reason).toBe('incompatible');
    expect(await channel.ended).toBe(error);
    expect(killed).toEqual(['SIGTERM']);
  });

  test('the bound is exact: a buffered line at the cap is tolerated, one byte past it is not', async () => {
    const CAP = 4 * 1024 * 1024; // mirrors NewlineFramer's MAX_REMAINDER_CHARS
    const killed: Array<NodeJS.Signals | number> = [];
    const process = fakeProcess(signal => killed.push(signal));
    const nextLine = jsonLines(process.input);
    const channel = await openChannel(process, nextLine);

    process.stdout.write('x'.repeat(CAP));
    const ping = channel.request({type: 'ping'});
    // The request reaching the helper's stdin proves the channel is still alive at exactly the
    // cap: a failed channel rejects `request()` before writing anything (see `request()`).
    await nextLine();

    process.stdout.write('x');
    const error = connectionFailure(await rejection(ping));
    expect(error.reason).toBe('incompatible');
    expect(error.message).toContain('without a newline');
    expect(await channel.ended).toBe(error);
    expect(killed).toEqual(['SIGTERM']);
  });
});
