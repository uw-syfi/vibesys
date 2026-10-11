import {describe, expect, test} from 'bun:test';
import {BackendClientError, type RunSnapshot} from '@vibesys/contracts';
import {checkMachine, type Live, type MachineAction} from '@vibesys/machine/testing';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import type {ControlChannelState} from '@vibesys/transport';
import {initialTransition, transition} from 'xstate';
import {
  type Reachability,
  type RefreshOutcome,
  type SessionEvent,
  type SessionSnapshot,
  type SnapshotResult,
  sessionInvariants,
  sessionMachine,
} from './session.js';

const ERRORS = [new Error('snapshot failed'), new Error('socket closed'), new Error('parse')];
const OUTAGE = new BackendClientError('disconnected', 'WebSocket transport error');
const CONTROLS: readonly ControlChannelState[] = [
  {status: 'connected'},
  {status: 'disconnected', error: OUTAGE, everConnected: false, retrying: false},
  {status: 'disconnected', error: OUTAGE, everConnected: true, retrying: true},
];
const VISIBLE: Reachability = {visible: true, online: true};

function reach(random: Seeded): Reachability {
  return {visible: random.next() < 0.8, online: random.next() < 0.8};
}

function maybeError(random: Seeded): Error | null {
  return random.bool() ? null : random.pick(ERRORS);
}

/** A snapshot whose sequence names the load that returned it, to trace what was applied. */
function snapshotOf(load: number): RunSnapshot {
  return {run_id: 'run-1', sequence: load, status: 'running'};
}

function loaded(load: number): SnapshotResult {
  return {ok: true, snapshot: snapshotOf(load)};
}

function snapshotResult(random: Seeded, load: number): SnapshotResult {
  const error = maybeError(random);
  return error === null ? loaded(load) : {ok: false, error};
}

/** The outcome of a live operation, carrying the issue sequence of its load. */
function outcome(random: Seeded, live: Live, operation: string, snapshot: SessionSnapshot) {
  const {startupLoad, refreshLoad} = snapshot.context;
  switch (operation) {
    case 'loadSnapshot':
      return live.done(operation, snapshotResult(random, startupLoad));
    case 'subscribe':
      return live.done(operation, maybeError(random));
    default: {
      const result: RefreshOutcome = random.bool()
        ? {kind: 'snapshot', result: snapshotResult(random, refreshLoad)}
        : {kind: 'redial-failed', error: random.pick(ERRORS)};
      return live.done(operation, result);
    }
  }
}

function randomEvent(
  random: Seeded,
  live: Live,
  snapshot: SessionSnapshot,
): SessionEvent | {readonly type: string} {
  if (live.operations.length > 0 && random.int(3) === 0) {
    return outcome(random, live, random.pick(live.operations), snapshot);
  }
  switch (random.int(11)) {
    case 0:
      return {type: 'start'};
    case 1:
      return random.bool()
        ? {type: 'stream', state: {status: 'connected'}}
        : {type: 'stream', state: {status: 'disconnected', error: random.pick(ERRORS)}};
    case 2:
      return {type: 'fault', error: random.pick(ERRORS)};
    case 3:
      return {type: 'controls', state: random.pick(CONTROLS)};
    case 4:
    case 5:
      return {type: 'wake', reach: reach(random)};
    case 6:
      return {type: 'reattach', reach: reach(random), runEnded: random.bool()};
    case 7:
      return {type: 'offline', runEnded: random.bool()};
    case 8:
      return {type: 'reconnect-controls'};
    default:
      // Rare, so most sequences stay open long enough to interleave.
      return random.int(4) === 0 ? {type: 'close'} : {type: 'start'};
  }
}

function named(actions: readonly MachineAction[], type: string): unknown[] {
  return actions.filter(action => action.type === type).map(action => action.params);
}

function started(actions: readonly MachineAction[], operation: string): boolean {
  return actions.some(
    action =>
      action.type === 'xstate.spawnChild' && (action.params as {id?: string}).id === operation,
  );
}

/** A snapshot is applied only from a settled load, and only when issued after the last applied. */
function snapshotProperties(
  before: SessionSnapshot,
  event: {readonly type: string},
  actions: readonly MachineAction[],
): void {
  const applied = named(actions, 'applySnapshot').flatMap(params => {
    const {snapshot} = params as {snapshot: RunSnapshot | null};
    return snapshot === null ? [] : [snapshot.sequence];
  });
  if (applied.length === 0) return;
  expect(['xstate.done.actor.loadSnapshot', 'xstate.done.actor.refresh']).toContain(event.type);
  expect(applied).toHaveLength(1);
  expect(applied[0]).toBeGreaterThan(before.context.applied);
}

/** Recovery is single-flight and keeps the strongest intent until the stream gets it. */
function recoveryProperties(
  before: SessionSnapshot,
  event: {readonly type: string},
  actions: readonly MachineAction[],
): void {
  if (started(actions, 'refresh')) {
    // A refresh starts only when none is in flight, and only when the page can reach anything.
    expect(before.matches({open: {recovery: 'none'}})).toBe(true);
    const {reach: asked} = event as {reach?: Reachability};
    expect(asked?.visible === true && asked.online).toBe(true);
  }
  for (const params of named(actions, 'recoverStream')) {
    // The stream recovers with the intent the flight held when it settled: a Reattach that
    // overlapped a lifecycle retry still gets its fresh bootstrap.
    expect((params as {mode: string}).mode).toBe(before.context.intent);
  }
}

