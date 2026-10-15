/**
 * The startup log panel's store over a Fake `RunLogSource` whose listings and reads the test
 * settles in a chosen order, on a `FakeClock`: what the panel shows, when it reads, and that
 * collapsing or hiding it stops reading.
 */
import {describe, expect, test} from 'bun:test';
import type {LogChunk, LogStream} from '@vibesys/contracts/host-helper';
import type {LogListOutcome, LogReadInput, LogReadOutcome, RunLogSource} from '@vibesys/logs';
import type {AbortSignalLike} from '@vibesys/transport';
import {FakeClock} from '@vibesys/transport/testing';
import {createStartupLogStore, type StartupLogStore} from './startup-log-store.js';

const INSTANCE = '0123456789ab';

interface Pending<I, T> {
  readonly input: I;
  readonly signal: AbortSignalLike;
  readonly resolve: (value: T) => void;
}

/** A `RunLogSource` whose every request waits until the test answers it. */
class FakeLogSource implements RunLogSource {
  readonly lists: Pending<null, LogListOutcome>[] = [];
  readonly reads: Pending<LogReadInput, LogReadOutcome>[] = [];

  list(signal: AbortSignalLike): Promise<LogListOutcome> {
    return new Promise(resolve => this.lists.push({input: null, signal, resolve}));
  }

  read(input: LogReadInput, signal: AbortSignalLike): Promise<LogReadOutcome> {
    return new Promise(resolve => this.reads.push({input, signal, resolve}));
  }

  /** Answer the newest listing with `streams`. */
  listed(streams: readonly LogStream[]): void {
    this.lists.at(-1)?.resolve({ok: true, instanceId: INSTANCE, streams});
  }

  /** The newest read still waiting; it throws when there is none. */
  lastRead(): Pending<LogReadInput, LogReadOutcome> {
    const read = this.reads.at(-1);
    if (read === undefined) throw new Error('no read was asked for');
    return read;
  }
}

function stream(id: string, owner: Record<string, unknown>): LogStream {
  return {id, owner, command: 'docker', retained_start: 0, size: 0, outcome: null};
}

const BUILD = stream('000001', {kind: 'startup_step', stage: 'building_image'});
const CONTAINER = stream('000002', {kind: 'startup_step', stage: 'starting_container'});
const GATE = stream('000003', {kind: 'gate_stage', execution_id: 'e', gate: 'g', attempt: 1});

function chunk(offset: number, data: string, closed = false): LogChunk {
  const next = offset + new TextEncoder().encode(data).length;
  return {
    offset,
    data,
    next_offset: next,
    eof: true,
    closed,
    truncated_before: false,
    pipes: [{offset, length: next - offset, pipe: 'stdout'}],
  };
}

/** Let every settled promise's continuation run; no timer is involved. */
async function settle(): Promise<void> {
  for (let turn = 0; turn < 20; turn += 1) await Promise.resolve();
}

function open(source: FakeLogSource, clock = new FakeClock()): StartupLogStore {
  const store = createStartupLogStore(source, clock.scheduleTimeout, {retryMs: 1000});
  store.send({type: 'observe', visible: true, stage: 'building_image'});
  return store;
}

describe('startup log panel', () => {
  test('a starting run shows the newest startup stream, with lines arriving across chunks', async () => {
    const source = new FakeLogSource();
    const store = open(source);
    expect(store.getState()).toMatchObject({visible: true, expanded: true, listing: 'loading'});

    source.listed([BUILD, GATE]);
    await settle();
    expect(store.getState().streams.map(item => item.id)).toEqual(['000001']);
    expect(source.lastRead().input).toMatchObject({
      instance_id: INSTANCE,
      stream_id: '000001',
      start: {tail_bytes: expect.any(Number)},
    });

    const first = chunk(0, 'building image 6/8: COPY\nbuilding image 7/8: WOR');
    source.lastRead().resolve({ok: true, chunk: first});
    await settle();
    expect(store.getState().lines).toEqual(['building image 6/8: COPY', 'building image 7/8: WOR']);

    const next = source.lastRead();
    expect(next.input.start).toEqual({offset: first.next_offset});
    next.resolve({ok: true, chunk: chunk(first.next_offset, 'KDIR\nbuilding image 8/8: RUN\n')});
    await settle();
    expect(store.getState().lines).toEqual([
      'building image 6/8: COPY',
      'building image 7/8: WORKDIR',
      'building image 8/8: RUN',
    ]);
    store.close();
  });

  test('a new stage lists again and follows its stream unless the user picked one', async () => {
    const source = new FakeLogSource();
    const store = open(source);
    source.listed([BUILD]);
    await settle();
    const building = source.lastRead();

    store.send({type: 'observe', visible: true, stage: 'starting_container'});
    source.listed([BUILD, CONTAINER]);
    await settle();
    expect(building.signal.aborted).toBe(true);
    expect(source.lastRead().input.stream_id).toBe('000002');
    expect(store.getState().shown).toBe('000002');

    store.send({type: 'select', streamId: '000001'});
    await settle();
    expect(source.lastRead().input.stream_id).toBe('000001');
    store.send({type: 'observe', visible: true, stage: 'starting_container'});
    expect(source.lists).toHaveLength(2);
    store.close();
  });

  test('the panel hides once the run is ready, which stops its read', async () => {
    const source = new FakeLogSource();
    const store = open(source);
    source.listed([BUILD]);
    await settle();
    const read = source.lastRead();

    store.send({type: 'observe', visible: false, stage: 'starting_container'});
    expect(store.getState().visible).toBe(false);
    expect(read.signal.aborted).toBe(true);
    store.close();
  });

  test('collapsing stops the follower, and nothing is fetched until it expands', async () => {
    const source = new FakeLogSource();
    const store = open(source);
    source.listed([BUILD]);
    await settle();
    const read = source.lastRead();

    store.send({type: 'collapse'});
    expect(store.getState()).toMatchObject({visible: true, expanded: false});
    expect(read.signal.aborted).toBe(true);
    store.send({type: 'observe', visible: true, stage: 'starting_container'});
    await settle();
    expect(source.lists).toHaveLength(1);
    expect(source.reads).toHaveLength(1);

    store.send({type: 'expand'});
    expect(source.lists).toHaveLength(2);
    store.close();
  });

  test('a host without logs shows unsupported instead of an error, and reads nothing', async () => {
    const source = new FakeLogSource();
    const store = open(source);
    source.lists.at(-1)?.resolve({
      ok: false,
      failure: {code: 'feature_not_negotiated', message: 'no logs'},
    });
    await settle();
    expect(store.getState().listing).toBe('unsupported');
    expect(source.reads).toHaveLength(0);
    store.close();
  });

  test('a failed listing retries on the clock', async () => {
    const source = new FakeLogSource();
    const clock = new FakeClock();
    const store = open(source, clock);
    source.lists.at(-1)?.resolve({ok: false, failure: {code: 'transport', message: 'dropped'}});
    await settle();
    expect(store.getState().listing).toBe('failed');
    expect(source.lists).toHaveLength(1);

    clock.advanceBy(1000);
    expect(source.lists).toHaveLength(2);
    store.close();
  });
});
