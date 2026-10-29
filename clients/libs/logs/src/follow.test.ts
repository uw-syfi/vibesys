import {describe, expect, test} from 'bun:test';
import type {LogChunk} from '@vibesys/contracts/host-helper';
import {checkMachine} from '@vibesys/machine/testing';
import type {AbortSignalLike} from '@vibesys/transport';
import {FakeClock} from '@vibesys/transport/testing';
import {
  DEFAULT_LOG_FOLLOW_CONFIG,
  type LogFollowConfig,
  type LogReadInput,
  type LogReadOutcome,
  type LogTransport,
  logFollowMachine,
  logViewComplete,
  logViewLines,
  startLogFollower,
} from './index.js';

const CONFIG: LogFollowConfig = {
  ...DEFAULT_LOG_FOLLOW_CONFIG,
  tailBytes: 1024,
  maxBytes: 1024,
  waitMs: 20_000,
  retryDelaysMs: [100, 400],
  bounds: {maxLines: 4, maxLineChars: 80},
};
const INPUT = {instanceId: '0123456789ab', streamId: '7', config: CONFIG};

function chunk(offset: number, data: string, {eof = true, closed = false} = {}): LogChunk {
  const length = new TextEncoder().encode(data).length;
  return {
    offset,
    data,
    next_offset: offset + length,
    eof,
    closed,
    truncated_before: false,
    pipes: length === 0 ? [] : [{offset, length, pipe: 'stdout'}],
  };
}

const ok = (value: LogChunk): LogReadOutcome => ({ok: true, chunk: value});

interface PendingRead {
  readonly input: LogReadInput;
  readonly signal: AbortSignalLike;
  settle(outcome: LogReadOutcome): void;
  fail(error: Error): void;
}

/** A transport whose reads the test settles, one by one, in the order it chooses. */
class FakeLogTransport implements LogTransport {
  readonly reads: PendingRead[] = [];

  read(input: LogReadInput, signal: AbortSignalLike): Promise<LogReadOutcome> {
    return new Promise((resolve, reject) => {
      this.reads.push({input, signal, settle: resolve, fail: reject});
    });
  }

  /** The reads that are still waiting for a reply and were not aborted. */
  inFlight(): PendingRead[] {
    return this.reads.filter(read => !read.signal.aborted);
  }
}

async function settled(): Promise<void> {
  for (let turn = 0; turn < 5; turn += 1) await Promise.resolve();
}

async function next(transport: FakeLogTransport, outcome: LogReadOutcome): Promise<void> {
  const reads = transport.inFlight();
  expect(reads.length).toBe(1);
  const [read] = reads;
  if (read === undefined) throw new Error('no read in flight');
  transport.reads.splice(transport.reads.indexOf(read), 1);
  read.settle(outcome);
  await settled();
}

