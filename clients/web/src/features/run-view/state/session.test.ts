import {describe, expect, test} from 'bun:test';
import {BackendClientError, type ControlChannelState} from '@vibesys/backend-client';
import {checkModel} from '../../../shared/testing/model.js';
import type {Seeded} from '../../../shared/testing/seeded.js';
import {
  type Reachability,
  SESSION_INITIAL,
  type SessionAction,
  type SessionRequest,
  type SessionState,
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

function randomAction(random: Seeded, issued: readonly number[]): SessionAction {
  const token = random.pick(issued);
  switch (random.int(13)) {
    case 0:
      return {type: 'start'};
    case 1:
      return {type: 'snapshot-reply', token, error: maybeError(random)};
    case 2:
      return {type: 'subscribe-reply', token, error: maybeError(random)};
    case 3:
      return {
        type: 'refresh-reply',
        token,
        outcome: random.bool()
          ? {kind: 'snapshot', error: maybeError(random)}
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

/** The token the slot a reply answers is waiting on, or null when it awaits none. */
function awaited(state: SessionState, action: SessionAction): number | null {
  if (state.kind === 'closed') return null;
  const {startup, recovery} = state;
  switch (action.type) {
    case 'snapshot-reply':
      return startup.kind === 'snapshot' ? startup.token : null;
    case 'subscribe-reply':
      return startup.kind === 'subscribing' ? startup.token : null;
    case 'refresh-reply':
      return recovery.kind === 'refreshing' ? recovery.token : null;
    default:
      return null;
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
      reply: (state, action) =>
        'token' in action ? {token: action.token, awaited: awaited(state, action)} : null,
      invariants: sessionInvariants,
      also: properties,
    });
  });

  test('starts with the snapshot, then the stream, and is connected once both succeed', () => {
    const asked = sessionStep(SESSION_INITIAL, {type: 'start'});
    expect(asked.requests).toEqual([{kind: 'load-snapshot', token: 1}]);
    const loaded = sessionStep(asked.state, {type: 'snapshot-reply', token: 1, error: null});
    expect(loaded.requests).toEqual([{kind: 'subscribe', token: 2}]);
    expect(loaded.state.view.hasSnapshot).toBe(true);
    const live = sessionStep(loaded.state, {type: 'subscribe-reply', token: 2, error: null});
    expect(live.state.view.status).toBe('connected');
  });

  test('a failed start-up snapshot keeps the page stale after the stream subscribes', () => {
    const failure = new Error('no snapshot');
    let state = sessionStep(SESSION_INITIAL, {type: 'start'}).state;
    state = sessionStep(state, {type: 'snapshot-reply', token: 1, error: failure}).state;
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
      outcome: {kind: 'snapshot', error: null},
    });
    expect(settled.requests).toEqual([{kind: 'recover-stream', mode: 'rebootstrap'}]);
  });
});
