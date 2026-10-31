import {expect, test} from 'bun:test';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {checkMachine} from '@vibesys/machine/testing';
import {
  NoticeCode,
  RecordSchema,
  RunHeadSchema,
  RunLiveness,
  Severity,
  type Record as StoreRecord,
  TailRequestSchema,
  TranscriptChannel,
  TranscriptEntrySchema,
  WatchRunsResponseSchema,
} from '@vibesys/proto';
import {storeRunFixture, withCoreRecords} from '@vibesys/run-fixtures';
import {foldSkeleton, seedSkeleton} from '@vibesys/run-skeleton';
import {FakeSkeletonCache} from '@vibesys/run-skeleton/cache';
import {gatewayReader} from '@vibesys/run-skeleton/reader';
import {FakeRunTransport} from '@vibesys/run-skeleton/testing';
import {FakeClock} from '@vibesys/transport/testing';
import fc from 'fast-check';
import {getInitialSnapshot, getNextSnapshot, type SnapshotFrom} from 'xstate';
import {
  gatewayRunMachine,
  headerView,
  projectSkeleton,
  runHasEnded,
  scrubberView,
  skeletonScreenState,
} from '../../state/index.js';
import {startGatewayRun} from '../index.js';

const SEED = {
  identity: {host: 'local' as const, runId: 'run-a'},
  title: {project: 'queue-rs', task: null},
  hostLabel: 'Local',
  unavailable: {},
};
const SERVICE = '/vibesys.api.v1.HostService/';
function reached(
  actor: ReturnType<typeof startGatewayRun>,
  predicate: (snapshot: SnapshotFrom<typeof gatewayRunMachine>) => boolean,
): Promise<void> {
  if (predicate(actor.getSnapshot())) return Promise.resolve();
  return new Promise(resolve => {
    const subscription = actor.subscribe(snapshot => {
      if (predicate(snapshot)) {
        subscription.unsubscribe();
        resolve();
      }
    });
  });
}

test('model: every content and log operation belongs to its active state', () => {
  const fixture = storeRunFixture();
  const skeleton = foldSkeleton(seedSkeleton(), fixture.records);
  checkMachine({
    machine: gatewayRunMachine,
    input: {seed: SEED},
    event: (random, live) =>
      random.pick([
        {
          type: 'loaded',
          load: {
            skeleton,
            committedSeq: skeleton.throughSeq ?? 0n,
            nextSeq: (skeleton.throughSeq ?? 0n) + 1n,
            complete: true,
          },
          progress: {phase: 'complete'},
          failure: undefined,
          terminal: false,
        },
        {type: 'ui', action: {type: 'select-stream', streamId: 'build-log'}},
        {
          type: 'follow-transcript',
          selection: random.pick([null, {spanId: 'turn-0', streamId: 'native-stream'}]),
        },
        ...live.operations.flatMap(id => [
          live.error(id, new Error('Fault')),
          ...(id === 'content' ? [live.done(id, {hash: 'hash', text: 'Content'})] : []),
        ]),
        ...live.delays.map(id => live.fire(id)),
      ]),
  });
});

test('Skeleton then Tail, local seek, verified cached content, logs and cancellation', async () => {
  const fixture = storeRunFixture('blobs');
  const transport = new FakeRunTransport({...fixture, initialCount: 3});
  const cache = new FakeSkeletonCache();
  const clock = new FakeClock();
  const ports = {
    reader: gatewayReader(transport, cache, clock.schedule),
  };
  const actor = startGatewayRun(ports, clock.schedule, {seed: SEED});
  await reached(
    actor,
    snapshot =>
      snapshot.context.load?.complete === true && Object.keys(snapshot.context.text).length === 2,
  );
  const tail = transport.calls.find(
    call =>
      call.method === `${SERVICE}Tail` &&
      fromBinary(TailRequestSchema, call.request).fromSeq === 4n,
  );
  if (!tail) throw new Error('Tail required');
  expect(fromBinary(TailRequestSchema, tail.request).fromSeq).toBe(4n);
  expect(transport.calls.filter(call => call.method === `${SERVICE}Skeleton`)).toHaveLength(1);
  expect(transport.calls.filter(call => call.method === `${SERVICE}GetBlobs`)).toHaveLength(2);
  expect(Object.values(actor.getSnapshot().context.text).sort()).toEqual([
    'Steady throughput',
    'Throughput',
  ]);
  const calls = transport.calls.length;
  actor.send({type: 'ui', action: {type: 'view', view: {at: 3}}});
  expect(actor.getSnapshot().context.ui.view.at).toBe(3);
  expect(transport.calls).toHaveLength(calls);
  actor.send({type: 'ui', action: {type: 'select-stream', streamId: 'build-log'}});
  await reached(actor, snapshot => snapshot.context.ui.views['build-log']?.lines.length === 1);
  expect(transport.activeStreams).toBe(3);
  actor.stop();
  await Promise.resolve();
  expect(
    transport.calls.every(
      call =>
        call.signal.aborted || call.method.endsWith('Skeleton') || call.method.endsWith('GetBlobs'),
    ),
  ).toBe(true);
  actor.stop();
  const reopened = startGatewayRun({...ports}, clock.schedule, {
    seed: SEED,
  });
  await reached(reopened, snapshot => Object.keys(snapshot.context.text).length === 2);
  expect(transport.calls.filter(call => call.method.endsWith('GetBlobs'))).toHaveLength(2);
  reopened.stop();
});

