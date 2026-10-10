/**
 * The connection supervisor: keeps one window's attachment to one run alive across SSH drops,
 * network changes, and sleep.
 *
 * There is exactly one retry loop per layer. The page's `WebSession` redials and resumes its own
 * streams (`PersistentEventStream`, reconcile); it cannot restore the link to the host under them.
 * The supervisor does only that: when a stream reports the link broke, the machine woke, or the
 * network changed, it runs one check (restore the host link, then confirm the run is still in the
 * host's registry and speaks this client's protocol), retrying a broken link with a finite backoff,
 * and on success tells the page to retry now (`wake`). It stops for good on what retrying cannot
 * fix: authentication the user must answer, a run that ended, a version mismatch, a broken command.
 *
 * `step` is the pure core (state and event in, state and requests out; no clock, no I/O). The shell
 * `ConnectionSupervisor` below runs its requests through injected functions and timers.
 */
import {DEFAULT_RECONNECT_DELAYS_MS, type ScheduleTimeout} from '@vibesys/backend-client';

/** Why a check failed: the closed set the supervisor decides on. */
export type CheckFailure =
  /** The host link is down; retrying may succeed. */
  | 'link'
  /** The host wants credentials the user has to give. */
  | 'auth'
  /** The run is no longer in the host's registry. */
  | 'run-gone'
  /** The run speaks another protocol version than this client. */
  | 'version-skew'
  /** Something retrying cannot fix (a missing vibesys command, a malformed listing). */
  | 'failed';

/** How a stream (or a dial) to the run ended, as the relay observed it. */
export type StreamEnd = 'normal' | 'link' | 'run-gone' | 'failed';

/** What the window shows about its connection. */
export type ConnectionStatus =
  | {readonly kind: 'connecting'}
  | {readonly kind: 'connected'}
  | {readonly kind: 'reconnecting'; readonly attempt: number}
  /** The backoff is spent; a wake, a network change, or the user retries. */
  | {readonly kind: 'offline'; readonly detail: string}
  | {readonly kind: 'auth-needed'; readonly detail: string}
  | {readonly kind: 'run-ended'}
  | {readonly kind: 'incompatible'; readonly detail: string}
  | {readonly kind: 'failed'; readonly detail: string};

export interface SupervisorState {
  readonly status: ConnectionStatus;
  /** Position in the backoff schedule for the next link failure. */
  readonly attempt: number;
  /** The check whose outcome the core is waiting for, or null. */
  readonly check: number | null;
  /** The pending retry or check-deadline timer, or null. */
  readonly timer: number | null;
  /** The next id for a check or timer; ids are never reused. */
  readonly nextId: number;
}

export type SupervisorEvent =
  | {readonly type: 'start'}
  | {readonly type: 'check-succeeded'; readonly check: number}
  | {
      readonly type: 'check-failed';
      readonly check: number;
      readonly cause: CheckFailure;
      readonly detail: string;
    }
  | {readonly type: 'stream-ended'; readonly end: StreamEnd}
  | {readonly type: 'timer-fired'; readonly timer: number}
  /** The machine woke from sleep (`powerMonitor` resume). */
  | {readonly type: 'resumed'}
  /** The network came back or changed. */
  | {readonly type: 'network-changed'}
  | {readonly type: 'user-retry'};

export type SupervisorRequest =
  /** Restore the host link and confirm the run; `interactive` may prompt the user. */
  | {readonly type: 'check'; readonly check: number; readonly interactive: boolean}
  | {readonly type: 'schedule'; readonly timer: number; readonly delayMs: number}
  | {readonly type: 'cancel'; readonly timer: number}
  /** Tell the page the host is reachable again, so its own session redials now. */
  | {readonly type: 'wake-page'}
  | {readonly type: 'show'; readonly status: ConnectionStatus};

export interface SupervisorConfig {
  /** Delays between link retries; finite, so the loop stops instead of storming. */
  readonly delaysMs: readonly number[];
  /** How long one check may take before it counts as a link failure. */
  readonly checkTimeoutMs: number;
}

