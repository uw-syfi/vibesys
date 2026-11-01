/**
 * How a run opens over a narrow link, as properties of generated runs. The real reader, loader and
 * page machine run against a gateway Fake with a round trip and a throughput in virtual time.
 *
 * Input space: native runs of 1 to 160 ideas, finished or live; writer checkpoints that are absent,
 * stale, current, or too big for the reply; reply budgets from 112 KiB to the gateway's 1 MiB;
 * a run with no run.log (NotFound); links from 20 ms and 700 B/ms to 200 ms and 2800 B/ms; a Tail
 * cut mid-way (clean end or error); a link that is down or refuses every call.
 */
import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {gunzipSync} from 'node:zlib';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {
  KeyframeSchema,
  NoticeCode,
  RecordSchema,
  Severity,
  SkeletonResponseSchema,
  TailRequestSchema,
} from '@vibesys/proto';
import {storeRunFixture, withCoreRecords} from '@vibesys/run-fixtures';
import {BACKFILL_PAGES, FAILURE_PAGES, MAX_ATTEMPTS, RECORD_WINDOW} from '@vibesys/run-skeleton';
import {FakeSkeletonCache} from '@vibesys/run-skeleton/cache';
import {gatewayReader} from '@vibesys/run-skeleton/reader';
import {
  FakeRunHost,
  GATEWAY_REPLY_BYTES,
  type LinkModel,
  type SimFault,
  SimGateway,
  type SimRun,
} from '@vibesys/run-skeleton/testing';
import {isCoreBody} from '@vibesys/run-state/records';
import {Seeded} from '@vibesys/store/testing';
import {FakeClock} from '@vibesys/transport/testing';
import {
  loadView,
  paintedSkeleton,
  projectSkeleton,
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
const MINUTE = 60_000;
const CASES = 20;

interface Case {
  readonly run: SimRun;
  readonly link: LinkModel;
  readonly ideas: number;
}

const generated = new Map<number, Case>();

function generate(seed: number): Case {
  const cached = generated.get(seed);
  if (cached) return cached;
  const made = build(seed);
  generated.set(seed, made);
  return made;
}

function build(seed: number): Case {
  const random = new Seeded(seed);
  const ideas = 1 + random.int(160);
  const records = storeRunFixture(random.pick(['short', 'live'] as const), ideas, seed).records;
  const keyframes = random.pick([
    [],
    [Math.ceil(records.length / 3)],
    [records.length],
    Array.from({length: Math.floor(records.length / 12)}, (_, index) => (index + 1) * 12),
  ]);
  const run: SimRun = {
    records,
    keyframes,
    replyBytes: random.pick([112 * 1024, 160 * 1024, 256 * 1024, GATEWAY_REPLY_BYTES]),
  };
  const link = {rttMs: 20 + random.int(180), bytesPerMs: 700 * (1 + random.int(4))};
  return {run, link, ideas};
}

async function settle(): Promise<void> {
  for (let tick = 0; tick < 200; tick += 1) await Promise.resolve();
}

/** Runs virtual time forward event by event until `done`; returns when it held, or undefined. */
async function until(
  clock: FakeClock,
  done: () => boolean,
  limitMs = 10 * MINUTE,
): Promise<number | undefined> {
  await settle();
  while (!done()) {
    if (clock.now() > limitMs || !clock.runNext()) return undefined;
    await settle();
  }
  return clock.now();
}

function open(
  run: SimRun,
  link: LinkModel,
  fault?: (method: string, ordinal: number) => SimFault | undefined,
) {
  const clock = new FakeClock();
  const gateway = new SimGateway(run, clock, link, fault);
  const actor = startGatewayRun(
    {
      reader: gatewayReader(
        gateway,
        new FakeSkeletonCache(),
        clock.schedule,
        new FakeRunHost().feed,
      ),
    },
    clock.schedule,
    {seed: SEED},
  );
  return {clock, gateway, actor};
}

const painted = (actor: ReturnType<typeof open>['actor']): boolean =>
  paintedSkeleton(actor.getSnapshot().context.load) !== undefined;

test('valid prefix paints after one round trip while history catches up', async () => {
  for (let seed = 1; seed <= CASES; seed += 1) {
    const {run, link} = generate(seed);
    const {clock, gateway, actor} = open(run, link);
    const at = await until(clock, () => actor.getSnapshot().context.progress.phase !== 'skeleton');
    const first = gateway.deliveries.find(item => item.method === `${SERVICE}Skeleton`);
    expect(at).toBeDefined();
    if (at === undefined || first === undefined) throw new Error(`seed ${seed}: no paint`);
    expect(at).toBeLessThanOrEqual(link.rttMs + first.bytes / link.bytesPerMs + 1);
    expect(painted(actor)).toBe(true);
    // Only the Skeleton reply (and the liveness hello) crossed the link before progress.
    expect(gateway.bytesBy(at)).toBeLessThanOrEqual(gateway.replyBytes + 256);
    actor.stop();
  }
});

test('the history converges on the full replay with one Tail that never re-reads', async () => {
  for (let seed = 1; seed <= CASES; seed += 1) {
    const {run, link} = generate(seed);
    const {clock, gateway, actor} = open(run, link);
    const last = run.records.at(-1)?.seq ?? 0n;
    await until(
      clock,
      () =>
        actor.getSnapshot().context.load?.skeleton.throughSeq === last &&
        (actor.getSnapshot().context.ui.recordLog?.length ?? 0) >=
          Math.min(Number(RECORD_WINDOW), run.records.length),
    );
    const snapshot = actor.getSnapshot().context;
    expect(snapshot.load?.complete).toBe(true);
    expect(snapshot.load?.skeleton.throughSeq).toBe(last);
    const next = gateway.skeletons[0]?.nextSeq ?? 0n;
    const tails = gateway.callsTo('Tail');
    expect(tails.length).toBeLessThanOrEqual(1);
    for (const tail of tails)
      expect(fromBinary(TailRequestSchema, tail.request).fromSeq).toBeGreaterThanOrEqual(
        next - RECORD_WINDOW,
      );
    // The record log holds the latest records even when a keyframe hides them from the Skeleton.
    const rows = snapshot.ui.recordLog ?? [];
    const wanted = Math.min(Number(RECORD_WINDOW), run.records.length);
    expect(rows.slice(-wanted).map(row => row.seq)).toEqual(
      run.records.slice(-wanted).map(record => String(record.seq)),
    );
    actor.stop();
  }
});

test('the history window holds a window of rows however many core records sit among them', async () => {
  for (let seed = 1; seed <= 6; seed += 1) {
    const random = new Seeded(seed);
    const rows = storeRunFixture('short', 100 + random.int(300), seed).records;
    // Core records written before random rows, from none to several times the window.
    const core = Array.from({length: rows.length}, () => 0);
    for (let count = random.int(4 * Number(RECORD_WINDOW)); count > 0; count -= 1) {
      const at = random.int(rows.length);
      core[at] = (core[at] ?? 0) + 1;
    }
    const records = withCoreRecords(rows, core);
    const keyframes = random.pick([[], [records.length], [Math.ceil(records.length / 2)]]);
    const link = {rttMs: 20 + random.int(180), bytesPerMs: 700 * (1 + random.int(4))};
    const {clock, gateway, actor} = open(
      {records, keyframes, replyBytes: GATEWAY_REPLY_BYTES},
      link,
    );
    const wanted = Math.min(Number(RECORD_WINDOW), rows.length);
    const ordinary = records.filter(record => !isCoreBody(record.body));
    const settled = await until(
      clock,
      () => (actor.getSnapshot().context.ui.recordLog?.length ?? 0) >= wanted,
    );
    const label = `seed ${seed}: ${rows.length} rows, ${records.length - rows.length} core`;
    expect(settled, label).toBeDefined();
    expect(
      actor.getSnapshot().context.ui.recordLog?.map(row => row.seq),
      label,
    ).toEqual(ordinary.slice(-wanted).map(record => String(record.seq)));
    // Paging back is bounded: the window's Tail, the failures' pages, and the backfill's pages.
    expect(gateway.callsTo('Tail').length, label).toBeLessThanOrEqual(
      1 + FAILURE_PAGES + BACKFILL_PAGES,
    );
    actor.stop();
  }
});

test('a run with no run.log is refused once, in words, and nothing retries it', async () => {
  for (let seed = 1; seed <= CASES; seed += 1) {
    const {run, link} = generate(seed);
    const {clock, gateway, actor} = open({...run, log: 'unindexed'}, link);
    const refused = await until(
      clock,
      () => loadView(actor.getSnapshot().context).kind === 'failed',
    );
    expect(refused).toBeDefined();
    expect(loadView(actor.getSnapshot().context)).toEqual({kind: 'failed', word: 'No run log'});
    const before = gateway.calls.length;
    await until(clock, () => false, 10 * MINUTE);
    expect(gateway.calls.length).toBe(before);
    expect(gateway.calls.length).toBeLessThanOrEqual(2);
    actor.stop();
  }
});

test('calls per open stay bounded while the link is down or refusing, and the end is a status word', async () => {
  const faults: readonly ((method: string) => SimFault)[] = [
    () => ({kind: 'status', status: 14}),
    () => ({kind: 'down'}),
  ];
  for (let seed = 1; seed <= 12; seed += 1) {
    const {run, link} = generate(seed);
    const {clock, gateway, actor} = open(
      run,
      link,
      method => faults[seed % 2]?.(method) ?? {kind: 'down'},
    );
    await until(clock, () => false, 10 * MINUTE);
    expect(gateway.calls.length).toBeLessThanOrEqual(MAX_ATTEMPTS + 1);
    expect(loadView(actor.getSnapshot().context)).toEqual({
      kind: 'failed',
      word: expect.any(String),
    });
    actor.stop();
  }
});

test('a Tail cut mid-way resumes from its cursor and loses nothing', async () => {
  for (let seed = 1; seed <= CASES; seed += 1) {
    const {run, link} = generate(seed);
    const clean = seed % 2 === 0;
    // Several messages (the service sends a large run so), and a cut after the first of them.
    const {clock, gateway, actor} = open({...run, tailBytes: 16 * 1024}, link, (method, ordinal) =>
      method === `${SERVICE}Tail` && ordinal === 0
        ? {kind: 'cut', afterMessages: 1, clean}
        : undefined,
    );
    await until(clock, () => false, MINUTE);
    const {load} = actor.getSnapshot().context;
    expect(load?.skeleton.throughSeq).toBe(run.records.at(-1)?.seq);
    expect(load?.complete).toBe(true);
    // A cut costs a second Tail from the cursor, never a second Skeleton or a re-read.
    expect(gateway.callsTo('Skeleton')).toHaveLength(1);
    expect(gateway.callsTo('Tail').length).toBeLessThanOrEqual(2);
    expect(gateway.skeletons).toHaveLength(1);
    const tailBytes = gateway.deliveries
      .filter(item => item.method === `${SERVICE}Tail`)
      .reduce((sum, item) => sum + item.bytes, 0);
    // At most the window and everything after the Skeleton's cursor, once, plus message framing.
    const from = (gateway.skeletons[0]?.nextSeq ?? 0n) - RECORD_WINDOW;
    const wanted = run.records.filter(record => record.seq >= from);
    const allowed = wanted.reduce(
      (sum, record) => sum + toBinary(RecordSchema, record).length + 16,
      64,
    );
    expect(tailBytes).toBeLessThanOrEqual(allowed);
    actor.stop();
  }
});

test('a converted run reports progress from its first reply and settles after catch-up', async () => {
  const root = new URL('../../../../../../proto/testdata/conversion/', import.meta.url);
  const values: string[] = JSON.parse(
    gunzipSync(readFileSync(new URL('records.base64.json.gz', root))).toString(),
  );
  const records = values.map(value =>
    fromBinary(RecordSchema, Uint8Array.from(Buffer.from(value, 'base64'))),
  );
  const link = {rttMs: 100, bytesPerMs: 1400};
  const {clock, gateway, actor} = open(
    {records, keyframes: [], replyBytes: GATEWAY_REPLY_BYTES},
    link,
  );
  const at = await until(clock, () => actor.getSnapshot().context.progress.phase !== 'skeleton');
  const first = gateway.deliveries.find(item => item.method === `${SERVICE}Skeleton`);
  if (at === undefined || first === undefined) throw new Error('no paint');
  expect(at).toBeLessThanOrEqual(link.rttMs + first.bytes / link.bytesPerMs + 1);
  expect(gateway.bytesBy(at)).toBeLessThanOrEqual(gateway.replyBytes + 256);
  expect(painted(actor)).toBe(true);
  await until(clock, () => actor.getSnapshot().context.progress.phase === 'complete');
  expect(actor.getSnapshot().context.progress.phase).toBe('complete');
  expect(actor.getSnapshot().context.load?.complete).toBe(true);
  expect(gateway.callsTo('Tail').length).toBeLessThanOrEqual(1);
  for (const tail of gateway.callsTo('Tail'))
    expect(fromBinary(TailRequestSchema, tail.request).fromSeq).toBeGreaterThanOrEqual(
      gateway.skeletons[0]?.nextSeq ?? 0n,
    );
  actor.stop();
});

/** What the gateway sent first, as bytes of Skeleton reply for a run offering only `keyframes`. */
async function firstReply(
  records: SimRun['records'],
  keyframes: readonly number[],
  replyBytes: number,
) {
  const {clock, gateway, actor} = open(
    {records, keyframes, replyBytes},
    {rttMs: 1, bytesPerMs: 1e9},
  );
  await until(clock, () => gateway.skeletons.length > 0);
  actor.stop();
  const reply = gateway.skeletons[0];
  if (!reply) throw new Error('no Skeleton reply');
  return reply;
}

test('a checkpoint past the service’s four newest is never used, even when it is the only one that fits', async () => {
  const records = storeRunFixture('short', 120, 1).records;
  const at = [5, 150, 250, 350, 450];
  // Each checkpoint alone, with room for all: how much of the reply it costs.
  const costs: number[] = [];
  for (const through of at) {
    const reply = await firstReply(records, [through], GATEWAY_REPLY_BYTES);
    costs.push(
      toBinary(
        SkeletonResponseSchema,
        create(SkeletonResponseSchema, {
          keyframe: reply.keyframe,
          descriptorSets: reply.descriptorSets,
        }),
      ).length,
    );
  }
  const budget = Math.floor(((costs[0] ?? 0) + (costs[1] ?? 0)) / 2) + 32 + 8 * 1024;
  const room = await firstReply(records, at.slice(0, 2), budget);
  expect(room.keyframe, 'the oldest of two fits').toBeDefined();
  const reply = await firstReply(records, at, budget);
  expect(reply.keyframe, 'the fifth-newest checkpoint is out of reach').toBeUndefined();
  expect(reply.records.length).toBeGreaterThan(0);
});

test('a run whose log is not created yet gives no content, and a deadline or a retry ends the wait', async () => {
  const records = storeRunFixture('short', 3, 1).records;
  const {clock, gateway, actor} = open({records, log: 'pending'}, {rttMs: 20, bytesPerMs: 1400});
  await until(clock, () => false, MINUTE);
  expect(gateway.skeletons[0]?.complete).toBe(true);
  expect(gateway.skeletons[0]?.records).toHaveLength(0);
  expect(loadView(actor.getSnapshot().context)).toEqual({kind: 'failed', word: 'No records'});
  actor.stop();
});

test('every load ends in content or a final state, whatever the replies are', async () => {
  const faults: readonly ((method: string, ordinal: number) => SimFault | undefined)[] = [
    () => undefined,
    method => (method === `${SERVICE}Skeleton` ? {kind: 'status', status: 14} : undefined),
    method => (method === `${SERVICE}Skeleton` ? {kind: 'down'} : undefined),
    (method, ordinal) =>
      method === `${SERVICE}Tail` && ordinal === 0
        ? {kind: 'cut', afterMessages: 1, clean: ordinal % 2 === 0}
        : undefined,
    () => ({kind: 'status', status: 14}),
  ];
  for (let seed = 1; seed <= CASES; seed += 1) {
    const random = new Seeded(1000 + seed);
    const {run, link} = generate(seed);
    const log = random.pick(['present', 'pending', 'unindexed'] as const);
    const fault = random.pick(faults);
    // A log its writer has not created yet appears after `appears` ms, or never.
    const appears = random.pick([0, 5000, 29_000, 31_000, undefined]);
    const {clock, gateway, actor} = open({...run, log}, link, fault);
    if (log === 'pending' && appears !== undefined)
      clock.schedule(() => gateway.publish(run.records), appears);
    await until(clock, () => false, 10 * MINUTE);
    const view = loadView(actor.getSnapshot().context);
    const painted = paintedSkeleton(actor.getSnapshot().context.load) !== undefined;
    const label = `seed ${seed}: log ${log}, fault ${faults.indexOf(fault)}, appears ${appears}`;
    // Content, or a final state with its Retry: never a blank screen, a bar or a retry in flight.
    expect(['ready', 'failed'], label).toContain(view.kind);
    if (view.kind === 'ready') expect(painted, label).toBe(true);
    actor.stop();
  }
});

/** A short run with an error notice early, then `trailing` ordinary records. */
function withEarlyFailure(trailing: number): SimRun['records'] {
  const base = storeRunFixture('short', 2, 1).records;
  const notice = (message: string, severity: Severity) =>
    create(RecordSchema, {
      epoch: 1,
      body: {case: 'notice', value: {severity, code: NoticeCode.INPUT_MEASUREMENT_FAILED, message}},
    });
  const all = [
    ...base.slice(0, 4),
    notice('Measurement unavailable', Severity.ERROR),
    ...base.slice(4),
    ...Array.from({length: trailing}, (_, index) => notice(`filler ${index}`, Severity.WARNING)),
  ];
  return all.map((record, index) => create(RecordSchema, {...record, seq: BigInt(index + 1)}));
}

/** What the banner says after the page has loaded, and how many Tails the load cost. */
async function bannerOf(records: SimRun['records'], run: Partial<SimRun>) {
  const {clock, gateway, actor} = open({records, ...run}, {rttMs: 20, bytesPerMs: 1400});
  await until(clock, () => false, MINUTE);
  const {load, ui} = actor.getSnapshot().context;
  if (!load) throw new Error('loaded history required');
  const whole = projectSkeleton(load.skeleton, load.skeleton.throughSeq ?? 0n);
  actor.stop();
  return {
    message: skeletonScreenState(SEED, load.skeleton, whole, ui).recordDiagnostic?.message,
    tails: gateway.callsTo('Tail').length,
  };
}

test('a finished run’s failure diagnostic does not depend on how many records follow it', async () => {
  // Tracked keyframes (none, final, middle) and keyframes without failure facts (final, middle),
  // with the notice inside the window, just outside it, and several pages back.
  const cases = [
    ...[0, 257, 700].flatMap(count =>
      (['none', 'final', 'middle'] as const).map(at => ({count, at, legacy: false})),
    ),
    ...[256, 257, 1500].flatMap(count =>
      (['final', 'middle'] as const).map(at => ({count, at, legacy: true})),
    ),
  ];
  for (const {count, at, legacy} of cases) {
    const records = withEarlyFailure(count);
    const through = at === 'final' ? records.length : Math.floor(records.length / 2);
    const label = `${count} records after the notice, ${at} checkpoint, ${legacy ? 'without failure facts' : 'tracked'}`;
    const {message, tails} = await bannerOf(records, {
      keyframes: at === 'none' ? [] : [through],
      legacyKeyframes: legacy,
    });
    expect(message, label).toBe('Measurement unavailable');
    // Bounded cost: the live Tail, plus at most one paged read per page looked back.
    expect(tails, label).toBeLessThanOrEqual(1 + FAILURE_PAGES);
  }
});

test('an old keyframe with no failure ever recorded costs a bounded read, not a replay', async () => {
  const records = storeRunFixture('short', 400, 1).records;
  const {clock, gateway, actor} = open(
    {records, keyframes: [records.length], legacyKeyframes: true},
    {rttMs: 20, bytesPerMs: 1400},
  );
  await until(clock, () => false, MINUTE);
  expect(gateway.callsTo('Tail').length).toBeLessThanOrEqual(1 + FAILURE_PAGES);
  const read = new Set(
    gateway
      .callsTo('Tail')
      .map(call => Number(fromBinary(TailRequestSchema, call.request).fromSeq)),
  );
  // No page starts before FAILURE_PAGES windows back from the first record read.
  expect(Math.min(...read)).toBeGreaterThanOrEqual(
    records.length - (FAILURE_PAGES + 1) * Number(RECORD_WINDOW),
  );
  actor.stop();
});

test('a checkpoint stored over the read limit is skipped though its reply would fit', async () => {
  const records = storeRunFixture('short', 120, 1).records;
  const stored = async (through: number) => {
    const reply = await firstReply(records, [through], GATEWAY_REPLY_BYTES);
    return fromBinary(KeyframeSchema, reply.keyframe ?? new Uint8Array());
  };
  const small = toBinary(KeyframeSchema, await stored(5)).length;
  const large = toBinary(KeyframeSchema, await stored(400)).length;
  expect(large).toBeGreaterThan(small);
  // The limit sits between the two stored sizes: the service reads the older checkpoint only.
  const limit = Math.floor((small + large) / 2) + 90 * 1024;
  const {clock, gateway, actor} = open(
    {records, keyframes: [5, 400], storedKeyframeLimit: limit},
    {rttMs: 1, bytesPerMs: 1e9},
  );
  await until(clock, () => gateway.skeletons.length > 0);
  actor.stop();
  const reply = gateway.skeletons[0];
  expect(fromBinary(KeyframeSchema, reply?.keyframe ?? new Uint8Array()).throughSeq).toBe(
    records[4]?.seq ?? 0n,
  );
});
