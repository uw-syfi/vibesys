/**
 * One host's connection as an XState machine (docs/contributing/clients-architecture.md, "State").
 *
 *   idle --open--> linking --link up--> handshaking --hello ok--> ready
 *   linking, handshaking: auth -> auth-needed; incompatible -> incompatible; link -> reconnecting
 *   ready --control or run channel ended (link), ping failed--> reconnecting(attempt)
 *   reconnecting --check ok--> ready; --check failed (link)--> backoff, then offline when spent
 *   offline --wake, network, user--> reconnecting;  auth-needed, incompatible --user--> linking
 *   linking, handshaking, ready, reconnecting --wake, user--> redo what the state waits on now
 *   any --close--> closed
 *
 * This is the app's one retry loop per host (`docs/contributing/desktop-connection.md`,
 * "Connection manager"). Its check is the operation that failed: the control channel is checked by
 * restoring it and `ping`, a run channel by a fresh `run.connect` to the same run.
 *
 * Every operation (link, handshake, ping, check) is an invoked actor the shell provides, and every
 * wait (its deadline, the heartbeat, a backoff slot) is a delayed transition on the clock the shell
 * injects. Leaving a state stops its operation (aborting its signal) and cancels its delays, so no
 * wait is orphaned and a stopped operation never reports. A wake (or the user's retry) redoes now
 * whatever the state waits on: an operation in flight is stopped and a fresh one of the same kind
 * replaces it rather than racing it (a check starts over from the first attempt), and a pending
 * heartbeat or backoff ends at once. Idle and the states only the user leaves hold nothing to redo.
 * An operation that opened a control channel returns it; the machine hands it to the shell
 * (`installControl`) only when it accepts the outcome, and back (`discard`) when it does not, so an
 * abandoned operation installs nothing.
 *
 * Transient states decide: `lost` (a ready connection lost a channel) and `failed` (an attempt
 * failed) route to auth-needed, incompatible, a backoff slot, or offline; `judging` tells a run
 * that is gone (reported with the emitted `run-unreachable`) from a host that is not reachable.
 */
import {unprovided} from '@vibesys/machine';
import {assign, emit, setup} from 'xstate';

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
  | 'denied'
  /**
   * The host refused a new channel: its session ceiling (sshd `MaxSessions`, or an equivalent local
   * limit) is already spent by this app's other channels to the same host. The host itself is fine;
   * retrying the same dial now would only repeat the refusal, so this is the caller's fact to show,
   * never a reason to back off and retry the link.
   */
  | 'capacity';

const CAUSES: ReadonlySet<string> = new Set<Cause>([
  'link',
  'auth',
  'incompatible',
  'run-gone',
  'denied',
  'capacity',
]);

/**
 * Which checkout problem made a host `incompatible`, from the bootstrap's own report: `missing` (no
 * VibeSys checkout at the path) or `too-old` (one that predates the helper).
 */
export type CheckoutProblem = 'missing' | 'too-old';

const CHECKOUT_PROBLEMS: ReadonlySet<string> = new Set<CheckoutProblem>(['missing', 'too-old']);

/** What a check proves: the control channel answers, or a run channel to `instanceId` connects. */
export type CheckTarget =
  | {readonly kind: 'control'}
  | {readonly kind: 'run'; readonly instanceId: string};

const CONTROL: CheckTarget = {kind: 'control'};

/** A phase as the app reads it: a reconnecting phase says which attempt it is on. */
export type Phase =
  | {readonly kind: 'idle'}
  | {readonly kind: 'linking'}
  | {readonly kind: 'handshaking'}
  | {readonly kind: 'ready'}
  | {readonly kind: 'reconnecting'; readonly attempt: number; readonly target: CheckTarget}
  | {readonly kind: 'offline'; readonly detail: string; readonly target: CheckTarget}
  | {readonly kind: 'auth-needed'; readonly detail: string}
  | {
      readonly kind: 'incompatible';
      readonly detail: string;
      /** The checkout problem the bootstrap reported, or null for another incompatibility. */
      readonly checkout: CheckoutProblem | null;
    }
  | {readonly kind: 'closed'};

export interface ConnectionConfig {
  /** Delays between failed attempts; finite, so the loop stops instead of storming. */
  readonly delaysMs: readonly number[];
  /** How long one operation may take before it counts as a link failure. */
  readonly deadlineMs: number;
  /** How often a ready connection pings its control channel. */
  readonly heartbeatMs: number;
}