test('live records append while scrubbed and switching streams cancels the prior follower', async () => {
  const fixture = storeRunFixture('live');
  const transport = new FakeRunTransport(fixture);
  const clock = new FakeClock();
  const actor = startGatewayRun(
    {
      reader: gatewayReader(transport, new FakeSkeletonCache(), clock.schedule),
    },
    clock.schedule,
    {seed: SEED},
  );
  await reached(actor, snapshot => snapshot.context.load?.complete === true);
  actor.send({type: 'ui', action: {type: 'view', view: {at: 3}}});
  const seq = (fixture.records.at(-1)?.seq ?? 0n) + 1n;
  transport.publish([
    create(RecordSchema, {
      seq,
      body: {case: 'lifecycle', value: {state: {case: 'parked', value: {}}}},
    }),
  ]);
  await reached(actor, snapshot => snapshot.context.load?.skeleton.throughSeq === seq);
  expect(actor.getSnapshot().context.ui.view.at).toBe(3);
  actor.send({type: 'ui', action: {type: 'select-stream', streamId: 'build-log'}});
  await reached(actor, snapshot => snapshot.context.ui.views['build-log'] !== undefined);
  actor.send({type: 'ui', action: {type: 'select-stream', streamId: 'missing'}});
  await reached(actor, snapshot => snapshot.context.ui.logProblem?.message === 'Log unavailable');
  actor.stop();
});

test('native record history comes from the loader without a second reader of the log', async () => {
  const fixture = storeRunFixture('live');
  const transport = new FakeRunTransport(fixture);
  const clock = new FakeClock();
  const actor = startGatewayRun(
    {
      reader: gatewayReader(transport, new FakeSkeletonCache(), clock.schedule),
    },
    clock.schedule,
    {seed: SEED},
  );
  try {
    await reached(
      actor,
      snapshot => snapshot.context.ui.recordLog?.length === fixture.records.length,
    );
    const seq = (fixture.records.at(-1)?.seq ?? 0n) + 1n;
    transport.publish([
      create(RecordSchema, {
        seq,
        epoch: 1,
        body: {
          case: 'notice',
          value: {
            severity: Severity.ERROR,
            code: NoticeCode.INPUT_MEASUREMENT_FAILED,
            message: 'Measurement unavailable',
          },
        },
      }),
    ]);
    await reached(actor, snapshot => snapshot.context.ui.recordLog?.at(-1)?.seq === String(seq));
    const {load, ui} = actor.getSnapshot().context;
    if (!load) throw new Error('load required');
    const model = projectSkeleton(load.skeleton, load.skeleton.throughSeq ?? 0n);
    expect(skeletonScreenState(SEED, load.skeleton, model, ui).recordDiagnostic?.message).toBe(
      'Measurement unavailable',
    );
    expect(actor.getSnapshot().context.ui.recordLog).toHaveLength(fixture.records.length + 1);
    // One reader: the records came from the loader's Skeleton and Tail, never a second Tail.
    const tails = transport.calls.filter(call => call.method === `${SERVICE}Tail`);
    expect(tails).toHaveLength(1);
    expect(
      fromBinary(TailRequestSchema, tails[0]?.request ?? new Uint8Array()).fromSeq,
    ).toBeGreaterThan(0n);
  } finally {
    actor.stop();
  }
});