describe('logFollowMachine', () => {
  test('model: at most one read is live, the view stays bounded, and stop releases everything', () => {
    checkMachine(
      {
        machine: logFollowMachine,
        input: INPUT,
        event: (random, live, snapshot) => {
          if (snapshot.status === 'done') return null;
          const offset = snapshot.context.view.nextOffset ?? 0;
          const outcomes: LogReadOutcome[] = [
            ok(chunk(offset, 'line\n'.repeat(random.int(4)), {eof: random.bool()})),
            ok(chunk(offset, 'end\n', {closed: true})),
            {ok: false, failure: {code: 'deadline_exceeded', message: 'late'}},
            {ok: false, failure: {code: 'unknown_log_stream', message: 'gone'}},
          ];
          return random.pick([
            {type: 'open'},
            {type: 'retry'},
            ...(random.int(10) === 0 ? [{type: 'stop'}] : []),
            ...live.operations.flatMap(operation => [
              live.done(operation, random.pick(outcomes)),
              live.error(operation, new Error('channel dropped')),
            ]),
            ...live.delays.map(delay => live.fire(delay)),
          ]);
        },
        invariants: snapshot => {
          const problems: string[] = [];
          if (Object.keys(snapshot.children).length > 1) problems.push('more than one read live');
          if (snapshot.context.view.lines.length > CONFIG.bounds.maxLines)
            problems.push('view exceeds its bound');
          if (snapshot.matches('closed') && !logViewComplete(snapshot.context.view))
            problems.push('closed before the stream completed');
          const reading = snapshot.matches('tailing') || snapshot.matches('following');
          if (reading !== Object.keys(snapshot.children).length > 0)
            problems.push('a read is live exactly while tailing or following');
          return problems;
        },
      },
      60,
    );
  });

  test('reads the tail, then follows from where the view ended with one long-poll in flight', async () => {
    const transport = new FakeLogTransport();
    const clock = new FakeClock();
    const follower = startLogFollower(transport, clock.schedule, INPUT);
    expect(transport.inFlight()[0]?.input).toEqual({
      instance_id: INPUT.instanceId,
      stream_id: INPUT.streamId,
      start: {tail_bytes: CONFIG.tailBytes},
      max_bytes: CONFIG.maxBytes,
      wait_ms: 0,
    });
    await next(transport, ok(chunk(500, 'step 7/8\nbuild')));
    expect(follower.getSnapshot().value).toBe('following');
    expect(transport.inFlight()[0]?.input.start).toEqual({offset: 514});
    expect(transport.inFlight()[0]?.input.wait_ms).toBe(CONFIG.waitMs);
    await next(transport, ok(chunk(514, 'ing\nstep 8/8\n')));
    expect(logViewLines(follower.getSnapshot().context.view)).toEqual([
      'step 7/8',
      'building',
      'step 8/8',
    ]);
    await next(transport, ok(chunk(527, 'done\n', {closed: true})));
    expect(follower.getSnapshot().value).toBe('closed');
    expect(transport.inFlight()).toEqual([]);
  });

  test('stopping, as an unmounting view does, aborts the read in flight and reads no more', async () => {
    const transport = new FakeLogTransport();
    const clock = new FakeClock();
    const follower = startLogFollower(transport, clock.schedule, INPUT);
    await next(transport, ok(chunk(0, 'a\n')));
    const [read] = transport.inFlight();
    follower.stop();
    expect(read?.signal.aborted).toBe(true);
    read?.settle(ok(chunk(2, 'late\n')));
    await settled();
    clock.advanceBy(60_000);
    expect(transport.inFlight()).toEqual([]);
    expect(logViewLines(follower.getSnapshot().context.view)).toEqual(['a']);
  });

  test('a transient failure retries after a growing delay on the injected clock', async () => {
    const transport = new FakeLogTransport();
    const clock = new FakeClock();
    const follower = startLogFollower(transport, clock.schedule, INPUT);
    await next(transport, ok(chunk(0, 'a\n')));
    await next(transport, {ok: false, failure: {code: 'deadline_exceeded', message: 'late'}});
    expect(follower.getSnapshot().matches('error')).toBe(true);
    clock.advanceBy(99);
    expect(transport.inFlight()).toEqual([]);
    clock.advanceBy(1);
    await settled();
    expect(transport.inFlight()[0]?.input.start).toEqual({offset: 2});
    const [read] = transport.inFlight();
    transport.reads.splice(0, transport.reads.length);
    read?.fail(new Error('channel dropped'));
    await settled();
    clock.advanceBy(399);
    expect(transport.inFlight()).toEqual([]);
    clock.advanceBy(1);
    await settled();
    expect(transport.inFlight().length).toBe(1);
    expect(follower.getSnapshot().context.failure?.code).toBe('transport');
  });

  test('a permanent failure waits for retry instead of retrying on its own', async () => {
    const transport = new FakeLogTransport();
    const clock = new FakeClock();
    const follower = startLogFollower(transport, clock.schedule, INPUT);
    await next(transport, {ok: false, failure: {code: 'unknown_log_stream', message: 'gone'}});
    clock.advanceBy(600_000);
    expect(transport.inFlight()).toEqual([]);
    follower.send({type: 'retry'});
    expect(transport.inFlight()[0]?.input.start).toEqual({tail_bytes: CONFIG.tailBytes});
  });
});