export interface ConnectionContext {
  readonly config: ConnectionConfig;
  /**
   * How many attempts (a link, or a check) this recovery has made; 0 once the connection is
   * proven. The failure of attempt `n` waits `delaysMs[n - 1]` before attempt `n + 1`, and goes
   * offline when the schedule has no such slot. A recovery keeps its count until a heartbeat (or a
   * fresh handshake) proves the connection held, since a check can pass while every dial fails; a
   * wake or the user's retry starts the count over.
   */
  readonly attempt: number;
  /** What a check proves: the channel that was lost. */
  readonly target: CheckTarget;
  /** Whether the next link may prompt (the user's own retry). */
  readonly interactive: boolean;
  /** Why the last attempt or channel failed. */
  readonly cause: Cause;
  readonly detail: string;
  /** The checkout problem the last failure reported, if any. */
  readonly checkout: CheckoutProblem | null;
}

export type ConnectionEvent =
  | {readonly type: 'open'}
  | {readonly type: 'close'}
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

/** A check found the host reachable but the run gone (or closed to this user). */
export interface RunUnreachable {
  readonly type: 'run-unreachable';
  readonly instanceId: string;
  readonly detail: string;
}

/** A control channel an operation opened, opaque to the machine; null when the existing one held. */
export type Opened = object | null;

/** A failed operation's cause, detail and checkout problem, from what it rejected with. */
function failureOf(error: unknown): {
  cause: Cause;
  detail: string;
  checkout: CheckoutProblem | null;
} {
  if (typeof error === 'object' && error !== null) {
    const {reason, message, checkout} = error as {
      reason?: unknown;
      message?: unknown;
      checkout?: unknown;
    };
    const detail = typeof message === 'string' ? message : String(error);
    return {
      cause: typeof reason === 'string' && CAUSES.has(reason) ? (reason as Cause) : 'link',
      detail,
      checkout:
        typeof checkout === 'string' && CHECKOUT_PROBLEMS.has(checkout)
          ? (checkout as CheckoutProblem)
          : null,
    };
  }
  return {cause: 'link', detail: String(error), checkout: null};
}

const TIMEOUT_DETAIL = 'the host did not answer in time';
const STALE_DETAIL = 'the helper ended during the connection operation';

/** Enter a recovery from its first attempt. */
const FIRST_ATTEMPT = {attempt: 1} as const;

/**
 * A wake (or the user's retry) in a state whose operation is in flight: stop that stale operation
 * and start a fresh one of the same kind, as if the wait had just begun, with the attempt kept.
 */
function redo<const State extends string>(state: State) {
  return {target: state, reenter: true} as const;
}