const DEFAULT_SUPERVISOR_CONFIG: SupervisorConfig = {
  delaysMs: DEFAULT_RECONNECT_DELAYS_MS,
  checkTimeoutMs: 45_000,
};

export const INITIAL_STATE: SupervisorState = {
  status: {kind: 'connecting'},
  attempt: 0,
  check: null,
  timer: null,
  nextId: 1,
};

export interface Step {
  readonly state: SupervisorState;
  readonly requests: readonly SupervisorRequest[];
}

/** Statuses the supervisor leaves only on the user's retry. */
export function isTerminal(status: ConnectionStatus): boolean {
  return (
    status.kind === 'auth-needed' ||
    status.kind === 'run-ended' ||
    status.kind === 'incompatible' ||
    status.kind === 'failed'
  );
}

export function step(
  state: SupervisorState,
  event: SupervisorEvent,
  config: SupervisorConfig,
): Step {
  switch (event.type) {
    case 'start':
      return state.check === null && state.status.kind === 'connecting'
        ? startCheck({...state, attempt: 0}, true, config, [])
        : {state, requests: []};
    case 'user-retry':
      if (state.check !== null) return {state, requests: []};
      return startCheck({...state, attempt: 0, status: {kind: 'connecting'}}, true, config, []);
    case 'resumed':
    case 'network-changed':
      return wake(state, config);
    case 'stream-ended':
      return streamEnded(state, event.end, config);
    case 'timer-fired':
      return timerFired(state, event.timer, config);
    case 'check-succeeded':
      return checkSucceeded(state, event.check);
    case 'check-failed':
      return checkFailed(state, event, config);
  }
}

function startCheck(
  state: SupervisorState,
  interactive: boolean,
  config: SupervisorConfig,
  before: readonly SupervisorRequest[],
): Step {
  const check = state.nextId;
  const deadline = state.nextId + 1;
  const requests: SupervisorRequest[] = [...before];
  if (state.timer !== null) requests.push({type: 'cancel', timer: state.timer});
  requests.push(
    {type: 'check', check, interactive},
    {type: 'schedule', timer: deadline, delayMs: config.checkTimeoutMs},
  );
  return {state: {...state, check, timer: deadline, nextId: state.nextId + 2}, requests};
}

/** A wake or network change: check now, from the start of the backoff, unless that cannot help. */
function wake(state: SupervisorState, config: SupervisorConfig): Step {
  if (state.check !== null || isTerminal(state.status) || state.status.kind === 'connecting') {
    return {state, requests: []};
  }
  return startCheck({...state, attempt: 0}, false, config, []);
}

function streamEnded(state: SupervisorState, end: StreamEnd, config: SupervisorConfig): Step {
  // A normal end is the page's to handle; a failure needs a check only if nobody is on it yet.
  if (end === 'normal' || state.status.kind !== 'connected' || state.check !== null) {
    return {state, requests: []};
  }
  const reconnecting = {...state, status: {kind: 'reconnecting', attempt: 1} as const};
  return startCheck(reconnecting, false, config, [{type: 'show', status: reconnecting.status}]);
}

function timerFired(state: SupervisorState, timer: number, config: SupervisorConfig): Step {
  if (timer !== state.timer) return {state, requests: []};
  const cleared = {...state, timer: null};
  if (state.check !== null) {
    // The check outlived its deadline: whatever it reports later is ignored.
    return linkFailure({...cleared, check: null}, 'the host did not answer in time', config);
  }
  return startCheck(cleared, false, config, []);
}

function checkSucceeded(state: SupervisorState, check: number): Step {
  if (check !== state.check) return {state, requests: []};
  const recovered = state.status.kind !== 'connecting';
  const status: ConnectionStatus = {kind: 'connected'};
  const requests: SupervisorRequest[] = [];
  if (state.timer !== null) requests.push({type: 'cancel', timer: state.timer});
  requests.push({type: 'show', status});
  if (recovered) requests.push({type: 'wake-page'});
  return {state: {...state, status, attempt: 0, check: null, timer: null}, requests};
}