/** The run history the screen lists for a log, once its reader has delivered the whole of it. */
async function historyOf(
  records: readonly StoreRecord[],
): Promise<readonly {body: string; message: string}[]> {
  const transport = new FakeRunTransport({records});
  const clock = new FakeClock();
  const actor = startGatewayRun(
    {reader: gatewayReader(transport, new FakeSkeletonCache(), clock.schedule)},
    clock.schedule,
    {seed: SEED},
  );
  try {
    await reached(actor, snapshot => snapshot.context.load?.complete === true);
    await reached(actor, snapshot => snapshot.context.ui.recordLog !== undefined);
    // The skeleton folds core records without a fact of their own, through the last of them.
    expect(actor.getSnapshot().context.load?.skeleton.throughSeq).toBe(records.at(-1)?.seq);
    return (actor.getSnapshot().context.ui.recordLog ?? []).map(({body, message}) => ({
      body,
      message,
    }));
  } finally {
    actor.stop();
  }
}

test('regression: core records written by the run are no rows of the run history', async () => {
  const ordinary = storeRunFixture('live').records;
  const mixed = withCoreRecords(ordinary, [2, 0, 1, 3]);
  expect(mixed.length).toBeGreaterThan(ordinary.length);
  const history = await historyOf(mixed);
  expect(history).toEqual(await historyOf(ordinary));
  expect(history).toHaveLength(ordinary.length);
});

test('property: any placement of core records among the ordinary ones leaves the same history', async () => {
  const ordinary = storeRunFixture('live', 3).records;
  const expected = await historyOf(ordinary);
  await fc.assert(
    fc.asyncProperty(fc.array(fc.nat(3), {maxLength: ordinary.length + 1}), async core => {
      expect(await historyOf(withCoreRecords(ordinary, core))).toEqual(expected);
    }),
    {numRuns: 20},
  );
});

test('failed unary and corrupt content surface errors without using the legacy path', async () => {
  const fixture = storeRunFixture();
  for (const operation of ['Skeleton', 'GetBlobs']) {
    const clock = new FakeClock();
    const transport = new FakeRunTransport(fixture);
    transport.failures.set(`${SERVICE}${operation}`, new Error('Fault'));
    const actor = startGatewayRun(
      {
        reader: gatewayReader(transport, new FakeSkeletonCache(), clock.schedule),
      },
      new FakeClock().schedule,
      {seed: SEED},
    );
    await reached(
      actor,
      snapshot => snapshot.context.error !== null || snapshot.context.failure !== undefined,
    );
    expect(actor.getSnapshot().context.error).toBe(
      operation === 'Skeleton' ? null : 'Content unavailable',
    );
    if (operation === 'Skeleton')
      expect(actor.getSnapshot().context.failure?.kind).toBe('transport');
    actor.stop();
  }
});

test('content and first-log deadlines use the injected clock', async () => {
  const fixture = storeRunFixture();
  const clock = new FakeClock();
  const transport = new FakeRunTransport(fixture);
  const reader = gatewayReader(transport, new FakeSkeletonCache(), clock.schedule);
  transport.held.add(`${SERVICE}GetBlobs`);
  transport.held.add(`${SERVICE}TailStream`);
  const actor = startGatewayRun({reader}, clock.schedule, {seed: SEED});
  await reached(actor, snapshot => snapshot.matches({active: {content: 'reading'}}));
  actor.send({type: 'ui', action: {type: 'select-stream', streamId: 'build-log'}});
  await transport.called(`${SERVICE}GetBlobs`);
  await transport.called(`${SERVICE}TailStream`);
  clock.advanceBy(30000);
  expect(actor.getSnapshot().context.error).toBe('Content timeout');
  expect(actor.getSnapshot().context.ui.logProblem).toBeUndefined();
  expect(transport.calls.find(call => call.method.endsWith('GetBlobs'))?.signal.aborted).toBe(true);
  expect(transport.calls.find(call => call.method.endsWith('TailStream'))?.signal.aborted).toBe(
    false,
  );
  actor.stop();
});

test('each objective hash is attempted once even when content fails', async () => {
  const fixture = storeRunFixture('blobs');
  const clock = new FakeClock();
  const transport = new FakeRunTransport(fixture);
  transport.failures.set(`${SERVICE}GetBlobs`, new Error('Fault'));
  const actor = startGatewayRun(
    {
      reader: gatewayReader(transport, new FakeSkeletonCache(), clock.schedule),
    },
    clock.schedule,
    {seed: SEED},
  );
  await reached(
    actor,
    snapshot =>
      transport.calls.filter(call => call.method.endsWith('GetBlobs')).length === 2 &&
      snapshot.matches({active: {content: 'idle'}}),
  );
  expect(transport.calls.filter(call => call.method.endsWith('GetBlobs'))).toHaveLength(2);
  expect(actor.getSnapshot().context.error).toBe('Content unavailable');
  actor.stop();
});