export const connectionMachine = setup({
  types: {
    context: {} as ConnectionContext,
    events: {} as ConnectionEvent,
    input: {} as {readonly config: ConnectionConfig},
    emitted: {} as RunUnreachable,
  },
  actors: {
    link: unprovided<void, {interactive: boolean}>('link'),
    handshake: unprovided<Opened>('handshake'),
    ping: unprovided<void>('ping'),
    check: unprovided<Opened, {target: CheckTarget}>('check'),
  },
  actions: {
    /** Shell hook: everything the connection holds is released. */
    release: () => {},
    /** Shell hook: the control channel an accepted operation opened becomes the control channel. */
    installControl: (_, _params: {opened: Opened}) => {},
    /** Shell hook: an outcome the machine did not accept hands back what it opened. */
    discard: (_, _params: {opened: Opened}) => {},
    recordError: assign(({event}) => failureOf((event as {error?: unknown}).error)),
    recordTimeout: assign({cause: 'link' as Cause, detail: TIMEOUT_DETAIL, checkout: null}),
    recordStale: assign({cause: 'link' as Cause, detail: STALE_DETAIL, checkout: null}),
    startRecovery: assign(FIRST_ATTEMPT),
    resetAttempts: assign({attempt: 0}),
  },
  delays: {
    deadline: ({context}) => context.config.deadlineMs,
    heartbeat: ({context}) => context.config.heartbeatMs,
    backoff: ({context}) => context.config.delaysMs[context.attempt - 1] ?? 0,
  },
  guards: {
    /**
     * Shell hook: the outcome still holds when the machine accepts it (the channel the operation
     * opened or proved has not ended since). The pure machine accepts every outcome.
     */
    current: (_, _params: {opened: Opened | undefined}) => true,
    auth: ({context}) => context.cause === 'auth',
    incompatible: ({context}) => context.cause === 'incompatible',
    hasSlot: ({context}) => context.config.delaysMs[context.attempt - 1] !== undefined,
    recovering: ({context}) => context.attempt > 0,
    runUnreachable: ({context}) =>
      context.target.kind === 'run' && (context.cause === 'run-gone' || context.cause === 'denied'),
  },
}).createMachine({
  id: 'host',
  context: ({input}) => ({
    config: input.config,
    attempt: 0,
    target: CONTROL,
    interactive: false,
    cause: 'link',
    detail: '',
    checkout: null,
  }),
  initial: 'idle',
  on: {close: '.closed'},
  states: {
    idle: {
      on: {open: {target: 'linking', actions: assign({...FIRST_ATTEMPT, interactive: false})}},
    },
    linking: {
      entry: assign({target: CONTROL}),
      invoke: {
        id: 'link',
        src: 'link',
        input: ({context}) => ({interactive: context.interactive}),
        onDone: 'handshaking',
        onError: {target: 'failed', actions: 'recordError'},
      },
      after: {deadline: {target: 'failed', actions: 'recordTimeout'}},
      on: {wake: redo('linking'), 'user-retry': redo('linking')},
    },
    handshaking: {
      invoke: {
        id: 'handshake',
        src: 'handshake',
        onDone: [
          {
            guard: {type: 'current', params: ({event}) => ({opened: event.output})},
            target: 'ready',
            actions: [
              {type: 'installControl', params: ({event}) => ({opened: event.output})},
              'resetAttempts',
            ],
          },
          {
            target: 'failed',
            actions: [
              {type: 'discard', params: ({event}) => ({opened: event.output})},
              'recordStale',
            ],
          },
        ],
        onError: {target: 'failed', actions: 'recordError'},
      },
      after: {deadline: {target: 'failed', actions: 'recordTimeout'}},
      on: {wake: redo('handshaking'), 'user-retry': redo('handshaking')},
    },
    ready: {
      initial: 'waiting',
      on: {
        'control-ended': {
          target: 'lost',
          actions: assign({
            target: CONTROL,
            cause: ({event}) => event.cause,
            detail: ({event}) => event.detail,
            checkout: null,
          }),
        },
        'run-ended': {
          // The run going away, refusing this user, or a spent channel ceiling is the dial's own
          // fact, not the link's: none of them says the host is unreachable, so none should start a
          // reconnect that would only repeat the same dial and the same refusal.
          guard: ({event}) =>
            event.cause !== 'run-gone' && event.cause !== 'denied' && event.cause !== 'capacity',
          target: 'lost',
          actions: assign({
            target: ({event}) => ({kind: 'run', instanceId: event.instanceId}) as const,
            cause: ({event}) => event.cause,
            detail: ({event}) => event.detail,
            checkout: null,
          }),
        },
      },
      states: {
        waiting: {after: {heartbeat: 'pinging'}, on: {wake: 'pinging', 'user-retry': 'pinging'}},
        pinging: {
          entry: assign({target: CONTROL}),
          invoke: {
            id: 'ping',
            src: 'ping',
            onDone: [
              {
                guard: {type: 'current', params: () => ({opened: undefined})},
                target: 'waiting',
                reenter: true,
                actions: 'resetAttempts',
              },
              {target: '#host.lost', actions: 'recordStale'},
            ],
            onError: {target: '#host.lost', actions: 'recordError'},
          },
          after: {deadline: {target: '#host.lost', actions: 'recordTimeout'}},
          on: {wake: redo('pinging'), 'user-retry': redo('pinging')},
        },
      },
    },
    /** A ready connection lost a channel: stuck, back off, or check at once. */
    lost: {
      always: [
        {guard: 'auth', target: 'authNeeded'},
        {guard: 'incompatible', target: 'incompatible'},
        // The last recovery did not hold: this loss waits its slot, as a failed attempt.
        {guard: 'recovering', target: 'failed'},
        {target: 'reconnecting', actions: 'startRecovery'},
      ],
    },
    /** An attempt failed: stuck, back off, or offline once the schedule is spent. */
    failed: {
      always: [
        {guard: 'auth', target: 'authNeeded'},
        {guard: 'incompatible', target: 'incompatible'},
        {guard: 'hasSlot', target: 'reconnecting.backoff'},
        {target: 'offline'},
      ],
    },
    reconnecting: {
      initial: 'checking',
      states: {
        checking: {
          invoke: {
            id: 'check',
            src: 'check',
            input: ({context}) => ({target: context.target}),
            onDone: [
              {
                guard: {type: 'current', params: ({event}) => ({opened: event.output})},
                target: '#host.ready',
                actions: {type: 'installControl', params: ({event}) => ({opened: event.output})},
              },
              {
                target: '#host.failed',
                actions: [
                  {type: 'discard', params: ({event}) => ({opened: event.output})},
                  'recordStale',
                ],
              },
            ],
            onError: {target: 'judging', actions: 'recordError'},
          },
          after: {deadline: {target: '#host.failed', actions: 'recordTimeout'}},
          // A wake checks again from the start of the schedule.
          on: {
            wake: {target: 'checking', reenter: true, actions: 'startRecovery'},
            'user-retry': {target: 'checking', reenter: true, actions: 'startRecovery'},
          },
        },
        /** A check failed: only the run is unreachable (the host answered), or the attempt failed. */
        judging: {
          always: [
            {
              guard: 'runUnreachable',
              target: '#host.ready',
              actions: emit(({context}) => ({
                type: 'run-unreachable' as const,
                instanceId: context.target.kind === 'run' ? context.target.instanceId : '',
                detail: context.detail,
              })),
            },
            {target: '#host.failed'},
          ],
        },
        backoff: {
          after: {
            backoff: {
              target: 'checking',
              actions: assign({attempt: ({context}) => context.attempt + 1}),
            },
          },
          on: {
            wake: {target: 'checking', actions: 'startRecovery'},
            'user-retry': {target: 'checking', actions: 'startRecovery'},
          },
        },
      },
    },
    offline: {
      on: {
        wake: {target: 'reconnecting', actions: 'startRecovery'},
        'user-retry': {target: 'reconnecting', actions: 'startRecovery'},
      },
    },
    authNeeded: {
      entry: 'resetAttempts',
      // The user's own action may prompt (a password, a host key): an interactive link.
      on: {
        'user-retry': {target: 'linking', actions: assign({...FIRST_ATTEMPT, interactive: true})},
      },
    },
    incompatible: {
      entry: 'resetAttempts',
      on: {
        'user-retry': {target: 'linking', actions: assign({...FIRST_ATTEMPT, interactive: true})},
      },
    },
    closed: {type: 'final', entry: 'release'},
  },
});

