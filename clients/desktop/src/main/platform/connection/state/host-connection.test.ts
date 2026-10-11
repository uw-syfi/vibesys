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
  phaseOf,
  type Step,
  step,
} from './host-connection.js';

const FIRST_DELAY = 500;
const CONFIG: ConnectionConfig = {
  delaysMs: [FIRST_DELAY, 1_000, 2_000],
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
const RUN = 'abcdefabcdef';

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

function timerOf(state: ConnectionState): number {
  if (state.timer === null) throw new Error('no timer pending');
  return state.timer.id;
}

function scheduledDelays(requests: readonly ConnectionRequest[]): number[] {
  return requests.flatMap(request => (request.type === 'schedule' ? [request.delayMs] : []));
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
      return {type: 'run-ended', instanceId: RUN, cause: random.pick(CAUSES), detail: 'x'};
    default:
      return {type: 'user-retry'};
  }
}

/**
 * The shell's view of what is in flight: every operation performed and not yet settled or aborted,
 * and every timer scheduled and not yet fired or cancelled. Nothing leaks when, after every step,
 * these are exactly what the state holds.
 */
class Ledger {
  readonly ops = new Set<number>();
  readonly timers = new Set<number>();

  /** Apply `event` (which the shell delivers) and `requests` (which it performs); the violations. */
  apply(before: ConnectionState, event: ConnectionEvent, requests: readonly ConnectionRequest[]) {
    const problems: string[] = [];
    // An outcome or a firing the machine accepted ends that operation or timer.
    if ((event.type === 'succeeded' || event.type === 'failed') && event.op === before.op?.id) {
      this.ops.delete(event.op);
    }
    if (event.type === 'timer-fired' && event.timer === before.timer?.id) {
      this.timers.delete(event.timer);
    }
    for (const request of requests) {
      switch (request.type) {
        case 'perform':
          this.ops.add(request.op);
          break;
        case 'abort':
          if (!this.ops.delete(request.op))
            problems.push(`abort of op ${request.op} not in flight`);
          break;
        case 'schedule':
          this.timers.add(request.timer);
          break;
        case 'cancel':
          if (!this.timers.delete(request.timer)) {
            problems.push(`cancel of timer ${request.timer} not pending`);
          }
          break;
        default:
          break;
      }
    }
    return problems;
  }

  /** What leaked: anything in flight that `state` no longer holds. */
  leaks(state: ConnectionState): {ops: number[]; timers: number[]} {
    return {
      ops: [...this.ops].filter(id => id !== state.op?.id),
      timers: [...this.timers].filter(id => id !== state.timer?.id),
    };
  }
}