test.each([
  'running',
  'paused',
] as const)('lock loss interrupts a %s run without changing history', async phase => {
  const fixture = storeRunFixture('live');
  const seq = (fixture.records.at(-1)?.seq ?? 0n) + 1n;
  const transport = new FakeRunTransport({
    ...fixture,
    records: [
      ...fixture.records,
      create(RecordSchema, {
        seq,
        epoch: 1,
        body: {
          case: 'lifecycle',
          value: {
            state: {
              case: 'running',
              value: phase === 'paused' ? {hold: {reason: {case: 'operator', value: {}}}} : {},
            },
          },
        },
      }),
    ],
  });
  const clock = new FakeClock();
  const actor = startGatewayRun(
    {
      reader: gatewayReader(transport, new FakeSkeletonCache(), clock.schedule),
    },
    clock.schedule,
    {seed: SEED},
  );
  await reached(actor, snapshot => snapshot.context.load?.complete === true);
  await transport.called(`${SERVICE}WatchRuns`);
  transport.observe(
    create(WatchRunsResponseSchema, {
      runId: 'other',
      liveness: RunLiveness.INTERRUPTED,
      head: {epoch: 1, process: {epoch: 1}},
    }),
  );
  transport.observe(
    create(WatchRunsResponseSchema, {
      runId: 'run-a',
      liveness: RunLiveness.ALIVE,
      head: {epoch: 1, process: {epoch: 1}},
    }),
  );
  await reached(actor, snapshot => snapshot.context.hint?.value === RunLiveness.ALIVE);
  transport.observe(
    create(WatchRunsResponseSchema, {
      runId: 'run-a',
      liveness: RunLiveness.INTERRUPTED,
      head: {epoch: 1, process: {epoch: 1}},
    }),
  );
  await reached(actor, snapshot => snapshot.context.hint?.value === RunLiveness.INTERRUPTED);
  const {load, hint, ui} = actor.getSnapshot().context;
  if (!load) throw new Error('loaded run required');
  const whole = projectSkeleton(load.skeleton, seq);
  const screen = skeletonScreenState(SEED, load.skeleton, whole, ui, hint);
  expect(headerView(screen).status.label).toBe('Interrupted');
  expect(runHasEnded(screen)).toBe(true);
  expect(scrubberView(screen.run.trace, screen.view, runHasEnded(screen))?.latestLabel).toBe('End');
  expect(load.skeleton.lifecycles.at(-1)?.lifecycle?.state.case).toBe('running');
  expect(headerView(skeletonScreenState(SEED, load.skeleton, whole, ui)).status.label).toBe(
    phase === 'paused' ? 'Paused by you' : 'Running',
  );
  expect(actor.getSnapshot().context.error).toBeNull();
  actor.stop();
  await Promise.resolve();
  expect(
    transport.calls
      .filter(call => call.method.endsWith('WatchRuns'))
      .every(call => call.signal.aborted),
  ).toBe(true);
});

test.each([
  'short',
  'live',
] as const)('Tail loss on %s distinguishes final history from a live connection', async kind => {
  for (const failure of ['closed', 'error'] as const) {
    const fixture = storeRunFixture(kind);
    const transport = new FakeRunTransport(fixture);
    const clock = new FakeClock();
    const actor = startGatewayRun(
      {
        reader: gatewayReader(transport, new FakeSkeletonCache(), clock.schedule),
      },
      clock.schedule,
      {seed: SEED},
    );
    await reached(actor, snapshot => snapshot.context.load?.complete === true);
    await reached(
      actor,
      snapshot =>
        snapshot.matches({active: {content: 'idle'}}) &&
        Object.keys(snapshot.context.text).length > 0,
    );
    expect(clock.pending).toBe(1);
    const through = actor.getSnapshot().context.load?.skeleton.throughSeq;
    if (failure === 'closed') transport.close(`${SERVICE}Tail`);
    else transport.fail(`${SERVICE}Tail`, new Error('connection lost'));
    // The retry timer is armed only after the stream's outcome has been consumed.
    // The liveness lease holds one timer; the retry is the second.
    await reached(actor, () => (kind === 'live' ? clock.pending > 1 : true));
    expect(actor.getSnapshot().context.failure === undefined).toBe(kind === 'short');
    expect(actor.getSnapshot().context.load?.skeleton.throughSeq).toBe(through);
    if (kind === 'live') {
      transport.recover(`${SERVICE}Tail`);
      clock.advanceBy(1000);
      await reached(actor, snapshot => snapshot.context.failure === undefined);
    }
    actor.stop();
    expect(clock.pending).toBe(0);
  }
});

