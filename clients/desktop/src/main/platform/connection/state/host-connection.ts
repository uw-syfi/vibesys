/**
 * One host's connection as a pure machine: `step(state, event) -> {state, requests}`.
 *
 *   idle --open--> linking --link up--> handshaking --hello ok--> ready
 *   linking, handshaking: auth -> auth-needed; incompatible -> incompatible; link -> reconnecting
 *   ready --control or run channel ended (link), ping failed--> reconnecting(attempt)
 *   reconnecting --check ok--> ready; --check failed (link)--> backoff, then offline when spent
 *   offline --wake, network, user--> reconnecting;  auth-needed, incompatible --user--> linking
 *   any --close--> closed
 *
 * This is the app's one retry loop per host (`docs/contributing/desktop-connection.md`,
 * "Connection manager"). Its check is the operation that failed: the control channel is checked by
 * restoring it and `ping`, a run channel by a fresh `run.connect` to the same run. Every operation
 * the machine waits for carries an id and a deadline timer, so a late or duplicated outcome is
 * ignored and no wait is orphaned. Time, ids, and outcomes arrive as events; the shell
 * (`../shell/host-connection.ts`) performs the requests.
 */

/** Why an operation or a channel failed: the closed set the machine decides on. */
export type Cause =
  /** The link to the host is down or broke, or an operation outlived its deadline: retry. */
  | 'link'
  /** The host wants credentials only the user can give (or a host key not yet trusted). */
  | 'auth'
  /** The host's helper cannot serve this app: another protocol, an old or missing checkout. */
  | 'incompatible'
  /** The run is no longer there (`run_gone`). */
  | 'run-gone'
  /** The run is there but this user may not connect to it (`connect_denied`). */
  | 'denied';

/** What a check proves: the control channel answers, or a run channel to `instanceId` connects. */
type CheckTarget = {readonly kind: 'control'} | {readonly kind: 'run'; readonly instanceId: string};

export type Phase =
  | {readonly kind: 'idle'}
  | {readonly kind: 'linking'}
  | {readonly kind: 'handshaking'}
  | {readonly kind: 'ready'}
  | {readonly kind: 'reconnecting'; readonly attempt: number; readonly target: CheckTarget}
  | {readonly kind: 'offline'; readonly detail: string; readonly target: CheckTarget}
  | {readonly kind: 'auth-needed'; readonly detail: string}
  | {readonly kind: 'incompatible'; readonly detail: string}
  | {readonly kind: 'closed'};

/** The operation the machine waits for. */
export type Operation =
  | {readonly kind: 'link'; readonly interactive: boolean}
  | {readonly kind: 'handshake'}
  | {readonly kind: 'ping'}
  | {readonly kind: 'check'; readonly target: CheckTarget};

type TimerPurpose = 'deadline' | 'backoff' | 'heartbeat';

export interface ConnectionState {
  readonly phase: Phase;
  /** The operation in flight, or null; at most one. */
  readonly op: {readonly id: number; readonly operation: Operation} | null;
  /** The pending timer, or null; at most one. */
  readonly timer: {readonly id: number; readonly purpose: TimerPurpose} | null;
  /**
   * Position in the backoff schedule for the next failure. A recovery keeps its place until a
   * heartbeat proves the connection held (a check can pass while every dial fails); only that, a
   * wake, or the user's retry starts the schedule over.
   */
  readonly attempt: number;
  /** The next id for an operation or timer; ids are never reused. */
  readonly nextId: number;
}

export type ConnectionEvent =
  | {readonly type: 'open'}
  | {readonly type: 'close'}
  | {readonly type: 'succeeded'; readonly op: number}
  | {
      readonly type: 'failed';
      readonly op: number;
      readonly cause: Cause;
      readonly detail: string;
    }
  | {readonly type: 'timer-fired'; readonly timer: number}
  /** The control channel ended on its own. */
  | {readonly type: 'control-ended'; readonly cause: Cause; readonly detail: string}
  /** A run channel ended abnormally. */
  | {
      readonly type: 'run-ended';
      readonly instanceId: string;
      readonly cause: Cause;
      readonly detail: string;
    }
  /** The machine woke from sleep, or the network came back. */
  | {readonly type: 'wake'}
  /** The user asked to retry (or to sign in): the only way out of auth-needed and incompatible. */
  | {readonly type: 'user-retry'};

