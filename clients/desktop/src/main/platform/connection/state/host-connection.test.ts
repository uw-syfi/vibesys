import {describe, expect, test} from 'bun:test';
import {SEEDS, Seeded} from '@vibesys/core-state/testing';
import {
  type Cause,
  type ConnectionConfig,
  type ConnectionEvent,
  type ConnectionRequest,
  type ConnectionState,
  INITIAL,
  invariants,
  isStuck,
  type Step,
  step,
} from './host-connection.js';

const CONFIG: ConnectionConfig = {
  delaysMs: [500, 1_000, 2_000],
  deadlineMs: 20_000,
  heartbeatMs: 15_000,
};
const CAUSES: readonly Cause[] = [
  'link',
  'link',
  'link',
  'auth',
  'incompatible',
  'run-gone',
  'denied',
];

/** Fold `events` from `state`, returning the end state and every request. */
function fold(events: readonly ConnectionEvent[], state = INITIAL) {
  const requests: ConnectionRequest[] = [];
  for (const event of events) {
    const next = step(state, event, CONFIG);
    state = next.state;
    requests.push(...next.requests);
  }
  return {state, requests};
}

function opOf(state: ConnectionState): number {
  if (state.op === null) throw new Error('no operation in flight');
  return state.op.id;
}

/** One random event; ids are drawn from every id issued so far, so stale ones are frequent. */
function randomEvent(random: Seeded, state: ConnectionState): ConnectionEvent {
  const id = (): number =>
    random.bool() && state.op !== null
      ? state.op.id
      : random.bool() && state.timer !== null
        ? state.timer.id
        : random.int(state.nextId);
  switch (random.int(10)) {
    case 0:
      return {type: 'open'};
    case 1:
      return random.int(8) === 0 ? {type: 'close'} : {type: 'wake'};
    case 2:
    case 3:
      return {type: 'succeeded', op: id()};
    case 4:
      return {type: 'failed', op: id(), cause: random.pick(CAUSES), detail: 'why'};
    case 5:
    case 6:
      return {type: 'timer-fired', timer: id()};
    case 7:
      return {type: 'control-ended', cause: random.pick(CAUSES), detail: 'ended'};
    case 8:
      return {
        type: 'run-ended',
        instanceId: 'abcdefabcdef',
        cause: random.pick(CAUSES),
        detail: 'x',
      };
    default:
      return {type: 'user-retry'};
  }
}

/** The properties of one step, from `before` through `event`; `scheduled` holds every timer issued. */
function checkStep(
  where: object,
  before: ConnectionState,
  event: ConnectionEvent,
  next: Step,
  scheduled: Set<number>,
): void {
  const {state, requests} = next;
  expect({...where, problems: invariants(state, CONFIG)}).toEqual({...where, problems: []});
  // At most one operation starts per step, and a cancel names a timer that was scheduled.
  expect(requests.filter(request => request.type === 'perform').length).toBeLessThanOrEqual(1);
  for (const request of requests) {
    if (request.type === 'schedule') scheduled.add(request.timer);
  }
  const unknown = requests.filter(
    request => request.type === 'cancel' && !scheduled.has(request.timer),
  );
  expect({...where, unknown}).toEqual({...where, unknown: []});
  const absorbed = state === before && requests.length === 0;
  if (before.phase.kind === 'closed')
    expect({...where, absorbed}).toEqual({...where, absorbed: true});
  const retried = event.type === 'user-retry' || event.type === 'close';
  if (isStuck(before.phase) && !retried) {
    expect({...where, phase: state.phase}).toEqual({...where, phase: before.phase});
  }
}

/** Only failures and timers: a host that never answers. */
function unanswered(random: Seeded, state: ConnectionState): ConnectionEvent {
  if (state.op !== null && random.bool()) {
    return {type: 'failed', op: state.op.id, cause: 'link', detail: 'down'};
  }
  if (state.timer !== null) return {type: 'timer-fired', timer: state.timer.id};
  return {type: 'wake'};
}