test.each([
  'Skeleton',
  'Tail',
] as const)('a run the host does not hold (%s NotFound) is final, not Disconnected', async method => {
  const fixture = storeRunFixture('live');
  const transport = new FakeRunTransport(fixture);
  const clock = new FakeClock();
  const missing = Object.assign(new Error('lstat runs/run-a: no such file or directory'), {
    status: 5,
  });
  if (method === 'Skeleton') transport.fail(`${SERVICE}Skeleton`, missing);
  const actor = startGatewayRun(
    {
      reader: gatewayReader(transport, new FakeSkeletonCache(), clock.schedule),
    },
    clock.schedule,
    {seed: SEED},
  );
  if (method === 'Tail') {
    await reached(actor, snapshot => snapshot.context.load !== undefined);
    transport.fail(`${SERVICE}Tail`, missing);
  }
  const final = (snapshot: ReturnType<typeof actor.getSnapshot>): boolean =>
    snapshot.context.terminal && snapshot.context.failure?.kind === 'refused';
  await reached(actor, final);
  const reads = () => transport.calls.filter(call => /\/(Skeleton|Tail)$/.test(call.method)).length;
  const before = reads();
  // WatchRuns keeps reporting the link as healthy; the reader's answer stays final.
  for (let tick = 0; tick < 5; tick += 1) {
    clock.advanceBy(1000);
    await Promise.resolve();
  }
  expect(final(actor.getSnapshot())).toBe(true);
  expect(reads()).toBe(before);
  actor.stop();
  expect(clock.pending).toBe(0);
});

test.each([
  'deadline',
  'closed',
  'error',
] as const)('liveness %s recovers independently and cancels with the reader', async failure => {
  const fixture = storeRunFixture('live');
  const clock = new FakeClock();
  const transport = new FakeRunTransport(fixture);
  if (failure === 'deadline') transport.held.add(`${SERVICE}WatchRuns`);
  const actor = startGatewayRun(
    {
      reader: gatewayReader(transport, new FakeSkeletonCache(), clock.schedule),
    },
    clock.schedule,
    {seed: SEED},
  );
  await reached(
    actor,
    snapshot =>
      snapshot.context.load?.complete === true && Object.keys(snapshot.context.text).length > 0,
  );
  await transport.called(`${SERVICE}WatchRuns`);
  if (failure === 'deadline') clock.advanceBy(30000);
  else if (failure === 'closed') transport.close(`${SERVICE}WatchRuns`);
  else transport.fail(`${SERVICE}WatchRuns`, new Error('offline'));
  await reached(actor, snapshot => snapshot.context.failure !== undefined);
  transport.release(`${SERVICE}WatchRuns`);
  transport.recover(`${SERVICE}WatchRuns`);
  clock.advanceBy(1000);
  await reached(actor, snapshot => snapshot.context.failure === undefined);
  transport.observe(
    create(WatchRunsResponseSchema, {
      runId: 'run-a',
      liveness: RunLiveness.INTERRUPTED,
      head: {epoch: 1, process: {epoch: 1}},
    }),
  );
  await reached(actor, snapshot => snapshot.context.hint?.value === RunLiveness.INTERRUPTED);
  transport.observe(create(WatchRunsResponseSchema, {runId: 'run-a', removed: true}));
  await reached(actor, snapshot => snapshot.context.hint === undefined);
  actor.stop();
  expect(clock.pending).toBe(0);
  expect(transport.calls.filter(call => call.method.endsWith('WatchRuns'))).toHaveLength(2);
});

