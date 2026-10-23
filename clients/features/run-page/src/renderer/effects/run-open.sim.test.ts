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
import {fromBinary, toBinary} from '@bufbuild/protobuf';
import {RecordSchema, TailRequestSchema} from '@vibesys/proto';
import {storeRunFixture} from '@vibesys/run-fixtures';
import {MAX_ATTEMPTS, RECORD_WINDOW, RETRY_CAP_MS} from '@vibesys/run-skeleton';
import {FakeSkeletonCache} from '@vibesys/run-skeleton/cache';
import {gatewayReader} from '@vibesys/run-skeleton/reader';
import {
  GATEWAY_REPLY_BYTES,
  type LinkModel,
  type SimFault,
  SimGateway,
  type SimRun,
} from '@vibesys/run-skeleton/testing';
import {Seeded} from '@vibesys/store/testing';
import {FakeClock} from '@vibesys/transport/testing';
import {loadView, paintedSkeleton} from '../../state/index.js';
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
      reader: gatewayReader(gateway, new FakeSkeletonCache(), clock.schedule),
      watchSetting: () => () => {},
      setting: async () => true,
    },
    clock.schedule,
    {seed: SEED},
  );
  return {clock, gateway, actor};
}

const painted = (actor: ReturnType<typeof open>['actor']): boolean =>
  paintedSkeleton(actor.getSnapshot().context.load) !== undefined;

test('the first paint costs one round trip and one Skeleton reply, whatever the history holds', async () => {
  for (let seed = 1; seed <= CASES; seed += 1) {
    const {run, link} = generate(seed);
    const {clock, gateway, actor} = open(run, link);
    const at = await until(clock, () => painted(actor));
    const first = gateway.deliveries.find(item => item.method === `${SERVICE}Skeleton`);
    expect(at).toBeDefined();
    if (at === undefined || first === undefined) throw new Error(`seed ${seed}: no paint`);
    expect(at).toBeLessThanOrEqual(link.rttMs + first.bytes / link.bytesPerMs + 1);
    // Only the Skeleton reply (and the liveness hello) crossed the link before the paint.
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

test('a run with no run.log is refused once, in words, and nothing retries it', async () => {
  for (let seed = 1; seed <= CASES; seed += 1) {
    const {run, link} = generate(seed);
    const {clock, gateway, actor} = open({...run, logPresent: false}, link);
    const refused = await until(
      clock,
      () => loadView(actor.getSnapshot().context).kind === 'failed',
    );
    expect(refused).toBeDefined();
    expect(loadView(actor.getSnapshot().context)).toEqual({kind: 'failed', word: 'No run log'});
    const before = gateway.calls.length;
    await until(clock, () => false, 10 * MINUTE);
    expect(gateway.calls.length).toBe(before);
    expect(
      gateway.calls.filter(call => !call.method.endsWith('WatchRuns')).length,
    ).toBeLessThanOrEqual(2);
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
    const history = gateway.calls.filter(call => !call.method.endsWith('WatchRuns')).length;
    const liveness = gateway.callsTo('WatchRuns').length;
    expect(history).toBeLessThanOrEqual(MAX_ATTEMPTS + 1);
    expect(liveness).toBeLessThanOrEqual(
      MAX_ATTEMPTS + 1 + Math.ceil((10 * MINUTE) / RETRY_CAP_MS),
    );
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
    const {clock, gateway, actor} = open(run, link, (method, ordinal) =>
      method === `${SERVICE}Tail` && ordinal === 0
        ? {kind: 'cut', afterMessages: 0, clean}
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

test('a converted run (the retained conversion corpus) paints from its first reply and reads on', async () => {
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
  const at = await until(clock, () => painted(actor));
  const first = gateway.deliveries.find(item => item.method === `${SERVICE}Skeleton`);
  if (at === undefined || first === undefined) throw new Error('no paint');
  expect(at).toBeLessThanOrEqual(link.rttMs + first.bytes / link.bytesPerMs + 1);
  expect(gateway.bytesBy(at)).toBeLessThanOrEqual(gateway.replyBytes + 256);
  await until(clock, () => actor.getSnapshot().context.load?.complete === true);
  expect(gateway.callsTo('Tail').length).toBeLessThanOrEqual(1);
  for (const tail of gateway.callsTo('Tail'))
    expect(fromBinary(TailRequestSchema, tail.request).fromSeq).toBeGreaterThanOrEqual(
      gateway.skeletons[0]?.nextSeq ?? 0n,
    );
  actor.stop();
});
