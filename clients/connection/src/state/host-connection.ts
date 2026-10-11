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
 * the machine waits for carries an id and a deadline timer; an operation the machine stops waiting
 * for (it outlived its deadline, a channel ended under it, the connection closed) is aborted, and
 * its late outcome is ignored by its id, so no wait is orphaned and no abandoned operation acts.
 * Time, ids, and outcomes arrive as events; the shell (`../shell/host-connection.ts`) performs the
 * requests.
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

/** Where the machine is. A reconnecting phase's attempt is the state's (`phaseOf`). */
type MachinePhase =
  | {readonly kind: 'idle'}
  | {readonly kind: 'linking'}
  | {readonly kind: 'handshaking'}
  | {readonly kind: 'ready'}
  | {readonly kind: 'reconnecting'; readonly target: CheckTarget}
  | {readonly kind: 'offline'; readonly detail: string; readonly target: CheckTarget}
  | {readonly kind: 'auth-needed'; readonly detail: string}
  | {readonly kind: 'incompatible'; readonly detail: string}
  | {readonly kind: 'closed'};

/** A phase as the app reads it: a reconnecting phase says which attempt it is on. */
export type Phase =
  | Exclude<MachinePhase, {kind: 'reconnecting'}>
  | {readonly kind: 'reconnecting'; readonly attempt: number; readonly target: CheckTarget};

/** The operation the machine waits for. */
export type Operation =
  | {readonly kind: 'link'; readonly interactive: boolean}
  | {readonly kind: 'handshake'}
  | {readonly kind: 'ping'}
  | {readonly kind: 'check'; readonly target: CheckTarget};

type TimerPurpose = 'deadline' | 'backoff' | 'heartbeat';

