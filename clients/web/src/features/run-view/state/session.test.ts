import {describe, expect, test} from 'bun:test';
import {
  BackendClientError,
  type ControlChannelState,
  type RunSnapshot,
} from '@vibesys/backend-client';
import {checkModel, SEEDS, Seeded} from '@vibesys/core-state/testing';
import {
  type Reachability,
  SESSION_INITIAL,
  type SessionAction,
  type SessionRequest,
  type SessionState,
  type SnapshotResult,
  sessionAwaited,
  sessionInvariants,
  sessionStep,
} from './session.js';

const ERRORS = [new Error('snapshot failed'), new Error('socket closed'), new Error('parse')];
const OUTAGE = new BackendClientError('disconnected', 'WebSocket transport error');
const CONTROLS: readonly ControlChannelState[] = [
  {status: 'connected'},
  {status: 'disconnected', error: OUTAGE, everConnected: false, retrying: false},
  {status: 'disconnected', error: OUTAGE, everConnected: true, retrying: true},
];

function reach(random: Seeded): Reachability {
  return {visible: random.next() < 0.8, online: random.next() < 0.8};
}

function maybeError(random: Seeded): Error | null {
  return random.bool() ? null : random.pick(ERRORS);
}

/** The snapshot a load with `token` returned: its sequence names the load, to trace what applied. */
function snapshotOf(token: number): RunSnapshot {
  return {run_id: 'run-1', sequence: token, status: 'running'};
}

function loaded(token: number): SnapshotResult {
  return {ok: true, snapshot: snapshotOf(token)};
}

function snapshotResult(random: Seeded, token: number): SnapshotResult {
  const error = maybeError(random);
  return error === null ? loaded(token) : {ok: false, error};
}

function randomAction(random: Seeded, issued: readonly number[]): SessionAction {
  const token = random.pick(issued);
  switch (random.int(13)) {
    case 0:
      return {type: 'start'};
    case 1:
      return {type: 'snapshot-reply', token, result: snapshotResult(random, token)};
    case 2:
      return {type: 'subscribe-reply', token, error: maybeError(random)};
    case 3:
      return {
        type: 'refresh-reply',
        token,
        outcome: random.bool()
          ? {kind: 'snapshot', result: snapshotResult(random, token)}
          : {kind: 'redial-failed', error: random.pick(ERRORS)},
      };
    case 4:
      return {
        type: 'stream',
        state: random.bool()
          ? {status: 'connected'}
          : {status: 'disconnected', error: random.pick(ERRORS)},
      };
    case 5:
      return {type: 'fault', error: random.pick(ERRORS)};
    case 6:
      return {type: 'controls', state: random.pick(CONTROLS)};
    case 7:
    case 8:
      return {type: 'wake', reach: reach(random)};
    case 9:
      return {type: 'reattach', reach: reach(random), runEnded: random.bool()};
    case 10:
      return {type: 'offline', runEnded: random.bool()};
    case 11:
      return {type: 'reconnect-controls'};
    default:
      // Rare, so most sequences stay open long enough to interleave.
      return random.int(4) === 0 ? {type: 'close'} : {type: 'start'};
  }
}

function issues(request: SessionRequest): number | null {
  return 'token' in request ? request.token : null;
}

/** The reply slot an action answers, by the reply's type; null for any other action. */
function replySlot(action: SessionAction): {slot: string; token: number} | null {
  switch (action.type) {
    case 'snapshot-reply':
      return {slot: 'snapshot', token: action.token};
    case 'subscribe-reply':
      return {slot: 'subscribe', token: action.token};
    case 'refresh-reply':
      return {slot: 'refresh', token: action.token};
    default:
      return null;
  }
}

/** A snapshot is applied only from the awaited reply that carried it, and only when newer. */
function snapshotProperties(
  before: SessionState,
  action: SessionAction,
  requests: readonly SessionRequest[],
): void {
  const applied = requests.flatMap(request =>
    request.kind === 'apply-snapshot' ? [request.snapshot.sequence] : [],
  );
  if (applied.length === 0) return;
  const reply = replySlot(action);
  expect(reply).not.toBeNull();
  expect(applied).toEqual([reply?.token ?? -1]);
  expect(before.kind).toBe('open');
  if (before.kind === 'open' && reply !== null) {
    expect(sessionAwaited(before)[reply.slot as 'snapshot' | 'refresh']).toBe(reply.token);
    expect(reply.token).toBeGreaterThan(before.applied);
  }
}

/** Recovery is single-flight and keeps the strongest intent until the stream gets it. */
function recoveryProperties(
  before: SessionState,
  action: SessionAction,
  requests: readonly SessionRequest[],
): void {
  if (requests.some(request => request.kind === 'refresh')) {
    // A refresh starts only when none is in flight, and only when the page can reach anything.
    expect(before.kind === 'open' && before.recovery.kind === 'none').toBe(true);
    const reachable =
      (action.type === 'wake' || action.type === 'reattach') &&
      action.reach.visible &&
      action.reach.online;
    expect(reachable).toBe(true);
  }
  const intent =
    before.kind === 'open' && before.recovery.kind === 'refreshing' ? before.recovery.intent : null;
  for (const request of requests) {
    // The stream recovers with the intent the flight held when it settled: a Reattach that
    // overlapped a lifecycle retry still gets its fresh bootstrap.
    if (request.kind === 'recover-stream') expect<string | null>(request.mode).toBe(intent);
  }
}