test('reply attachments load through the same verified content owner as objectives', async () => {
  const fixture = storeRunFixture('short', 1);
  const skeleton = foldSkeleton(seedSkeleton(), fixture.records);
  const span = Object.values(skeleton.spans)
    .find(history => history.versions.at(-1)?.span?.label?.kind === 'agent_turn')
    ?.versions.at(-1)?.span;
  if (!span) throw new Error('fixture');
  const reply = {kind: 'Result', json_schema: {type: 'object'}, value: {answer: 'x'.repeat(65537)}};
  const text = JSON.stringify(reply);
  const data = new TextEncoder().encode(text);
  const hash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', data)))
    .map(value => value.toString(16).padStart(2, '0'))
    .join('');
  const seq = (skeleton.throughSeq ?? 0n) + 1n;
  const transport = new FakeRunTransport({
    ...fixture,
    records: [
      ...fixture.records,
      create(RecordSchema, {
        seq,
        epoch: 1,
        body: {
          case: 'blob',
          value: {
            span: span.id,
            role: 'reply',
            blob: {sha256: hash, size: BigInt(data.length), mediaType: 'application/json'},
          },
        },
      }),
    ],
    blobs: {...fixture.blobs, [hash]: data},
  });
  const clock = new FakeClock();
  const actor = startGatewayRun(
    {
      reader: gatewayReader(transport, new FakeSkeletonCache(), clock.schedule),
    },
    clock.schedule,
    {seed: SEED},
  );
  try {
    await reached(actor, snapshot => snapshot.context.text[hash] !== undefined);
    const {load, ui, replies, text: content} = actor.getSnapshot().context;
    if (!load) throw new Error('loaded history required');
    const model = projectSkeleton(load.skeleton, seq, '', {content: {text: content, replies}});
    const state = skeletonScreenState(SEED, load.skeleton, model, ui, undefined, {
      text: content,
      replies,
    });
    expect(state.run.core?.transcript.find(row => row.spanId === span.id)?.reply).toEqual(reply);
    expect(replies[span.id]).toEqual([{seq, hash}]);
  } finally {
    actor.stop();
  }
  expect(
    transport.calls
      .filter(call => !call.method.endsWith('Skeleton') && !call.method.endsWith('GetBlobs'))
      .every(call => call.signal.aborted),
  ).toBe(true);
});

async function expireAndRestoreWatch(
  actor: ReturnType<typeof startGatewayRun>,
  transport: FakeRunTransport,
  clock: FakeClock,
  idle: number,
) {
  transport.held.add(`${SERVICE}WatchRuns`);
  clock.advanceBy(idle);
  expect(actor.getSnapshot().context.failure?.kind).toBe('transport');
  transport.release(`${SERVICE}WatchRuns`);
  // The retries back off while the channel is down, and a stream needs a few turns to deliver:
  // step through the waits a second at a time, letting the streams run between steps.
  const restored = () => {
    const {context} = actor.getSnapshot();
    return context.failure === undefined && context.hint?.epoch === 1;
  };
  for (let step = 0; step < 120 && !restored(); step += 1) {
    clock.advanceBy(1000);
    for (let turn = 0; turn < 20; turn += 1) await Promise.resolve();
  }
  await reached(
    actor,
    snapshot => snapshot.context.failure === undefined && snapshot.context.hint?.epoch === 1,
  );
}

test('property: quiet logs and peer failures stay scoped to their stream for any idle interval', async () => {
  const fixture = storeRunFixture('live');
  await fc.assert(
    fc.asyncProperty(
      fc.integer({min: 30000, max: 3600000}),
      fc.constantFrom('Not found', 'Unsupported by an older host', 'Connection closed'),
      async (idle, error) => {
        const transport = new FakeRunTransport({
          ...fixture,
          streams: {...fixture.streams, quiet: new Uint8Array()},
        });
        const clock = new FakeClock();
        const actor = startGatewayRun(
          {
            reader: gatewayReader(transport, new FakeSkeletonCache(), clock.schedule),
          },
          clock.schedule,
          {seed: SEED},
        );
        try {
          await reached(
            actor,
            snapshot =>
              snapshot.context.load?.complete === true &&
              Object.keys(snapshot.context.text).length > 0,
          );
          transport.observe(
            create(WatchRunsResponseSchema, {
              runId: SEED.identity.runId,
              liveness: RunLiveness.ALIVE,
              head: {epoch: 1},
            }),
          );
          await reached(actor, snapshot => snapshot.context.hint?.epoch === 1);
          actor.send({type: 'ui', action: {type: 'select-stream', streamId: 'quiet'}});
          await transport.called(`${SERVICE}TailStream`);
          await expireAndRestoreWatch(actor, transport, clock, idle);
          expect(actor.getSnapshot().context.error).toBeNull();
          expect(actor.getSnapshot().context.ui.logProblem).toBeUndefined();
          expect(actor.getSnapshot().matches({active: {logs: 'following'}})).toBe(true);
          transport.fail(`${SERVICE}TailStream`, new Error(error));
          await reached(actor, snapshot => snapshot.context.ui.logProblem !== undefined);
          expect(actor.getSnapshot().context.error).toBeNull();
          transport.recover(`${SERVICE}TailStream`);
          actor.send({type: 'ui', action: {type: 'select-stream', streamId: 'quiet'}});
          expect(actor.getSnapshot().context.ui.logProblem).toBeUndefined();
          await expireAndRestoreWatch(actor, transport, clock, idle);
          expect(actor.getSnapshot().context.error).toBeNull();
          actor.send({type: 'ui', action: {type: 'select-stream', streamId: 'build-log'}});
          await reached(actor, snapshot => snapshot.context.ui.views['build-log'] !== undefined);
          expect(actor.getSnapshot().context.ui.logProblem).toBeUndefined();
        } finally {
          actor.stop();
        }
      },
    ),
    {numRuns: 16, seed: 253},
  );
});