export interface ConnectionState {
  readonly phase: MachinePhase;
  /** The operation in flight, or null; at most one. */
  readonly op: {readonly id: number; readonly operation: Operation} | null;
  /** The pending timer, or null; at most one. */
  readonly timer: {readonly id: number; readonly purpose: TimerPurpose} | null;
  /**
   * How many attempts (a link, or a check) this recovery has made; 0 once the connection is
   * proven. The failure of attempt `n` waits `delaysMs[n - 1]` before attempt `n + 1`, and goes
   * offline when the schedule has no such slot. A recovery keeps its count until a heartbeat (or a
   * fresh handshake) proves the connection held, since a check can pass while every dial fails; a
   * wake or the user's retry starts the count over.
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
  /** A run channel ended abnormally, or a dial to the run failed. */
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
  /** Stop operation `op`: the machine no longer waits for it, and ignores what it reports. */
  | {readonly type: 'abort'; readonly op: number}
  | {readonly type: 'schedule'; readonly timer: number; readonly delayMs: number}
  | {readonly type: 'cancel'; readonly timer: number}
  /** A check found the host reachable but the run gone (or closed to this user). */
  | {readonly type: 'run-unreachable'; readonly instanceId: string; readonly detail: string}
  /** Release every channel and the link: the connection is closed for good. */
  | {readonly type: 'release'};

export interface ConnectionConfig {
  /** Delays between failed attempts; finite, so the loop stops instead of storming. */
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
export function isStuck(phase: Phase | MachinePhase): boolean {
  return phase.kind === 'auth-needed' || phase.kind === 'incompatible';
}

/** The phase as the app reads it. */
export function phaseOf(state: ConnectionState): Phase {
  const {phase} = state;
  return phase.kind === 'reconnecting' ? {...phase, attempt: state.attempt} : phase;
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
        ? begin(clear(state), {kind: 'linking'}, link(false), config, 1)
        : same(state);
    case 'succeeded':
      return event.op === state.op?.id ? succeeded(state, config) : same(state);
    case 'failed':
      return event.op === state.op?.id
        ? failed(settle(state), event.cause, event.detail, config)
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

/** `state` with no operation and no timer, and the requests that end what it held. */
interface Cleared {
  readonly state: ConnectionState;
  readonly requests: readonly ConnectionRequest[];
}

function cancelTimer(state: ConnectionState): ConnectionRequest[] {
  return state.timer === null ? [] : [{type: 'cancel', timer: state.timer.id}];
}

/** Stop waiting: the operation in flight is aborted and the timer cancelled. */
function clear(state: ConnectionState): Cleared {
  const abort: ConnectionRequest[] = state.op === null ? [] : [{type: 'abort', op: state.op.id}];
  return {state: {...state, op: null, timer: null}, requests: [...abort, ...cancelTimer(state)]};
}

/** The operation in flight reported its outcome: it is over, and its deadline is cancelled. */
function settle(state: ConnectionState): Cleared {
  return {state: {...state, op: null, timer: null}, requests: cancelTimer(state)};
}

/** Enter `phase` and start `operation` under a deadline, as attempt `attempt`. */
function begin(
  cleared: Cleared,
  phase: MachinePhase,
  operation: Operation,
  config: ConnectionConfig,
  attempt = cleared.state.attempt,
): Step {
  const {state} = cleared;
  const op = state.nextId;
  const timer = op + 1;
  return {
    state: {
      ...state,
      phase,
      attempt,
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
  phase: MachinePhase,
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

function ready(cleared: Cleared, config: ConnectionConfig, attempt?: number): Step {
  return wait(cleared, {kind: 'ready'}, 'heartbeat', config.heartbeatMs, attempt);
}

function succeeded(state: ConnectionState, config: ConnectionConfig): Step {
  const operation = state.op?.operation;
  const cleared = settle(state);
  switch (operation?.kind) {
    case 'link':
      return begin(cleared, {kind: 'handshaking'}, {kind: 'handshake'}, config);
    case 'handshake':
    case 'ping':
      // A fresh handshake or an answered heartbeat proves the connection: the count starts over.
      return ready(cleared, config, 0);
    case 'check':
      return ready(cleared, config);
    case undefined:
      return same(state);
  }
}

function failed(cleared: Cleared, cause: Cause, detail: string, config: ConnectionConfig): Step {
  // The operation is gone; the phase still says what was being done.
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

function stuck(cleared: Cleared, phase: MachinePhase): Step {
  return {state: {...cleared.state, phase, attempt: 0}, requests: cleared.requests};
}

/** Attempt `attempt` failed: wait its slot before the next one, or go offline once it has none. */
function backoff(
  cleared: Cleared,
  target: CheckTarget,
  detail: string,
  config: ConnectionConfig,
): Step {
  const delayMs = config.delaysMs[cleared.state.attempt - 1];
  if (delayMs === undefined) {
    return {
      state: {...cleared.state, phase: {kind: 'offline', detail, target}},
      requests: cleared.requests,
    };
  }
  return wait(cleared, {kind: 'reconnecting', target}, 'backoff', delayMs);
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
  return begin(cleared, {kind: 'reconnecting', target}, {kind: 'check', target}, config, 1);
}

/**
 * A run channel ended, or a dial to the run failed. While ready, that is a lost channel to check.
 * Otherwise the machine is already recovering (its recovery ends in ready, which wakes every dial)
 * or waits for a wake or the user, so the event changes nothing; either way the host is not ready
 * after it, so a dial that failed is always woken by the next ready.
 */
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
  // The timer fired, so there is nothing to cancel.
  const fired: ConnectionState = {...state, timer: null};
  switch (purpose) {
    case 'deadline':
      // The operation outlived its deadline: it is aborted, and whatever it reports is ignored.
      return failed(clear(fired), 'link', 'the host did not answer in time', config);
    case 'heartbeat':
      return begin({state: fired, requests: []}, phase, {kind: 'ping'}, config);
    case 'backoff': {
      // Only a reconnecting phase holds a backoff timer (`invariants`).
      const target = phase.kind === 'reconnecting' ? phase.target : CONTROL;
      return begin(
        {state: fired, requests: []},
        phase,
        {kind: 'check', target},
        config,
        state.attempt + 1,
      );
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
    const {target} = phase;
    return begin(clear(state), {kind: 'reconnecting', target}, {kind: 'check', target}, config, 1);
  }
  return same(state);
}

function userRetry(state: ConnectionState, config: ConnectionConfig): Step {
  if (isStuck(state.phase)) {
    // The user's own action may prompt (a password, a host key): an interactive link.
    return begin(clear(state), {kind: 'linking'}, link(true), config, 1);
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

function sameTarget(left: CheckTarget, right: CheckTarget): boolean {
  return left.kind === 'control'
    ? right.kind === 'control'
    : right.kind === 'run' && left.instanceId === right.instanceId;
}

/** An operation in flight and a pending timer, by kind; null for none. */
interface Holding {
  readonly operation: Operation['kind'] | null;
  readonly purpose: TimerPurpose | null;
}

const NOTHING: readonly Holding[] = [{operation: null, purpose: null}];

/** What each phase may hold, exactly: the operation and timer that end its wait. */
const PHASE_HOLDS: Readonly<Record<MachinePhase['kind'], readonly Holding[]>> = {
  idle: NOTHING,
  linking: [{operation: 'link', purpose: 'deadline'}],
  handshaking: [{operation: 'handshake', purpose: 'deadline'}],
  // Waiting for the next heartbeat, or for its ping; never backing off.
  ready: [
    {operation: null, purpose: 'heartbeat'},
    {operation: 'ping', purpose: 'deadline'},
  ],
  // Backing off before the next check, or checking.
  reconnecting: [
    {operation: null, purpose: 'backoff'},
    {operation: 'check', purpose: 'deadline'},
  ],
  offline: NOTHING,
  'auth-needed': NOTHING,
  incompatible: NOTHING,
  closed: NOTHING,
};

/** What `state`'s phase must hold, and nothing else; a check's target is the phase's. */
function phaseWaits(state: ConnectionState): string[] {
  const {phase, op, timer} = state;
  const operation = op?.operation.kind ?? null;
  const purpose = timer?.purpose ?? null;
  const allowed = PHASE_HOLDS[phase.kind].some(
    holding => holding.operation === operation && holding.purpose === purpose,
  );
  const problems = allowed
    ? []
    : [`${phase.kind} holds ${operation ?? 'no operation'} and ${purpose ?? 'no timer'}`];
  const checked = op?.operation.kind === 'check' ? op.operation.target : null;
  if (phase.kind === 'reconnecting' && checked !== null && !sameTarget(checked, phase.target)) {
    problems.push(`a check of ${checked.kind} while reconnecting ${phase.target.kind}`);
  }
  return problems;
}

/** The attempt count each phase allows. */
function attemptProblems(state: ConnectionState, config: ConnectionConfig): string[] {
  const {phase, attempt} = state;
  const most = config.delaysMs.length + 1;
  if (attempt < 0 || attempt > most) return [`attempt ${attempt} outside 0..${most}`];
  const recovering = ['linking', 'handshaking', 'reconnecting', 'offline'].includes(phase.kind);
  if (recovering && attempt === 0) return [`${phase.kind} without an attempt`];
  const fresh =
    phase.kind === 'idle' || phase.kind === 'auth-needed' || phase.kind === 'incompatible';
  if (fresh && attempt !== 0) return [`${phase.kind} keeps attempt ${attempt}`];
  return [];
}

/**
 * What must hold after every step; the violations (empty when none). Each phase holds exactly the
 * operation and timer that end its wait (no orphan waits, no stray timers), every operation runs
 * under a deadline, and the attempt count fits the schedule.
 */
export function invariants(state: ConnectionState, config: ConnectionConfig): string[] {
  const {op, timer} = state;
  const problems = [...phaseWaits(state), ...attemptProblems(state, config)];
  if (op !== null && timer?.purpose !== 'deadline')
    problems.push('an operation without a deadline');
  if (timer?.purpose === 'deadline' && op === null)
    problems.push('a deadline without an operation');
  if (op !== null && timer !== null && timer.id !== op.id + 1) {
    problems.push('a deadline that belongs to no operation in flight');
  }
  const unissued = [op?.id, timer?.id].filter(id => id !== undefined && id >= state.nextId);
  return [...problems, ...unissued.map(id => `id ${id} not issued`)];
}