function properties(
  before: SessionState,
  action: SessionAction,
  after: SessionState,
  requests: readonly SessionRequest[],
): void {
  // The snapshot on screen is never forgotten.
  if (before.view.hasSnapshot) expect(after.view.hasSnapshot).toBe(true);
  // A closed session stays closed and asks for nothing.
  if (before.kind === 'closed') expect({after, requests}).toEqual({after: before, requests: []});
  recoveryProperties(before, action, requests);
  snapshotProperties(before, action, requests);
  if (action.type === 'close' && before.kind === 'open') {
    expect({kind: after.kind, requests}).toEqual({kind: 'closed', requests: [{kind: 'close'}]});
  }
  // A healthy ended run's Reattach is inert.
  if (action.type === 'reattach' && action.runEnded && before.view.status !== 'stale') {
    expect(after).toBe(before);
  }
}

/**
 * Start-up asks for a snapshot (token 1) and a wake overlaps it with a refresh (token 2); both
 * replies, plus a duplicate of one, arrive in a seeded order. Returns the loads applied, in order.
 */
function overlappingSnapshots(random: Seeded): number[] {
  const visible = {visible: true, online: true};
  let state = sessionStep(SESSION_INITIAL, {type: 'start'}).state;
  state = sessionStep(state, {type: 'wake', reach: visible}).state;
  const startup: SessionAction = {type: 'snapshot-reply', token: 1, result: loaded(1)};
  const refresh: SessionAction = {
    type: 'refresh-reply',
    token: 2,
    outcome: {kind: 'snapshot', result: loaded(2)},
  };
  const replies = [startup, refresh, random.bool() ? startup : refresh]
    .map(reply => ({reply, key: random.next()}))
    .sort((left, right) => left.key - right.key);
  const applied: number[] = [];
  for (const {reply} of replies) {
    const next = sessionStep(state, reply);
    for (const request of next.requests) {
      if (request.kind === 'apply-snapshot') applied.push(request.snapshot.sequence);
    }
    state = next.state;
  }
  return applied;
}

describe('sessionStep', () => {
  test('holds its invariants and drops stale replies over random interleavings', () => {
    checkModel({
      initial: SESSION_INITIAL,
      step: sessionStep,
      action: randomAction,
      issues,
      awaited: sessionAwaited,
      reply: replySlot,
      invariants: sessionInvariants,
      also: properties,
    });
  });

  test('starts with the snapshot, then the stream, and is connected once both succeed', () => {
    const asked = sessionStep(SESSION_INITIAL, {type: 'start'});
    expect(asked.requests).toEqual([{kind: 'load-snapshot', token: 1}]);
    const reply = sessionStep(asked.state, {type: 'snapshot-reply', token: 1, result: loaded(1)});
    expect(reply.requests).toEqual([
      {kind: 'apply-snapshot', snapshot: snapshotOf(1)},
      {kind: 'subscribe', token: 2},
    ]);
    expect(reply.state.view.hasSnapshot).toBe(true);
    const live = sessionStep(reply.state, {type: 'subscribe-reply', token: 2, error: null});
    expect(live.state.view.status).toBe('connected');
  });

  test('a failed start-up snapshot keeps the page stale after the stream subscribes', () => {
    const failure = new Error('no snapshot');
    let state = sessionStep(SESSION_INITIAL, {type: 'start'}).state;
    state = sessionStep(state, {
      type: 'snapshot-reply',
      token: 1,
      result: {ok: false, error: failure},
    }).state;
    state = sessionStep(state, {type: 'subscribe-reply', token: 2, error: null}).state;
    expect(state.view).toMatchObject({status: 'stale', error: failure, hasSnapshot: false});
  });

  test('a Reattach during a lifecycle refresh promotes the stream recovery', () => {
    const visible = {visible: true, online: true};
    let state = sessionStep(SESSION_INITIAL, {type: 'wake', reach: visible}).state;
    const reattach = sessionStep(state, {type: 'reattach', reach: visible, runEnded: false});
    expect(reattach.requests).toEqual([]);
    state = reattach.state;
    const settled = sessionStep(state, {
      type: 'refresh-reply',
      token: 1,
      outcome: {kind: 'snapshot', result: loaded(1)},
    });
    expect(settled.requests).toEqual([
      {kind: 'apply-snapshot', snapshot: snapshotOf(1)},
      {kind: 'recover-stream', mode: 'rebootstrap'},
    ]);
  });

  test('overlapping start-up and refresh snapshots apply only the newest awaited one, in any order', () => {
    for (const seed of SEEDS) {
      const applied = overlappingSnapshots(new Seeded(seed));
      // Whatever arrived first, the run fold ends on the refresh's newer snapshot, applied once,
      // and never goes back to an older one.
      expect({
        seed,
        applied: applied.at(-1),
        once: new Set(applied).size === applied.length,
      }).toEqual({seed, applied: 2, once: true});
      expect([...applied].sort((left, right) => left - right)).toEqual(applied);
    }
  });
});