test('regression: native turn content crosses the real reader for every stream split, and leaving releases it', async () => {
  const payload = toBinary(
    TranscriptEntrySchema,
    create(TranscriptEntrySchema, {
      entry: {
        case: 'text',
        value: {
          channel: TranscriptChannel.ANALYSIS,
          content: {value: {case: 'inline', value: 'Thinking 🦊 before acting'}},
        },
      },
    }),
  );
  expect(payload.length).toBeLessThan(128);
  const wire = new Uint8Array([payload.length, ...payload]);
  for (const streamChunkBytes of [1, 2, 5, 13, 127]) {
    const fixture = storeRunFixture();
    const transport = new FakeRunTransport({
      ...fixture,
      streams: {...fixture.streams, 'native-stream': wire},
      streamChunkBytes,
    });
    const clock = new FakeClock();
    const actor = startGatewayRun(
      {reader: gatewayReader(transport, new FakeSkeletonCache(), clock.schedule)},
      clock.schedule,
      {seed: SEED},
    );
    actor.send({
      type: 'follow-transcript',
      selection: {spanId: 'turn-0', streamId: 'native-stream'},
    });
    expect(actor.getSnapshot().context.selectedTranscript).toEqual({
      spanId: 'turn-0',
      streamId: 'native-stream',
    });
    await reached(
      actor,
      snapshot => (snapshot.context.transcripts['turn-0']?.entries.length ?? 0) === 1,
    );
    const context = actor.getSnapshot().context;
    const skeleton = foldSkeleton(seedSkeleton(), fixture.records);
    const model = projectSkeleton(skeleton, skeleton.throughSeq ?? 0n, '', {
      content: {transcripts: context.transcripts},
    });
    const screen = skeletonScreenState(SEED, skeleton, model, context.ui, undefined, {
      transcripts: context.transcripts,
    });
    expect(screen.run.core?.transcript).toContainEqual(
      expect.objectContaining({
        spanId: 'turn-0',
        kind: 'analysis',
        content: 'Thinking 🦊 before acting',
      }),
    );
    for (const record of fixture.records.filter(
      record => record.seq < (skeleton.throughSeq ?? 0n),
    )) {
      const earlier = skeletonScreenState(
        SEED,
        skeleton,
        model,
        {...context.ui, view: {...context.ui.view, at: Number(record.seq)}},
        undefined,
        {transcripts: context.transcripts},
      );
      expect(earlier.run.core?.usageRecords).toEqual(screen.run.core?.usageRecords);
      expect(headerView(earlier).agents).toEqual(headerView(screen).agents);
      expect(
        earlier.run.core?.transcript.some(entry => entry.content.includes('Thinking 🦊')),
      ).toBe(false);
    }
    const stream = transport.calls.find(call => call.method === `${SERVICE}TailStream`);
    expect(stream?.signal.aborted).toBe(false);
    actor.send({type: 'follow-transcript', selection: null});
    expect(stream?.signal.aborted).toBe(true);
    actor.stop();
    expect(clock.pending).toBe(0);
  }
});