/** The properties of one step from `before` through `event`. */
function checkStep(
  where: object,
  before: ConnectionState,
  event: ConnectionEvent,
  next: Step,
  ledger: Ledger,
): void {
  const {state, requests} = next;
  expect({...where, problems: invariants(state, CONFIG)}).toEqual({...where, problems: []});
  // At most one operation starts per step.
  expect(requests.filter(request => request.type === 'perform').length).toBeLessThanOrEqual(1);
  expect({...where, ledger: ledger.apply(before, event, requests)}).toEqual({...where, ledger: []});
  expect({...where, leaks: ledger.leaks(state)}).toEqual({...where, leaks: {ops: [], timers: []}});
  // The state holds nothing the shell is not running.
  if (state.op !== null) expect(ledger.ops.has(state.op.id)).toBe(true);
  if (state.timer !== null) expect(ledger.timers.has(state.timer.id)).toBe(true);
  const absorbed = state === before && requests.length === 0;
  if (before.phase.kind === 'closed') {
    expect({...where, absorbed}).toEqual({...where, absorbed: true});
  }
  const retried = event.type === 'user-retry' || event.type === 'close';
  if (isStuck(before.phase) && !retried) {
    expect({...where, phase: state.phase}).toEqual({...where, phase: before.phase});
  }
  // A run channel lost to the link leaves the host not ready, so the next ready wakes its dial.
  if (event.type === 'run-ended' && event.cause === 'link') {
    expect({...where, phase: state.phase.kind}).not.toEqual({...where, phase: 'ready'});
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
  test('invariants hold, terminal phases absorb, and nothing in flight leaks', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const ledger = new Ledger();
      let state = INITIAL;
      for (let index = 0; index < 80; index += 1) {
        const event = index === 0 ? ({type: 'open'} as const) : randomEvent(random, state);
        const next = step(state, event, CONFIG);
        checkStep({seed, index, event: event.type}, state, event, next, ledger);
        state = next.state;
      }
      // Closing ends everything still in flight.
      const closed = step(state, {type: 'close'}, CONFIG);
      checkStep({seed, event: 'close'}, state, {type: 'close'}, closed, ledger);
      expect({seed, ops: [...ledger.ops], timers: [...ledger.timers]}).toEqual({
        seed,
        ops: [],
        timers: [],
      });
    }
  });

  test('a run of link failures walks the whole schedule in order, then the host is offline', () => {
    for (const seed of SEEDS.slice(0, 50)) {
      const random = new Seeded(seed);
      let state = fold([{type: 'open'}]).state;
      let attempts = 1;
      const backoffs: number[] = [];
      for (let index = 0; index < 100 && state.phase.kind !== 'offline'; index += 1) {
        const before = state;
        const next = step(state, unanswered(random, state), CONFIG);
        attempts += next.requests.filter(request => request.type === 'perform').length;
        if (next.state.timer?.purpose === 'backoff' && next.state.timer !== before.timer) {
          backoffs.push(...scheduledDelays(next.requests));
        }
        state = next.state;
      }
      expect({seed, phase: state.phase.kind}).toEqual({seed, phase: 'offline'});
      expect({seed, backoffs}).toEqual({seed, backoffs: [...CONFIG.delaysMs]});
      expect(attempts).toBe(CONFIG.delaysMs.length + 1);
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
    expect(state.attempt).toBe(0);
  });

  test('a run channel lost to the link is checked with a fresh run.connect to that run', () => {
    const {state, requests} = fold(
      [{type: 'run-ended', instanceId: RUN, cause: 'link', detail: 'reset'}],
      readyState(),
    );
    expect(phaseOf(state)).toEqual({
      kind: 'reconnecting',
      attempt: 1,
      target: {kind: 'run', instanceId: RUN},
    });
    expect(requests).toContainEqual({
      type: 'perform',
      op: opOf(state),
      operation: {kind: 'check', target: {kind: 'run', instanceId: RUN}},
    });
  });

  test('regression: the first failed check after a drop or a wake waits the first slot', () => {
    const drops: ConnectionEvent[] = [
      {type: 'control-ended', cause: 'link', detail: 'reset'},
      {type: 'run-ended', instanceId: RUN, cause: 'link', detail: 'reset'},
    ];
    for (const drop of drops) {
      const checking = fold([drop], readyState()).state;
      const {requests} = fold(
        [{type: 'failed', op: opOf(checking), cause: 'link', detail: 'down'}],
        checking,
      );
      expect({drop: drop.type, delays: scheduledDelays(requests)}).toEqual({
        drop: drop.type,
        delays: [FIRST_DELAY],
      });
    }
    // Offline, then a wake: its check failing waits the first slot too.
    let state = fold([drops[0] as ConnectionEvent], readyState()).state;
    for (let index = 0; state.phase.kind !== 'offline' && index < 20; index += 1) {
      state = fold([unanswered(new Seeded(index), state)], state).state;
    }
    expect(state.phase.kind).toBe('offline');
    const woken = fold([{type: 'wake'}], state).state;
    expect(phaseOf(woken)).toMatchObject({kind: 'reconnecting', attempt: 1});
    const {requests} = fold(
      [{type: 'failed', op: opOf(woken), cause: 'link', detail: 'down'}],
      woken,
    );
    expect(scheduledDelays(requests)).toEqual([FIRST_DELAY]);
  });

  test('a recovery that did not hold backs off instead of checking at once', () => {
    let state = fold([{type: 'control-ended', cause: 'link', detail: 'x'}], readyState()).state;
    state = fold([{type: 'succeeded', op: opOf(state)}], state).state;
    expect(state.phase.kind).toBe('ready');
    const {state: lost, requests} = fold(
      [{type: 'control-ended', cause: 'link', detail: 'again'}],
      state,
    );
    expect(lost.op).toBeNull();
    expect(scheduledDelays(requests)).toEqual([FIRST_DELAY]);
    // A heartbeat that answers proves the connection: the next loss checks at once.
    let held = fold([{type: 'timer-fired', timer: timerOf(lost)}], lost).state;
    held = fold([{type: 'succeeded', op: opOf(held)}], held).state;
    held = fold([{type: 'timer-fired', timer: timerOf(held)}], held).state;
    held = fold([{type: 'succeeded', op: opOf(held)}], held).state;
    expect(held.attempt).toBe(0);
    const next = fold([{type: 'control-ended', cause: 'link', detail: 'x'}], held).state;
    expect(next.op?.operation.kind).toBe('check');
  });

  test('an operation past its deadline is aborted, and its late outcome is ignored', () => {
    const opened = fold([{type: 'open'}]).state;
    const op = opOf(opened);
    const {state, requests} = fold([{type: 'timer-fired', timer: timerOf(opened)}], opened);
    expect(requests).toContainEqual({type: 'abort', op});
    expect(fold([{type: 'succeeded', op}], state).state).toBe(state);
    expect(fold([{type: 'failed', op, cause: 'auth', detail: 'late'}], state).state).toBe(state);
  });

  test('a channel lost under a ping, and closing, abort the operation in flight', () => {
    const ready = readyState();
    const pinging = fold([{type: 'timer-fired', timer: timerOf(ready)}], ready).state;
    const ping = opOf(pinging);
    const lost = fold([{type: 'control-ended', cause: 'link', detail: 'x'}], pinging);
    expect(lost.requests).toContainEqual({type: 'abort', op: ping});
    const closed = fold([{type: 'close'}], lost.state);
    expect(closed.requests).toContainEqual({type: 'abort', op: opOf(lost.state)});
  });

  test('the control channel is checked with a ping on a heartbeat', () => {
    const ready = readyState();
    const {requests} = fold([{type: 'timer-fired', timer: timerOf(ready)}], ready);
    expect(
      requests.some(request => request.type === 'perform' && request.operation.kind === 'ping'),
    ).toBe(true);
  });

  test('a run found gone by its check leaves the host ready and reports the run', () => {
    const lost = fold(
      [{type: 'run-ended', instanceId: RUN, cause: 'link', detail: 'x'}],
      readyState(),
    ).state;
    const {state, requests} = fold(
      [{type: 'failed', op: opOf(lost), cause: 'run-gone', detail: 'gone'}],
      lost,
    );
    expect(state.phase.kind).toBe('ready');
    expect(requests).toContainEqual({type: 'run-unreachable', instanceId: RUN, detail: 'gone'});
  });

  test('a run dial lost while a recovery is under way is left to that recovery', () => {
    const checking = fold(
      [{type: 'control-ended', cause: 'link', detail: 'x'}],
      readyState(),
    ).state;
    const {state, requests} = fold(
      [{type: 'run-ended', instanceId: RUN, cause: 'link', detail: 'dial'}],
      checking,
    );
    expect(state).toBe(checking);
    expect(requests).toEqual([]);
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
