import {describe, expect, test} from 'bun:test';
import {BackendClientError, type RunSnapshot} from '@vibesys/contracts';
import {checkModel, type Seeded} from '@vibesys/core-state/testing';
import type {ControlChannelState} from '@vibesys/transport';
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
  switch (random.int(14)) {
    case 0:
      return {type: 'start'};
    case 1:
      return {type: 'subscribe-reply', token, error: maybeError(random)};
    case 2:
      return {
        type: 'refresh-reply',
        token,
        outcome: random.pick([
          {kind: 'snapshot', result: snapshotResult(random, token)} as const,
          {kind: 'redial-failed', error: random.pick(ERRORS)} as const,
          {kind: 'ready'} as const,
        ]),
      };
    case 3:
      return {type: 'stream-established', storeId: `store-${random.int(4) + 1}`};
    case 4:
      return {
        type: 'replacement-snapshot-reply',
        token,
        storeId: `store-${random.int(4) + 1}`,
        result: snapshotResult(random, token),
      };
    case 5:
      return {
        type: 'stream',
        state: random.bool()
          ? {status: 'connected'}
          : {status: 'disconnected', error: random.pick(ERRORS)},
      };
    case 6:
      return {type: 'fault', error: random.pick(ERRORS)};
    case 7:
      return {type: 'controls', state: random.pick(CONTROLS)};
    case 8:
    case 9:
      return {type: 'wake', reach: reach(random)};
    case 10:
      return {type: 'reattach', reach: reach(random), runEnded: random.bool()};
    case 11:
      return {type: 'offline', runEnded: random.bool()};
    case 12:
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
    case 'subscribe-reply':
      return {slot: 'subscribe', token: action.token};
    case 'refresh-reply':
      return {slot: 'refresh', token: action.token};
    case 'replacement-snapshot-reply':
      return {slot: 'replacement', token: action.token};
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
    expect(sessionAwaited(before)[reply.slot as 'refresh' | 'replacement']).toBe(reply.token);
    expect(reply.token).toBeGreaterThan(before.applied);
  }
}

function expectRefreshStartsOnce(
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
}

function storeReplacementRecovery(action: SessionAction): 'rebootstrap' | null {
  if (action.type === 'replacement-snapshot-reply') {
    return !action.result.ok && action.result.reason === 'store-replaced' ? 'rebootstrap' : null;
  }
  if (action.type !== 'refresh-reply' || action.outcome.kind !== 'snapshot') return null;
  return !action.outcome.result.ok && action.outcome.result.reason === 'store-replaced'
    ? 'rebootstrap'
    : null;
}