export type ConnectionRequest =
  | {readonly type: 'perform'; readonly op: number; readonly operation: Operation}
  | {readonly type: 'schedule'; readonly timer: number; readonly delayMs: number}
  | {readonly type: 'cancel'; readonly timer: number}
  /** A check found the host reachable but the run gone (or closed to this user). */
  | {readonly type: 'run-unreachable'; readonly instanceId: string; readonly detail: string}
  /** Release every channel and the link: the connection is closed for good. */
  | {readonly type: 'release'};

export interface ConnectionConfig {
  /** Delays between failed checks; finite, so the loop stops instead of storming. */
  readonly delaysMs: readonly number[];
  /** How long one operation may take before it counts as a link failure. */
  readonly deadlineMs: number;
  /** How often a ready connection pings its control channel. */
  readonly heartbeatMs: number;
}

export interface Step {
  readonly state: ConnectionState;
  readonly requests: readonly ConnectionRequest[];
}

export const INITIAL: ConnectionState = {
  phase: {kind: 'idle'},
  op: null,
  timer: null,
  attempt: 0,
  nextId: 1,
};

const CONTROL: CheckTarget = {kind: 'control'};

/** Phases left only by the user's retry or by closing. */
export function isStuck(phase: Phase): boolean {
  return phase.kind === 'auth-needed' || phase.kind === 'incompatible';
}

export function step(
  state: ConnectionState,
  event: ConnectionEvent,
  config: ConnectionConfig,
): Step {
  if (state.phase.kind === 'closed') return {state, requests: []};
  switch (event.type) {
    case 'close':
      return close(state);
    case 'open':
      return state.phase.kind === 'idle'
        ? begin(clear(state), {kind: 'linking'}, link(false), config)
        : same(state);
    case 'succeeded':
      return event.op === state.op?.id ? succeeded(state, config) : same(state);
    case 'failed':
      return event.op === state.op?.id
        ? failed(clear(state), event.cause, event.detail, config)
        : same(state);
    case 'timer-fired':
      return event.timer === state.timer?.id ? timerFired(state, config) : same(state);
    case 'control-ended':
      return state.phase.kind === 'ready'
        ? lost(clear(state), CONTROL, event.cause, event.detail, config)
        : same(state);
    case 'run-ended':
      return runEnded(state, event, config);
    case 'wake':
      return wake(state, config);
    case 'user-retry':
      return userRetry(state, config);
  }
}

function same(state: ConnectionState): Step {
  return {state, requests: []};
}

function link(interactive: boolean): Operation {
  return {kind: 'link', interactive};
}

/** `state` with its operation forgotten and its timer cancelled (the cancel is in `pending`). */
interface Cleared {
  readonly state: ConnectionState;
  readonly requests: readonly ConnectionRequest[];
}

function clear(state: ConnectionState): Cleared {
  const requests: ConnectionRequest[] =
    state.timer === null ? [] : [{type: 'cancel', timer: state.timer.id}];
  return {state: {...state, op: null, timer: null}, requests};
}

/** Enter `phase` and start `operation` under a deadline. */
function begin(
  cleared: Cleared,
  phase: Phase,
  operation: Operation,
  config: ConnectionConfig,
): Step {
  const {state} = cleared;
  const op = state.nextId;
  const timer = op + 1;
  return {
    state: {
      ...state,
      phase,
      op: {id: op, operation},
      timer: {id: timer, purpose: 'deadline'},
      nextId: op + 2,
    },
    requests: [
      ...cleared.requests,
      {type: 'perform', op, operation},
      {type: 'schedule', timer, delayMs: config.deadlineMs},
    ],
  };
}