test('an incomplete native transcript releases its reader on close or the first-entry deadline', async () => {
  for (const end of ['close', 'deadline']) {
    const fixture = storeRunFixture();
    const transport = new FakeRunTransport({
      ...fixture,
      streams: {'native-stream': new Uint8Array([128])},
    });
    const clock = new FakeClock();
    const actor = startGatewayRun(
      {reader: gatewayReader(transport, new FakeSkeletonCache(), clock.schedule)},
      clock.schedule,
      {seed: SEED},
    );
    actor.send({
      type: 'follow-transcript',
      selection: {spanId: 'turn-0', streamId: 'native-stream'},
    });
    await reached(
      actor,
      snapshot => (snapshot.context.transcripts['turn-0']?.pending.length ?? 0) > 0,
    );
    await reached(
      actor,
      snapshot =>
        snapshot.context.load?.complete === true &&
        Object.keys(snapshot.context.text).length === Object.keys(fixture.blobs).length,
    );
    if (end === 'close') transport.close(`${SERVICE}TailStream`);
    else {
      clock.advanceBy(30000);
      expect(actor.getSnapshot().context.error).toBe('Transcript timeout');
    }
    await reached(
      actor,
      snapshot =>
        snapshot.context.error ===
        (end === 'close' ? 'Transcript unavailable' : 'Transcript timeout'),
    );
    actor.stop();
    await transport.whenIdle();
    expect(transport.activeStreams).toBe(0);
    expect(clock.pending).toBe(0);
  }
});

test('property: writer refreshes never replace a displayed epoch with an older committed prefix', () => {
  fc.assert(
    fc.property(fc.array(fc.nat(40), {minLength: 1, maxLength: 40}), arrivals => {
      const fixture = storeRunFixture('live');
      const records = [...fixture.records];
      const first = records[0];
      if (first?.body.case !== 'epochStarted') throw new Error('Epoch required');
      for (let epoch = 2; epoch <= 4; epoch += 1) {
        records.push(
          create(RecordSchema, {
            ...first,
            seq: BigInt(records.length + 1),
            epoch,
            body: {case: 'epochStarted', value: {...first.body.value, epoch}},
          }),
        );
        for (const state of ['starting', 'running'] as const)
          records.push(
            create(RecordSchema, {
              seq: BigInt(records.length + 1),
              epoch,
              body: {case: 'lifecycle', value: {state: {case: state, value: {}}}},
            }),
          );
      }
      let snapshot = getInitialSnapshot(gatewayRunMachine, {seed: SEED});
      let newest = 0;
      for (const arrival of arrivals) {
        const length = arrival % (records.length + 1);
        const skeleton = foldSkeleton(seedSkeleton(), records.slice(0, length));
        newest = Math.max(newest, length);
        snapshot = getNextSnapshot(gatewayRunMachine, snapshot, {
          type: 'loaded',
          load:
            length === 0
              ? undefined
              : {
                  skeleton,
                  nextSeq: BigInt(length + 1),
                  committedSeq: BigInt(records.length),
                  complete: length === records.length,
                  windowFrom: undefined,
                  failuresBefore: undefined,
                },
          progress: {phase: 'history', through: BigInt(length), committed: BigInt(records.length)},
          failure: undefined,
          terminal: false,
          fresh: false,
        });
        expect(snapshot.context.load?.skeleton.throughSeq ?? 0n).toBe(BigInt(newest));
      }
    }),
    {numRuns: 60, seed: 341805, examples: [[[40, 0, 1, 2, 3]]]},
  );
});

test('property: expiring or replayed lock hints cannot roll back committed writer facts', () => {
  fc.assert(
    fc.property(fc.integer({min: 1, max: 8}), fc.nat(100), (epoch, seq) => {
      const head = create(RunHeadSchema, {
        epoch,
        committedSeq: BigInt(seq + 2),
        lifecycle: {state: {case: 'running', value: {}}},
      });
      let snapshot = getInitialSnapshot(gatewayRunMachine, {seed: SEED});
      const loaded = {
        type: 'loaded' as const,
        load: undefined,
        progress: {phase: 'skeleton' as const},
        failure: undefined,
        terminal: false,
        fresh: true,
      };
      snapshot = getNextSnapshot(gatewayRunMachine, snapshot, {
        ...loaded,
        hint: {epoch, value: RunLiveness.ALIVE, head},
      });
      for (const hint of [
        undefined,
        {
          epoch,
          value: RunLiveness.ALIVE,
          head: create(RunHeadSchema, {
            epoch,
            committedSeq: BigInt(seq + 1),
            lifecycle: {state: {case: 'starting', value: {}}},
          }),
        },
        {epoch: epoch - 1, value: RunLiveness.INTERRUPTED},
      ]) {
        snapshot = getNextSnapshot(gatewayRunMachine, snapshot, {
          ...loaded,
          ...(hint ? {hint} : {}),
        });
        expect(snapshot.context.head).toEqual(head);
      }
    }),
    {numRuns: 40, seed: 341808, examples: [[1, 0]]},
  );
});