/** Recovery is single-flight and keeps the strongest intent until the stream gets it. */
function recoveryProperties(
  before: SessionState,
  action: SessionAction,
  requests: readonly SessionRequest[],
): void {
  expectRefreshStartsOnce(before, action, requests);
  const replacementMode = storeReplacementRecovery(action);
  const heldIntent =
    before.kind === 'open' && before.recovery.kind === 'refreshing' ? before.recovery.intent : null;
  for (const request of requests) {
    // The stream recovers with the intent the flight held when it settled: a Reattach that
    // overlapped a lifecycle retry still gets its fresh bootstrap.
    if (request.kind !== 'recover-stream') continue;
    if (replacementMode !== null) expect(request.mode).toBe(replacementMode);
    else if (heldIntent === 'rebootstrap') expect(request.mode).toBe('rebootstrap');
    else expect(['retry', 'rebootstrap']).toContain(request.mode);
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

  test('starts the stream, then applies only a snapshot fenced to its store', () => {
    const asked = sessionStep(SESSION_INITIAL, {type: 'start'});
    expect(asked.requests).toEqual([{kind: 'subscribe', token: 1}]);
    const established = sessionStep(asked.state, {
      type: 'stream-established',
      storeId: 'store-a',
    });
    expect(established.requests).toEqual([
      {kind: 'load-replacement-snapshot', token: 2, storeId: 'store-a'},
    ]);
    const subscribed = sessionStep(established.state, {
      type: 'subscribe-reply',
      token: 1,
      error: null,
    });
    const live = sessionStep(subscribed.state, {
      type: 'replacement-snapshot-reply',
      token: 2,
      storeId: 'store-a',
      result: loaded(2),
    });
    expect(live.requests).toEqual([{kind: 'apply-snapshot', snapshot: snapshotOf(2)}]);
    expect(live.state.view.hasSnapshot).toBe(true);
    expect(live.state.view.status).toBe('connected');
  });

  test('a failed start-up snapshot keeps the page stale after the stream subscribes', () => {
    const failure = new Error('no snapshot');
    let state = sessionStep(SESSION_INITIAL, {type: 'start'}).state;
    state = sessionStep(state, {type: 'stream-established', storeId: 'store-a'}).state;
    state = sessionStep(state, {type: 'subscribe-reply', token: 1, error: null}).state;
    state = sessionStep(state, {
      type: 'replacement-snapshot-reply',
      token: 2,
      storeId: 'store-a',
      result: {ok: false, error: failure},
    }).state;
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
    expect(settled.requests).toEqual([{kind: 'recover-stream', mode: 'rebootstrap'}]);
  });

  test('a failed fresh stream releases recovery for a later Reattach', () => {
    const visible = {visible: true, online: true};
    const failure = ERRORS[1] ?? new Error();
    const reports: readonly SessionAction[] = [
      {type: 'stream', state: {status: 'disconnected', error: failure}},
      {type: 'fault', error: failure},
    ];
    for (const report of reports) {
      let state = sessionStep(SESSION_INITIAL, {type: 'start'}).state;
      state = sessionStep(state, {type: 'stream-established', storeId: 'store-a'}).state;
      state = sessionStep(state, {
        type: 'replacement-snapshot-reply',
        token: 2,
        storeId: 'store-a',
        result: loaded(2),
      }).state;
      state = sessionStep(state, {type: 'subscribe-reply', token: 1, error: null}).state;

      const first = sessionStep(state, {type: 'reattach', reach: visible, runEnded: false});
      expect(first.requests).toEqual([{kind: 'refresh', token: 3, mode: 'rebootstrap'}]);
      const establishing = sessionStep(first.state, {
        type: 'refresh-reply',
        token: 3,
        outcome: {kind: 'ready'},
      });
      expect(establishing.requests).toEqual([{kind: 'recover-stream', mode: 'rebootstrap'}]);

      const failed = sessionStep(establishing.state, report);
      const retry = sessionStep(failed.state, {
        type: 'reattach',
        reach: visible,
        runEnded: false,
      });
      expect(retry.requests).toEqual([{kind: 'refresh', token: 4, mode: 'rebootstrap'}]);
    }
  });

  test('fences pre-replacement replies and accepts only the proven generation snapshot', () => {
    const visible = {visible: true, online: true};
    const refreshing = sessionStep(SESSION_INITIAL, {type: 'wake', reach: visible}).state;
    const replaced = sessionStep(refreshing, {type: 'stream-established', storeId: 'store-b'});
    expect(replaced.requests).toEqual([
      {kind: 'load-replacement-snapshot', token: 2, storeId: 'store-b'},
    ]);

    const staleRefresh = sessionStep(replaced.state, {
      type: 'refresh-reply',
      token: 1,
      outcome: {kind: 'snapshot', result: loaded(1)},
    });
    expect(staleRefresh).toEqual({state: replaced.state, requests: []});

    const accepted = sessionStep(staleRefresh.state, {
      type: 'replacement-snapshot-reply',
      token: 2,
      storeId: 'store-b',
      result: loaded(2),
    });
    expect(accepted.requests).toEqual([
      {kind: 'apply-snapshot', snapshot: snapshotOf(2)},
      {kind: 'reevaluate-stream'},
    ]);
  });

  test('a superseded fenced snapshot keeps recovery owned until a fresh store is established', () => {
    const visible = {visible: true, online: true};
    const refreshing = sessionStep(SESSION_INITIAL, {type: 'wake', reach: visible}).state;
    const established = sessionStep(refreshing, {
      type: 'stream-established',
      storeId: 'store-b',
    });
    const superseded = sessionStep(established.state, {
      type: 'replacement-snapshot-reply',
      token: 2,
      storeId: 'store-b',
      result: {ok: false, error: ERRORS[0] ?? new Error(), reason: 'store-replaced'},
    });
    expect(superseded.requests).toEqual([{kind: 'recover-stream', mode: 'rebootstrap'}]);
    expect(superseded.state).toMatchObject({
      recovery: {kind: 'refreshing', intent: 'rebootstrap', phase: 'establishing'},
      replacement: {kind: 'none'},
    });
  });
});