/** Enter `phase` with only `purpose`'s timer pending. */
function wait(
  cleared: Cleared,
  phase: Phase,
  purpose: TimerPurpose,
  delayMs: number,
  attempt = cleared.state.attempt,
): Step {
  const {state} = cleared;
  const timer = state.nextId;
  return {
    state: {...state, phase, attempt, timer: {id: timer, purpose}, nextId: timer + 1},
    requests: [...cleared.requests, {type: 'schedule', timer, delayMs}],
  };
}

function ready(cleared: Cleared, config: ConnectionConfig): Step {
  return wait(cleared, {kind: 'ready'}, 'heartbeat', config.heartbeatMs);
}

function succeeded(state: ConnectionState, config: ConnectionConfig): Step {
  const operation = state.op?.operation;
  const cleared = clear(state);
  switch (operation?.kind) {
    case 'link':
      return begin(cleared, {kind: 'handshaking'}, {kind: 'handshake'}, config);
    case 'ping': {
      // A heartbeat answered: the connection held, so the next failure starts the schedule over.
      return ready({...cleared, state: {...cleared.state, attempt: 0}}, config);
    }
    case 'handshake':
    case 'check':
      return ready(cleared, config);
    case undefined:
      return same(state);
  }
}

function failed(cleared: Cleared, cause: Cause, detail: string, config: ConnectionConfig): Step {
  // `clear` already dropped the operation; the phase still says what was being done.
  const phase = cleared.state.phase;
  if (cause === 'auth') return stuck(cleared, {kind: 'auth-needed', detail});
  if (cause === 'incompatible') return stuck(cleared, {kind: 'incompatible', detail});
  // A heartbeat failed: the control channel is lost.
  if (phase.kind === 'ready') return lost(cleared, CONTROL, cause, detail, config);
  const unreachable = cause === 'run-gone' || cause === 'denied';
  if (phase.kind === 'reconnecting' && phase.target.kind === 'run' && unreachable) {
    // The host answered; only the run is unreachable. The link is fine: back to ready.
    const next = ready(cleared, config);
    return {
      state: next.state,
      requests: [
        ...next.requests,
        {type: 'run-unreachable', instanceId: phase.target.instanceId, detail},
      ],
    };
  }
  const target = phase.kind === 'reconnecting' || phase.kind === 'offline' ? phase.target : CONTROL;
  return backoff(cleared, target, detail, config);
}

function stuck(cleared: Cleared, phase: Phase): Step {
  return {state: {...cleared.state, phase}, requests: cleared.requests};
}

/** Wait before the next check, or go offline once the schedule is spent. */
function backoff(
  cleared: Cleared,
  target: CheckTarget,
  detail: string,
  config: ConnectionConfig,
): Step {
  const delayMs = config.delaysMs[cleared.state.attempt];
  if (delayMs === undefined) {
    return {
      state: {...cleared.state, phase: {kind: 'offline', detail, target}},
      requests: cleared.requests,
    };
  }
  const attempt = cleared.state.attempt + 1;
  return wait(cleared, {kind: 'reconnecting', attempt, target}, 'backoff', delayMs, attempt);
}

/** A ready connection lost a channel: check now, or back off when the last recovery did not hold. */
function lost(
  cleared: Cleared,
  target: CheckTarget,
  cause: Cause,
  detail: string,
  config: ConnectionConfig,
): Step {
  if (cause === 'auth' || cause === 'incompatible') return failed(cleared, cause, detail, config);
  if (cleared.state.attempt > 0) return backoff(cleared, target, detail, config);
  const attempt = 1;
  const withAttempt = {...cleared, state: {...cleared.state, attempt}};
  return begin(
    withAttempt,
    {kind: 'reconnecting', attempt, target},
    {kind: 'check', target},
    config,
  );
}