describe('HostConnection machine (model-based)', () => {
  test('invariants hold, terminal phases absorb, and waits are never orphaned', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const opened = step(INITIAL, {type: 'open'}, CONFIG);
      let state = opened.state;
      const scheduled = new Set<number>(
        opened.requests.flatMap(request => (request.type === 'schedule' ? [request.timer] : [])),
      );
      for (let index = 0; index < 60; index += 1) {
        const event = randomEvent(random, state);
        const next = step(state, event, CONFIG);
        checkStep({seed, index, event: event.type}, state, event, next, scheduled);
        state = next.state;
      }
    }
  });

  test('a run of link failures is bounded by the schedule, then the host is offline', () => {
    for (const seed of SEEDS.slice(0, 50)) {
      const random = new Seeded(seed);
      let state = fold([{type: 'open'}]).state;
      let checks = 0;
      for (let index = 0; index < 100 && state.phase.kind !== 'offline'; index += 1) {
        const next = step(state, unanswered(random, state), CONFIG);
        checks += next.requests.filter(request => request.type === 'perform').length;
        state = next.state;
      }
      expect({seed, phase: state.phase.kind}).toEqual({seed, phase: 'offline'});
      expect(checks).toBeLessThanOrEqual(CONFIG.delaysMs.length + 1);
    }
  });
});

describe('HostConnection machine (scenarios)', () => {
  function readyState(): ConnectionState {
    let state = fold([{type: 'open'}]).state;
    state = fold([{type: 'succeeded', op: opOf(state)}], state).state;
    return fold([{type: 'succeeded', op: opOf(state)}], state).state;
  }

  test('opens through linking and handshaking to ready with a heartbeat', () => {
    const state = readyState();
    expect(state.phase.kind).toBe('ready');
    expect(state.timer?.purpose).toBe('heartbeat');
  });

  test('a run channel lost to the link is checked with a fresh run.connect to that run', () => {
    const {state, requests} = fold(
      [{type: 'run-ended', instanceId: 'abcdefabcdef', cause: 'link', detail: 'reset'}],
      readyState(),
    );
    expect(state.phase).toEqual({
      kind: 'reconnecting',
      attempt: 1,
      target: {kind: 'run', instanceId: 'abcdefabcdef'},
    });
    expect(requests).toContainEqual({
      type: 'perform',
      op: opOf(state),
      operation: {kind: 'check', target: {kind: 'run', instanceId: 'abcdefabcdef'}},
    });
  });

  test('the control channel is checked with a ping on a heartbeat', () => {
    const ready = readyState();
    const {requests} = fold([{type: 'timer-fired', timer: ready.timer?.id ?? -1}], ready);
    expect(
      requests.some(request => request.type === 'perform' && request.operation.kind === 'ping'),
    ).toBe(true);
  });

  test('a run found gone by its check leaves the host ready and reports the run', () => {
    const lost = fold(
      [{type: 'run-ended', instanceId: 'abcdefabcdef', cause: 'link', detail: 'x'}],
      readyState(),
    ).state;
    const {state, requests} = fold(
      [{type: 'failed', op: opOf(lost), cause: 'run-gone', detail: 'gone'}],
      lost,
    );
    expect(state.phase.kind).toBe('ready');
    expect(requests).toContainEqual({
      type: 'run-unreachable',
      instanceId: 'abcdefabcdef',
      detail: 'gone',
    });
  });

  test('auth stops the loop until the user retries, which links interactively', () => {
    const opened = fold([{type: 'open'}]).state;
    const stuck = fold(
      [{type: 'failed', op: opOf(opened), cause: 'auth', detail: 'denied'}],
      opened,
    ).state;
    expect(stuck.phase.kind).toBe('auth-needed');
    expect(fold([{type: 'wake'}], stuck).state).toBe(stuck);
    const {requests} = fold([{type: 'user-retry'}], stuck);
    expect(requests).toContainEqual(
      expect.objectContaining({type: 'perform', operation: {kind: 'link', interactive: true}}),
    );
  });

  test('closing releases everything once and absorbs later events', () => {
    const {state, requests} = fold([{type: 'close'}], readyState());
    expect(requests.filter(request => request.type === 'release')).toHaveLength(1);
    expect(fold([{type: 'open'}, {type: 'wake'}, {type: 'close'}], state).requests).toEqual([]);
  });
});