function checkFailed(
  state: SupervisorState,
  event: Extract<SupervisorEvent, {type: 'check-failed'}>,
  config: SupervisorConfig,
): Step {
  if (event.check !== state.check) return {state, requests: []};
  const requests: SupervisorRequest[] =
    state.timer === null ? [] : [{type: 'cancel', timer: state.timer}];
  const settled = {...state, check: null, timer: null};
  if (event.cause === 'link') {
    const next = linkFailure(settled, event.detail, config);
    return {state: next.state, requests: [...requests, ...next.requests]};
  }
  const status: ConnectionStatus =
    event.cause === 'auth'
      ? {kind: 'auth-needed', detail: event.detail}
      : event.cause === 'run-gone'
        ? {kind: 'run-ended'}
        : event.cause === 'version-skew'
          ? {kind: 'incompatible', detail: event.detail}
          : {kind: 'failed', detail: event.detail};
  return {state: {...settled, status}, requests: [...requests, {type: 'show', status}]};
}

/** Back off before the next check, or go offline once the schedule is spent. */
function linkFailure(state: SupervisorState, detail: string, config: SupervisorConfig): Step {
  const delayMs = config.delaysMs[state.attempt];
  if (delayMs === undefined) {
    const status: ConnectionStatus = {kind: 'offline', detail};
    return {state: {...state, status, timer: null}, requests: [{type: 'show', status}]};
  }
  const attempt = state.attempt + 1;
  const status: ConnectionStatus = {kind: 'reconnecting', attempt};
  const timer = state.nextId;
  return {
    state: {...state, status, attempt, timer, nextId: state.nextId + 1},
    requests: [
      {type: 'show', status},
      {type: 'schedule', timer, delayMs},
    ],
  };
}

/** What a check found. */
export type CheckOutcome =
  | {readonly ok: true}
  | {readonly ok: false; readonly cause: CheckFailure; readonly detail: string};

export interface SupervisorShellOptions {
  /** Restore the host link and confirm the run; never rejects. */
  readonly check: (interactive: boolean) => Promise<CheckOutcome>;
  readonly scheduleTimeout: ScheduleTimeout;
  readonly wakePage: () => void;
  readonly show: (status: ConnectionStatus) => void;
  readonly config?: SupervisorConfig;
}

/** The thin shell: feeds events to `step` and carries out its requests. */
export class ConnectionSupervisor {
  readonly #options: SupervisorShellOptions;
  readonly #config: SupervisorConfig;
  readonly #timers = new Map<number, () => void>();
  #state: SupervisorState = INITIAL_STATE;

  constructor(options: SupervisorShellOptions) {
    this.#options = options;
    this.#config = options.config ?? DEFAULT_SUPERVISOR_CONFIG;
  }

  get status(): ConnectionStatus {
    return this.#state.status;
  }

  dispatch(event: SupervisorEvent): void {
    const {state, requests} = step(this.#state, event, this.#config);
    this.#state = state;
    for (const request of requests) this.#run(request);
  }

  #run(request: SupervisorRequest): void {
    switch (request.type) {
      case 'check':
        void this.#options.check(request.interactive).then(outcome =>
          this.dispatch(
            outcome.ok
              ? {type: 'check-succeeded', check: request.check}
              : {
                  type: 'check-failed',
                  check: request.check,
                  cause: outcome.cause,
                  detail: outcome.detail,
                },
          ),
        );
        return;
      case 'schedule':
        this.#timers.set(
          request.timer,
          this.#options.scheduleTimeout(() => {
            this.#timers.delete(request.timer);
            this.dispatch({type: 'timer-fired', timer: request.timer});
          }, request.delayMs),
        );
        return;
      case 'cancel':
        this.#timers.get(request.timer)?.();
        this.#timers.delete(request.timer);
        return;
      case 'wake-page':
        this.#options.wakePage();
        return;
      case 'show':
        this.#options.show(request.status);
        return;
    }
  }
}