function runEnded(
  state: ConnectionState,
  event: Extract<ConnectionEvent, {type: 'run-ended'}>,
  config: ConnectionConfig,
): Step {
  // The run going away, or refusing this user, is the run's fact, not the link's.
  if (event.cause === 'run-gone' || event.cause === 'denied') return same(state);
  if (state.phase.kind !== 'ready') return same(state);
  return lost(
    clear(state),
    {kind: 'run', instanceId: event.instanceId},
    event.cause,
    event.detail,
    config,
  );
}

function timerFired(state: ConnectionState, config: ConnectionConfig): Step {
  const purpose = state.timer?.purpose;
  const phase = state.phase;
  const cleared: Cleared = {state: {...state, timer: null}, requests: []};
  switch (purpose) {
    case 'deadline':
      // The operation outlived its deadline: whatever it reports later is ignored.
      return failed(
        {state: {...cleared.state, op: null}, requests: []},
        'link',
        'the host did not answer in time',
        config,
      );
    case 'heartbeat':
      return begin(cleared, phase, {kind: 'ping'}, config);
    case 'backoff': {
      const target = phase.kind === 'reconnecting' ? phase.target : CONTROL;
      return begin(cleared, phase, {kind: 'check', target}, config);
    }
    case undefined:
      return same(state);
  }
}

/** A wake or network change: check now from the start of the schedule, if anything is waiting. */
function wake(state: ConnectionState, config: ConnectionConfig): Step {
  const phase = state.phase;
  if (phase.kind === 'ready' && state.op === null) {
    return begin(clear(state), phase, {kind: 'ping'}, config);
  }
  if ((phase.kind === 'reconnecting' && state.op === null) || phase.kind === 'offline') {
    const cleared = clear({...state, attempt: 1});
    return begin(
      cleared,
      {kind: 'reconnecting', attempt: 1, target: phase.target},
      {kind: 'check', target: phase.target},
      config,
    );
  }
  return same(state);
}

function userRetry(state: ConnectionState, config: ConnectionConfig): Step {
  const phase = state.phase;
  if (isStuck(phase)) {
    // The user's own action may prompt (a password, a host key): an interactive link.
    return begin(clear({...state, attempt: 0}), {kind: 'linking'}, link(true), config);
  }
  return wake(state, config);
}

function close(state: ConnectionState): Step {
  const cleared = clear(state);
  return {
    state: {...cleared.state, phase: {kind: 'closed'}},
    requests: [...cleared.requests, {type: 'release'}],
  };
}

/** The wait a phase must hold: its own operation, something, or nothing (it waits on no one). */
function phaseWaits(state: ConnectionState): string[] {
  const {phase, op, timer} = state;
  const waiting = op !== null || timer !== null;
  switch (phase.kind) {
    case 'linking':
      return op?.operation.kind === 'link' ? [] : ['linking without its operation'];
    case 'handshaking':
      return op?.operation.kind === 'handshake' ? [] : ['handshaking without its operation'];
    case 'ready':
    case 'reconnecting':
      return waiting ? [] : [`${phase.kind} waits on nothing`];
    case 'idle':
    case 'offline':
    case 'auth-needed':
    case 'incompatible':
    case 'closed':
      return waiting ? [`${phase.kind} holds an operation or timer`] : [];
  }
}

/**
 * What must hold after every step; the violations (empty when none). No orphan waits: every phase
 * that waits has the operation or timer that will end the wait.
 */
export function invariants(state: ConnectionState, config: ConnectionConfig): string[] {
  const {op, timer} = state;
  const problems = phaseWaits(state);
  if (op !== null && timer?.purpose !== 'deadline') {
    problems.push('an operation without a deadline');
  }
  if (timer?.purpose === 'deadline' && op === null)
    problems.push('a deadline without an operation');
  if (state.attempt > config.delaysMs.length) {
    problems.push(`attempt ${state.attempt} past the schedule`);
  }
  const unissued = [op?.id, timer?.id].filter(id => id !== undefined && id >= state.nextId);
  return [...problems, ...unissued.map(id => `id ${id} not issued`)];
}