function properties(
  before: SessionSnapshot,
  event: {readonly type: string},
  after: SessionSnapshot,
  actions: readonly MachineAction[],
): void {
  // The snapshot on screen is never forgotten.
  if (before.context.view.hasSnapshot) expect(after.context.view.hasSnapshot).toBe(true);
  recoveryProperties(before, event, actions);
  snapshotProperties(before, event, actions);
  if (event.type === 'close' && before.status === 'active') {
    expect(after.status).toBe('done');
    expect(named(actions, 'closeShell')).toHaveLength(1);
  }
  // A healthy ended run's Reattach is inert.
  const reattach = event as Partial<Extract<SessionEvent, {type: 'reattach'}>>;
  if (event.type === 'reattach' && reattach.runEnded && before.context.view.status !== 'stale') {
    expect({context: after.context, actions}).toEqual({context: before.context, actions: []});
  }
}

/** Fold `events` from the initial snapshot; the actions of the last step. */
function run(
  events: readonly (SessionEvent | {readonly type: string; readonly output?: unknown})[],
) {
  let [snapshot, actions] = initialTransition(sessionMachine);
  for (const event of events) {
    [snapshot, actions] = transition(sessionMachine, snapshot, event as SessionEvent);
  }
  return {snapshot, actions: actions as readonly MachineAction[]};
}

const done = (operation: string, output: unknown) => ({
  type: `xstate.done.actor.${operation}`,
  output,
});

/**
 * Start-up loads a snapshot (load 1) and a wake overlaps it with a refresh (load 2); both settle in
 * a seeded order. Returns the loads applied, in order.
 */
function overlappingSnapshots(random: Seeded): number[] {
  const startup = done('loadSnapshot', loaded(1));
  const refresh = done('refresh', {kind: 'snapshot', result: loaded(2)});
  let {snapshot} = run([{type: 'start'}, {type: 'wake', reach: VISIBLE}]);
  const applied: number[] = [];
  for (const reply of random.bool() ? [startup, refresh] : [refresh, startup]) {
    const [next, actions] = transition(sessionMachine, snapshot, reply as unknown as SessionEvent);
    for (const params of named(actions as readonly MachineAction[], 'applySnapshot')) {
      const {snapshot: applies} = params as {snapshot: RunSnapshot | null};
      if (applies !== null) applied.push(applies.sequence);
    }
    snapshot = next;
  }
  return applied;
}

describe('session machine', () => {
  test('holds its invariants over random interleavings', () => {
    checkMachine({
      machine: sessionMachine,
      input: undefined,
      event: randomEvent,
      invariants: sessionInvariants,
      also: properties,
    });
  });

  test('starts with the snapshot, then the stream, and is connected once both succeed', () => {
    const asked = run([{type: 'start'}]);
    expect(asked.snapshot.matches({open: {startup: 'snapshot'}})).toBe(true);
    const reply = run([{type: 'start'}, done('loadSnapshot', loaded(1))]);
    expect(named(reply.actions, 'applySnapshot')).toEqual([{snapshot: snapshotOf(1)}]);
    expect(reply.snapshot.matches({open: {startup: 'subscribing'}})).toBe(true);
    expect(reply.snapshot.context.view.hasSnapshot).toBe(true);
    const live = run([{type: 'start'}, done('loadSnapshot', loaded(1)), done('subscribe', null)]);
    expect(live.snapshot.context.view.status).toBe('connected');
  });

  test('a failed start-up snapshot keeps the page stale after the stream subscribes', () => {
    const failure = new Error('no snapshot');
    const {snapshot} = run([
      {type: 'start'},
      done('loadSnapshot', {ok: false, error: failure}),
      done('subscribe', null),
    ]);
    expect(snapshot.context.view).toMatchObject({
      status: 'stale',
      error: failure,
      hasSnapshot: false,
    });
  });

  test('a Reattach during a lifecycle refresh promotes the stream recovery', () => {
    const reattach = run([
      {type: 'wake', reach: VISIBLE},
      {type: 'reattach', reach: VISIBLE, runEnded: false},
    ]);
    expect(started(reattach.actions, 'refresh')).toBe(false);
    const settled = run([
      {type: 'wake', reach: VISIBLE},
      {type: 'reattach', reach: VISIBLE, runEnded: false},
      done('refresh', {kind: 'snapshot', result: loaded(1)}),
    ]);
    expect(named(settled.actions, 'applySnapshot')).toEqual([{snapshot: snapshotOf(1)}]);
    expect(named(settled.actions, 'recoverStream')).toEqual([{mode: 'rebootstrap'}]);
  });

  test('overlapping start-up and refresh snapshots apply only the newest, in any order', () => {
    for (const seed of SEEDS) {
      const applied = overlappingSnapshots(new Seeded(seed));
      // Whatever arrived first, the run fold ends on the refresh's newer snapshot, applied once,
      // and never goes back to an older one.
      expect({seed, last: applied.at(-1), once: new Set(applied).size === applied.length}).toEqual({
        seed,
        last: 2,
        once: true,
      });
      expect([...applied].sort((left, right) => left - right)).toEqual(applied);
    }
  });
});
