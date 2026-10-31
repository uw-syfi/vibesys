import {expect, test} from 'bun:test';
import {SUBSCRIPTION_LINGER_MS} from '@vibesys/connection';
import {hubRig, settle} from '@vibesys/connection/testing';
import {machineEntry} from '@vibesys/subscription/testing';
import fc from 'fast-check';
import {
  cardStatus,
  FAILURE_WORDS,
  type GaugeView,
  initialMachines,
  type Machine,
  machinesStep,
  type StatsView,
  statsView,
} from '../../state/index.js';
import {createLiveStats} from './live.js';

const HOST = 'ssh:host-a';

const OPS = [
  'snapshot',
  'entry',
  'beat',
  'fail',
  'end',
  'tick1s',
  'tick5s',
  'tick30s',
  'retry',
  'wake',
  'remove',
  'leave',
  'join',
] as const;
type Op = (typeof OPS)[number];

/** A machine whose own check passed, as the card's status folds it with the stream's. */
function readyMachine(): Machine {
  let {state} = initialMachines();
  state = machinesStep(state, {
    type: 'listed',
    token: 1,
    result: {ok: true, value: [{host: HOST, label: 'host-a', checkout: '~/vibesys'}]},
  }).state;
  const [machine] = state.machines;
  if (machine === undefined) throw new Error('no machine');
  const facts = {
    checkout: null,
    hardware: {cpu_cores: 8, gpus: [], accelerator: 'cpu'},
    docker: {reachable: true, version: '29.1'},
    harness_clis: [],
    runs: {live: 0, ended_today: 0},
  };
  return {...machine, report: {kind: 'ready' as const, facts}, checking: null};
}

/** The word a stream's state is owed, said again from the state alone. */
function owed(view: StatsView): string {
  switch (view.kind) {
    case 'waiting':
      return 'Connecting';
    case 'failed':
      return FAILURE_WORDS[view.reason];
    case 'values':
      if (view.stale === null) return 'Online';
      return view.stale.reconnecting ? 'Connecting' : 'Stale';
  }
}

function figures(gauges: readonly GaugeView[]): string[] {
  return gauges.map(gauge => `${gauge.label} ${gauge.figure}`);
}

/** One host's card over the real hub and a fake clock, and what a test has seen of it. */
function rig() {
  const {clock, gateway, subscriptions} = hubRig();
  const live = createLiveStats(subscriptions.port, clock.schedule, () => clock.now());
  const source = live.source(HOST);
  return {
    clock,
    gateway,
    subscriptions,
    source,
    machine: readyMachine(),
    release: source.subscribe(() => {}) as (() => void) | null,
    removed: false,
    sample: 0,
    lastValues: null as string[] | null,
  };
}
type Rig = ReturnType<typeof rig>;

const streamOf = (world: Rig) => world.gateway.streams.filter(each => each.open).at(-1);

function entry(world: Rig) {
  world.sample += 1;
  return machineEntry('machine', world.sample, {cpuPercent: world.sample % 100});
}

/** What each op does to the world: the host speaks, time passes, or a window asks or leaves. */
const DO: Readonly<Record<Op, (world: Rig) => void>> = {
  snapshot: world => {
    const stream = streamOf(world);
    stream?.reached('runs');
    stream?.send({case: 'entry', value: entry(world)});
    stream?.send({case: 'synced', value: {}});
  },
  entry: world => streamOf(world)?.send({case: 'entry', value: entry(world)}),
  beat: world => streamOf(world)?.send({case: 'heartbeat', value: {}}),
  fail: world => streamOf(world)?.fail(),
  end: world => streamOf(world)?.end(),
  tick1s: world => world.clock.advanceBy(1_000),
  tick5s: world => world.clock.advanceBy(5_000),
  tick30s: world => world.clock.advanceBy(30_000),
  retry: world => world.source.retry(),
  wake: world => world.subscriptions.wake(),
  remove: world => {
    world.subscriptions.closeHost(HOST);
    world.removed = true;
  },
  leave: world => {
    world.release?.();
    world.release = null;
  },
  join: world => {
    if (world.release !== null) return;
    world.release = world.source.subscribe(() => {});
    // A host that was removed and is shown again is a new subscription, with a stream of its own.
    world.removed = false;
  },
};

/** What must hold of the card after any op. */
function verify(world: Rig, op: Op, before: StatsView): void {
  const state = world.source.getSnapshot();
  const view = statsView(state);
  const status = cardStatus(world.machine, view);
  // One status, and the word the stream's state is owed; `Online` only while the stream delivers.
  expect(status.word).toBe(owed(view));
  expect(status.word === 'Online').toBe(state.kind === 'live');
  expect(view.kind === 'values').toBe(state.kind === 'live' || state.kind === 'stale');
  if (view.kind === 'values') {
    expect(view.gauges.length).toBeGreaterThanOrEqual(2);
    // What a stale card shows is what the host last read, never something else.
    if (view.stale !== null && world.lastValues !== null)
      expect(figures(view.gauges)).toEqual(world.lastValues);
    if (view.stale === null) world.lastValues = figures(view.gauges);
  }
  // A press that is answered: a failed host that is asked again is no longer failed.
  if (op === 'retry' && before.kind === 'failed' && before.retry && !world.removed)
    expect(view.kind).not.toBe('failed');
  // A host is read through one stream at a time, and none while it is removed.
  expect(world.gateway.open.length).toBeLessThanOrEqual(1);
  if (world.removed) expect(world.gateway.open).toHaveLength(0);
}

async function run(ops: readonly Op[]): Promise<void> {
  const world = rig();
  for (const op of ops) {
    const before = statsView(world.source.getSnapshot());
    DO[op](world);
    await settle();
    if (world.release !== null) verify(world, op, before);
  }
  // Whatever happened, leaving releases every stream and every timer.
  world.release?.();
  world.clock.advanceBy(SUBSCRIPTION_LINGER_MS);
  await settle();
  expect(world.gateway.open).toHaveLength(0);
  expect(world.clock.pending).toBe(0);
}

test('property: in any order of losses, retries, wakes, removals and comings and goings, a card has one status that its stream is owed, and leaves nothing running', async () => {
  await fc.assert(fc.asyncProperty(fc.array(fc.constantFrom(...OPS), {maxLength: 40}), run), {
    numRuns: 150,
  });
});