/** What `phaseOf` reads of a snapshot. */
interface SnapshotView {
  readonly value: unknown;
  readonly context: ConnectionContext;
}

/** The top-level state `snapshot` is in. */
function topState(value: unknown): string {
  return typeof value === 'string' ? value : (Object.keys(value as object)[0] ?? '');
}

/** The phase as the app reads it. */
export function phaseOf({value, context}: SnapshotView): Phase {
  const top = topState(value);
  switch (top) {
    case 'idle':
    case 'linking':
    case 'handshaking':
    case 'ready':
    case 'closed':
      return {kind: top};
    case 'reconnecting':
      return {kind: 'reconnecting', attempt: context.attempt, target: context.target};
    case 'offline':
      return {kind: 'offline', detail: context.detail, target: context.target};
    case 'authNeeded':
      return {kind: 'auth-needed', detail: context.detail};
    case 'incompatible':
      return {kind: 'incompatible', detail: context.detail, checkout: context.checkout};
    default:
      throw new Error(`the connection machine has no phase for state ${top}`);
  }
}

/**
 * What must hold in every snapshot; the violations (empty when none): the attempt count fits the
 * schedule, a recovery has an attempt, and a phase that starts over has none.
 */
export function invariants(snapshot: SnapshotView): string[] {
  const phase = phaseOf(snapshot);
  const {attempt, config} = snapshot.context;
  const most = config.delaysMs.length + 1;
  if (attempt < 0 || attempt > most) return [`attempt ${attempt} outside 0..${most}`];
  const recovering = ['linking', 'handshaking', 'reconnecting', 'offline'].includes(phase.kind);
  if (recovering && attempt === 0) return [`${phase.kind} without an attempt`];
  const fresh =
    phase.kind === 'idle' || phase.kind === 'auth-needed' || phase.kind === 'incompatible';
  if (fresh && attempt !== 0) return [`${phase.kind} keeps attempt ${attempt}`];
  return [];
}
