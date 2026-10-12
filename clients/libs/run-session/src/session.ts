/**
 * The live session's connection core as a machine: start-up (event stream, then a store-fenced
 * snapshot), what the stream and the control channel report, and the single-flight recovery that a
 * browser wake or an explicit Reattach starts. Control state, so a machine
 * (docs/contributing/clients-architecture.md, "State"); the shell (`web-session.ts`) provides its
 * operations and actions through the run connection.
 *
 * `open` has three parallel regions, each owning one invoked operation: `startup` the first
 * subscription's handshake, `recovery` the refresh (a control redial, then a snapshot), and
 * `replacement` the snapshot fenced to the stream store a batch established. Leaving a state stops
 * its operation, and a stopped operation never reports, so no reply is matched by a token. The
 * regions affect each other only through the internal `session.*` events they raise.
 *
 * Two orderings remain data. A snapshot is applied only when its load was issued after the newest
 * one applied or fenced (`issued`, `applied`), so a slow start-up snapshot cannot overwrite a newer
 * refresh's, and a stream replacement fences every load issued before it. And each stale cause is
 * numbered (`problemEpoch`): a snapshot clears only the causes that existed when its load began.
 *
 * A load reports success only for a snapshot the run fold would accept (the shell dry-runs the
 * fold), so a successful load is proof of the current run. Rejected projection evidence (a foreign
 * batch, or a snapshot rejected after its check) keeps the page stale through stream reconnects
 * until a later fenced snapshot succeeds; a transient connection fault never erases it.
 */

import type {RunSnapshot} from '@vibesys/contracts';
import {unprovided} from '@vibesys/machine';
import type {IterationNavigationState} from '@vibesys/run-state';
import type {ControlChannelState, StreamConnectionState} from '@vibesys/transport';
import {and, assign, not, raise, type SnapshotFrom, setup, stateIn} from 'xstate';
import type {WebSessionState, WebSessionStatus} from './connection.js';

/** How a recovery restarts the event stream: resume where it was, or a fresh bootstrap. */
export type StreamRecovery = 'retry' | 'rebootstrap';

/** Whether the page could reach anything now: a hidden tab or an offline browser waits. */
export interface Reachability {
  readonly visible: boolean;
  readonly online: boolean;
}

/** A snapshot load's result: the snapshot, or why it could not be loaded. */
export type SnapshotResult =
  | {readonly ok: true; readonly snapshot: RunSnapshot}
  | {
      readonly ok: false;
      readonly error: Error;
      /** The requested stream store was replaced before the server acquired the snapshot. */
      readonly reason?: 'store-replaced';
    };

/** A refresh redials the control channel, then reloads the snapshot (a retry) or not (a rebootstrap). */
export type RefreshOutcome =
  | {readonly kind: 'redial-failed'; readonly error: Error}
  | {readonly kind: 'ready'}
  | {readonly kind: 'snapshot'; readonly result: SnapshotResult};

/** A complete policy-navigation traversal, or no publishable tree for this response. */
export type IterationQueryOutcome = IterationNavigationState | null;

/** Which flights wait on a fenced snapshot load: they settle together when it does. */
type LoadOwner = 'startup' | 'startup-recovery' | 'recovery' | 'replacement';

type StreamProblem = 'none' | 'snapshot' | 'connection' | 'projection';

export type SessionEvent =
  | {readonly type: 'start'}
  /** A first or replacement batch established the store a snapshot can fence to. */
  | {readonly type: 'stream-established'; readonly storeId: string}
  /** A semantic event invalidated navigation without changing the stream's store. */
  | {readonly type: 'iterations-refresh'; readonly storeId: string}
  /** The event stream changed state. */
  | {readonly type: 'stream'; readonly state: StreamConnectionState}
  /** The stream reported a fault in band (a protocol error), or could not be recovered. */
  | {readonly type: 'fault'; readonly error: Error}
  /**
   * The run fold rejected data as belonging to another run: a stream batch, or a snapshot whose
   * identity changed between its load's check and its fold.
   */
  | {readonly type: 'projection-rejected'; readonly error: Error}
  /** The control channel changed state (the shell drops repeats of the same state). */
  | {readonly type: 'controls'; readonly state: ControlChannelState}
  /** The tab became visible or the network came back (or the machine woke). */
  | {readonly type: 'wake'; readonly reach: Reachability}
  /** The user asked to reload the run and restart the stream from a fresh bootstrap. */
  | {readonly type: 'reattach'; readonly reach: Reachability; readonly runEnded: boolean}
  | {readonly type: 'offline'; readonly runEnded: boolean}
  /** The user asked to redial the control channel now. */
  | {readonly type: 'reconnect-controls'}
  | {readonly type: 'close'};

/** What one region tells its siblings; raised inside the machine, never sent by the shell. */
type InternalEvent =
  /** A fenced load settled (not with `store-replaced`): its owners settle with it. */
  | {readonly type: 'session.snapshot-settled'; readonly owner: LoadOwner; readonly ok: boolean}
  /** A fenced load found its store replaced: a fresh generation must be established first. */
  | {readonly type: 'session.store-replaced'; readonly owner: LoadOwner}
  /** The stream dropped or faulted: a fresh stream still establishing has failed. */
  | {readonly type: 'session.stream-failed'};

export interface SessionContext {
  readonly view: WebSessionState;
  /** Whether the current stream generation has connection proof. */
  readonly streamConnected: boolean;
  /** Whether the newest fenced snapshot proof is usable. */
  readonly streamSnapshot: 'none' | 'trusted' | 'failed';
  /** Owner of the newest reason the stream is stale. */
  readonly streamProblem: StreamProblem;
  /** The number of stale causes recorded so far, which orders them against snapshot loads. */
  readonly problemEpoch: number;
  /** The issue number of the newest snapshot load. */
  readonly issued: number;
  /** The newest load applied or fenced; a load issued at or below it is never applied. */
  readonly applied: number;
  /** Start-up's snapshot, while `startup` is subscribing. */
  readonly startupSnapshot: 'waiting' | 'loaded' | 'failed';
  /** The recovery flight, while `recovery` is refreshing: monotone intent, monotone while owned. */
  readonly recovery: {
    readonly intent: StreamRecovery;
    /** The issue number of the refresh's snapshot load. */
    readonly issue: number;
    /** Stale causes visible when this recovery began. */
    readonly problemEpoch: number;
  };
  /** The fenced load, while `replacement` is loading. */
  readonly replacement: {
    readonly issue: number;
    readonly storeId: string;
    readonly owner: LoadOwner;
    /** Stale causes visible when this load began. */
    readonly problemEpoch: number;
  };
  /** The current navigation query and at most one latest-wins follow-up. */
  readonly iterations: {
    readonly currentStoreId: string | null;
    readonly loadingStoreId: string;
    readonly pendingStoreId: string | null;
  };
}

/** A settled snapshot load: which issue it was, what it returned, and the causes it saw. */
interface Settled {
  readonly issue: number;
  readonly result: SnapshotResult;
  readonly observedProblemEpoch: number;
}

const INITIAL_CONTEXT: SessionContext = {
  view: {
    hasSnapshot: false,
    status: 'connecting',
    error: null,
    iterationError: null,
    controls: {status: 'connected'},
  },
  streamConnected: false,
  streamSnapshot: 'none',
  streamProblem: 'none',
  problemEpoch: 0,
  issued: 0,
  applied: 0,
  startupSnapshot: 'waiting',
  recovery: {intent: 'retry', issue: 0, problemEpoch: 0},
  replacement: {issue: 0, storeId: '', owner: 'replacement', problemEpoch: 0},
  iterations: {currentStoreId: null, loadingStoreId: '', pendingStoreId: null},
};

const OPEN = '#session.open';

/**
 * A batch established `storeId`: unless the load in flight is for it already, (re)start the fenced
 * load, owned by the flights still waiting on a snapshot. On both of `replacement`'s states, as a
 * self-transition of `loading`: a re-entry declared on the region would exit all of `open`.
 */
const ESTABLISH = [
  {
    guard: and(['loadingStore', 'recovering']),
    actions: 'shareReplacementWithRecovery',
  },
  {guard: 'loadingStore'},
  {
    guard: 'startupRecovering',
    target: 'loading',
    reenter: true,
    actions: {type: 'issueReplacement', params: {owner: 'startup-recovery'}},
  },
  {
    guard: 'startupWaiting',
    target: 'loading',
    reenter: true,
    actions: {type: 'issueReplacement', params: {owner: 'startup'}},
  },
  {
    guard: 'recovering',
    target: 'loading',
    reenter: true,
    actions: {type: 'issueReplacement', params: {owner: 'recovery'}},
  },
  {
    target: 'loading',
    reenter: true,
    actions: {type: 'issueReplacement', params: {owner: 'replacement'}},
  },
] as const;

export function toError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error));
}

/** The context with the stream's status (and its error) set; the same context when unchanged. */
function withStatus(
  context: SessionContext,
  status: WebSessionStatus,
  error: Error | null,
): SessionContext {
  if (context.view.status === status && context.view.error === error) return context;
  return {...context, view: {...context.view, status, error}};
}

/** Record a new stale cause so older asynchronous evidence cannot erase it. */
function withProblem(
  context: SessionContext,
  problem: Exclude<StreamProblem, 'none'>,
  error: Error,
): SessionContext {
  return withStatus(
    {...context, streamProblem: problem, problemEpoch: context.problemEpoch + 1},
    'stale',
    error,
  );
}

/** Add a transient connection fault without erasing stronger snapshot or projection evidence. */
function connectionFailed(context: SessionContext, error: Error): SessionContext {
  return context.streamProblem === 'snapshot' || context.streamProblem === 'projection'
    ? context
    : withProblem(context, 'connection', error);
}

/** The stream lost its connection: stale with `error`, unless a stronger cause already owns it. */
function streamLost(context: SessionContext, error: Error): SessionContext {
  return connectionFailed({...context, streamConnected: false}, error);
}

/** The snapshot a settled load applies: a loaded one issued after the newest applied, else null. */
function applies(context: SessionContext, settled: Settled | null): RunSnapshot | null {
  if (settled === null) return null;
  const {issue, result} = settled;
  return result.ok && issue > context.applied ? result.snapshot : null;
}

/**
 * A load settled: on success the snapshot is trusted unless a newer one already was; on failure
 * the page goes stale. A load issued at or below `applied` was retired before it settled, by a
 * proven replacement or a newer snapshot, so its failure is dropped as much as its success.
 */
function snapshotSettled(context: SessionContext, settled: Settled): SessionContext {
  const {issue, result, observedProblemEpoch} = settled;
  if (issue <= context.applied) return context;
  if (!result.ok) {
    return withProblem({...context, streamSnapshot: 'failed'}, 'snapshot', result.error);
  }
  const shown = context.view.hasSnapshot
    ? context
    : {...context, view: {...context.view, hasSnapshot: true}};
  // This response may retire only the stale evidence that existed when its query began. A stream
  // close or protocol fault reported afterward owns the view until newer connection evidence.
  const supersedesProblem =
    shown.streamProblem !== 'none' && shown.problemEpoch <= observedProblemEpoch;
  const trusted: SessionContext = {
    ...shown,
    streamSnapshot: 'trusted',
    streamProblem: supersedesProblem ? 'none' : shown.streamProblem,
    applied: issue,
  };
  return trusted.streamProblem === 'none' && trusted.streamConnected
    ? withStatus(trusted, 'connected', null)
    : trusted;
}

/** The stream reported: connected (which may clear a connection problem), or dropped. */
function streamReported(context: SessionContext, state: StreamConnectionState): SessionContext {
  // Every drop is reported, on an ended run too: `connectionBanners` decides what to show, and
  // suppressing it here left an ended run's empty transcript unexplained (#1044).
  if (state.status !== 'connected') return streamLost(context, state.error);
  const connected = context.streamConnected ? context : {...context, streamConnected: true};
  // Only connection evidence retires a connection problem: a live socket does not make a failed
  // snapshot or rejected foreign-run data trustworthy.
  return context.streamSnapshot === 'trusted' &&
    (context.streamProblem === 'none' || context.streamProblem === 'connection')
    ? withStatus({...connected, streamProblem: 'none'}, 'connected', null)
    : connected;
}

const reachable = (reach: Reachability): boolean => reach.visible && reach.online;

/** A healthy ended stream stays inert; a stale one can always ask for a fresh bootstrap. */
function mayReattach(
  context: SessionContext,
  event: Extract<SessionEvent, {type: 'reattach'}>,
): boolean {
  return reachable(event.reach) && (context.view.status === 'stale' || !event.runEnded);
}

/** Whether a fenced load's owner includes the recovery flight. */
const recoveryOwns = (owner: LoadOwner): boolean =>
  owner === 'recovery' || owner === 'startup-recovery';

/** Whether a fenced load's owner includes start-up. */
const startupOwns = (owner: LoadOwner): boolean =>
  owner === 'startup' || owner === 'startup-recovery';

/** A load whose store was replaced before the server acquired its snapshot. */
const storeReplaced = (result: SnapshotResult): boolean =>
  !result.ok && result.reason === 'store-replaced';

/** The stream recovery a settled refresh asks for: a fresh bootstrap first, or a retry. */
function refreshEstablishes(context: SessionContext, outcome: RefreshOutcome): boolean {
  return (
    context.recovery.intent === 'rebootstrap' ||
    outcome.kind === 'ready' ||
    (outcome.kind === 'snapshot' && storeReplaced(outcome.result))
  );
}

/** The refresh's snapshot as a settled load; null for a refresh that loaded none. */
function refreshSettled(context: SessionContext, outcome: RefreshOutcome): Settled | null {
  if (outcome.kind !== 'snapshot') return null;
  return {
    issue: context.recovery.issue,
    result: outcome.result,
    observedProblemEpoch: context.recovery.problemEpoch,
  };
}

/** The fenced load as a settled load. */
function replacementSettled(context: SessionContext, result: SnapshotResult): Settled {
  return {
    issue: context.replacement.issue,
    result,
    observedProblemEpoch: context.replacement.problemEpoch,
  };
}

/** Issue a fenced load for `storeId`: every earlier load began before this store was proven. */
function issueReplacement(
  context: SessionContext,
  storeId: string,
  owner: LoadOwner,
): Partial<SessionContext> {
  const issue = context.issued + 1;
  return {
    issued: issue,
    applied: Math.max(context.applied, issue - 1),
    replacement: {issue, storeId, owner, problemEpoch: context.problemEpoch},
  };
}

/** Issue a refresh with `intent`. */
function issueRefresh(context: SessionContext, intent: StreamRecovery): Partial<SessionContext> {
  const issue = context.issued + 1;
  return {issued: issue, recovery: {intent, issue, problemEpoch: context.problemEpoch}};
}

/** Start-up's handshake succeeded after its snapshot settled: connected if both are good. */
function startupSubscribed(context: SessionContext): SessionContext {
  const connected = {...context, streamConnected: true};
  return context.startupSnapshot === 'loaded' &&
    context.streamSnapshot === 'trusted' &&
    context.streamProblem === 'none'
    ? withStatus(connected, 'connected', null)
    : connected;
}

/** Start-up's snapshot settled after its handshake: connected if the stream is too. */
function startupSnapshotLoaded(context: SessionContext, ok: boolean): SessionContext {
  return ok && context.streamConnected && context.streamProblem === 'none'
    ? withStatus(context, 'connected', null)
    : context;
}

/** The store named by an event that requests policy navigation. */
function iterationStoreId(event: SessionEvent | InternalEvent): string | null {
  return event.type === 'iterations-refresh' ? event.storeId : null;
}

/** Start a navigation query for one stream-store generation. */
function beginIterationQuery(storeId: string): Partial<SessionContext> {
  return {
    iterations: {
      currentStoreId: storeId,
      loadingStoreId: storeId,
      pendingStoreId: null,
    },
  };
}

/** Retain exactly one latest-wins query behind the operation already in flight. */
function queueIterationQuery(context: SessionContext, storeId: string): Partial<SessionContext> {
  return {
    iterations: {
      ...context.iterations,
      currentStoreId: storeId,
      pendingStoreId: storeId,
    },
  };
}

/** Promote the coalesced follow-up after its predecessor physically settles. */
function promoteIterationQuery(context: SessionContext): Partial<SessionContext> {
  const storeId = context.iterations.pendingStoreId;
  if (storeId === null) return {};
  return {
    iterations: {
      currentStoreId: storeId,
      loadingStoreId: storeId,
      pendingStoreId: null,
    },
  };
}

export const sessionMachine = setup({
  types: {
    context: {} as SessionContext,
    events: {} as SessionEvent | InternalEvent,
  },
  actors: {
    /** Subscribe the event stream; settles when the first subscription's handshake does. */
    subscribe: unprovided<void>('subscribe'),
    /** Redial the control channel, then load the snapshot unless `mode` rebootstraps. */
    refresh: unprovided<RefreshOutcome, {readonly mode: StreamRecovery}>('refresh'),
    /** Load the snapshot fenced to the stream store `storeId`. */
    loadReplacementSnapshot: unprovided<SnapshotResult, {readonly storeId: string}>(
      'loadReplacementSnapshot',
    ),
    /** Load and normalize the complete iteration tree fenced to `storeId`. */
    loadIterations: unprovided<IterationQueryOutcome, {readonly storeId: string}>('loadIterations'),
  },
  actions: {
    /**
     * Fold `snapshot` into the run; null (an older or a failed load) does nothing. The load already
     * reported a snapshot of another run as failed; one the fold still rejects comes back as
     * `projection-rejected`.
     */
    applySnapshot: (_, _params: {readonly snapshot: RunSnapshot | null}) => undefined,
    /** Restart the stream; a failure comes back as a `fault`. */
    recoverStream: (_, _params: {readonly mode: StreamRecovery}) => undefined,
    /** Retry the stream if the applied snapshot is ahead of the event fold. */
    reevaluateStream: (_, _params: {readonly applied: boolean}) => undefined,
    /** Redial the control channel now; a failure comes back as a `fault`. */
    redialControls: () => undefined,
    /** Release the connection and the page's listeners. */
    closeShell: () => undefined,
    /** Publish one complete navigation traversal into the run's data fold. */
    publishIterations: (_, _params: {readonly navigation: IterationNavigationState | null}) =>
      undefined,
    /** A load settled (null: none did): the view, and the newest load applied. */
    settleSnapshot: assign(({context}, settled: Settled | null) =>
      settled === null ? context : snapshotSettled(context, settled),
    ),
    /** Issue the fenced load for the store the event established, owned by `owner`. */
    issueReplacement: assign(({context, event}, {owner}: {readonly owner: LoadOwner}) =>
      event.type === 'stream-established' ? issueReplacement(context, event.storeId, owner) : {},
    ),
    /** Let a recovery await the same-store fenced load instead of starting a duplicate request. */
    shareReplacementWithRecovery: assign(({context}) => ({
      replacement: {
        ...context.replacement,
        owner: startupOwns(context.replacement.owner) ? 'startup-recovery' : 'recovery',
      },
    })),
    beginIterationQuery: assign(({event}) => {
      const storeId = iterationStoreId(event);
      return storeId === null ? {} : beginIterationQuery(storeId);
    }),
    beginCurrentIterationQuery: assign(({context}) => {
      const storeId = context.iterations.currentStoreId;
      return storeId === null ? {} : beginIterationQuery(storeId);
    }),
    queueIterationQuery: assign(({context, event}) => {
      const storeId = iterationStoreId(event);
      return storeId === null ? {} : queueIterationQuery(context, storeId);
    }),
    queueCurrentIterationQuery: assign(({context}) => {
      const storeId = context.iterations.currentStoreId;
      return storeId === null ? {} : queueIterationQuery(context, storeId);
    }),
    promoteIterationQuery: assign(({context}) => promoteIterationQuery(context)),
    clearIterationError: assign(({context}) =>
      context.view.iterationError === null ? {} : {view: {...context.view, iterationError: null}},
    ),
    reportIterationError: assign(({context}, {error}: {readonly error: Error}) => ({
      view: {...context.view, iterationError: error},
    })),
  },
  guards: {
    /** The fenced load in flight is for this store already. */
    loadingStore: and([
      stateIn(`${OPEN}.replacement.loading`),
      ({context, event}) =>
        event.type === 'stream-established' && context.replacement.storeId === event.storeId,
    ]),
    /** Start-up subscribed or subscribing, and its snapshot is still to come. */
    startupWaiting: and([
      stateIn(`${OPEN}.startup.subscribing`),
      ({context}) => context.startupSnapshot === 'waiting',
    ]),
    recovering: stateIn(`${OPEN}.recovery.refreshing`),
    startupRecovering: and([
      stateIn(`${OPEN}.startup.subscribing`),
      ({context}) => context.startupSnapshot === 'waiting',
      stateIn(`${OPEN}.recovery.refreshing`),
    ]),
    iterationRefreshPending: ({context}) => context.iterations.pendingStoreId !== null,
    canRefreshIterations: ({context, event}) =>
      event.type === 'wake' && reachable(event.reach) && context.iterations.currentStoreId !== null,
  },
}).createMachine({
  id: 'session',
  context: INITIAL_CONTEXT,
  initial: 'open',
  states: {
    open: {
      type: 'parallel',
      on: {
        close: {target: 'closed', actions: 'closeShell'},
        stream: [
          {
            guard: ({event}) => event.state.status === 'connected',
            actions: assign(({context, event}) => streamReported(context, event.state)),
          },
          {
            actions: [
              assign(({context, event}) => streamReported(context, event.state)),
              raise({type: 'session.stream-failed'}),
            ],
          },
        ],
        fault: {
          actions: [
            assign(({context, event}) => streamLost(context, event.error)),
            raise({type: 'session.stream-failed'}),
          ],
        },
        // Only a later fenced snapshot, issued after this cause, can retire it.
        'projection-rejected': {
          actions: assign(({context, event}) => withProblem(context, 'projection', event.error)),
        },
        controls: {
          actions: assign(({context, event}) => ({view: {...context.view, controls: event.state}})),
        },
        offline: {
          // Offline is this browser's reachability: it cannot make an ended run's complete
          // transcript incomplete.
          guard: ({event}) => !event.runEnded,
          actions: assign(({context}) => streamLost(context, new Error('Network is offline'))),
        },
        // Not a refresh: a channel that reported a drop has a redial armed already, and only the
        // transport's `reconnect` cancels that backoff and dials now.
        'reconnect-controls': {actions: 'redialControls'},
      },
      states: {
        startup: {
          initial: 'idle',
          states: {
            idle: {on: {start: {target: 'subscribing'}}},
            subscribing: {
              initial: 'handshake',
              states: {
                handshake: {
                  invoke: {
                    id: 'subscribe',
                    src: 'subscribe',
                    onDone: [
                      {
                        guard: ({context}) => context.startupSnapshot === 'waiting',
                        target: 'subscribed',
                        actions: assign({streamConnected: true}),
                      },
                      {
                        target: `${OPEN}.startup.done`,
                        actions: assign(({context}) => startupSubscribed(context)),
                      },
                    ],
                    onError: {
                      target: `${OPEN}.startup.done`,
                      actions: [
                        assign(({context, event}) =>
                          withProblem(
                            {...context, streamConnected: false},
                            'connection',
                            toError(event.error),
                          ),
                        ),
                        raise({type: 'session.stream-failed'}),
                      ],
                    },
                  },
                  on: {
                    'session.snapshot-settled': {
                      guard: ({event}) => event.ok || startupOwns(event.owner),
                      actions: assign(({event}) => ({
                        startupSnapshot: event.ok ? ('loaded' as const) : ('failed' as const),
                      })),
                    },
                  },
                },
                subscribed: {
                  on: {
                    'session.snapshot-settled': {
                      guard: ({event}) => event.ok || startupOwns(event.owner),
                      target: `${OPEN}.startup.done`,
                      actions: assign(({context, event}) =>
                        startupSnapshotLoaded(
                          {...context, startupSnapshot: event.ok ? 'loaded' : 'failed'},
                          event.ok,
                        ),
                      ),
                    },
                  },
                },
              },
            },
            done: {},
          },
        },
        recovery: {
          initial: 'none',
          states: {
            none: {
              on: {
                wake: {
                  guard: ({event}) => reachable(event.reach),
                  target: 'refreshing',
                  actions: assign(({context}) => issueRefresh(context, 'retry')),
                },
                reattach: {
                  guard: ({context, event}) => mayReattach(context, event),
                  target: 'refreshing',
                  actions: assign(({context}) => issueRefresh(context, 'rebootstrap')),
                },
                'session.store-replaced': {
                  guard: ({event}) => !startupOwns(event.owner),
                  target: 'refreshing.establishing',
                  actions: assign(({context}) => ({
                    recovery: {
                      intent: 'rebootstrap' as const,
                      issue: context.replacement.issue,
                      problemEpoch: context.problemEpoch,
                    },
                  })),
                },
              },
            },
            refreshing: {
              initial: 'preparing',
              on: {
                // Single flight, monotone intent: a Reattach upgrades the flight in progress.
                reattach: {
                  guard: ({context, event}) =>
                    mayReattach(context, event) && context.recovery.intent !== 'rebootstrap',
                  actions: assign(({context}) => ({
                    recovery: {...context.recovery, intent: 'rebootstrap' as const},
                  })),
                },
                // A proven store hands this flight to its fenced snapshot; a refresh reply that
                // began before the replacement is dropped by leaving `preparing`.
                'stream-established': {guard: not('loadingStore'), target: '.snapshot'},
                // A recovery that supplied the fenced generation retires with it, so the next wake
                // or Reattach can start another.
                'session.snapshot-settled': {
                  guard: ({event}) => recoveryOwns(event.owner),
                  target: 'none',
                },
                'session.store-replaced': {
                  guard: ({event}) => !startupOwns(event.owner),
                  target: '.establishing',
                  actions: assign(({context}) => ({
                    recovery: {...context.recovery, intent: 'rebootstrap' as const},
                  })),
                },
              },
              states: {
                preparing: {
                  invoke: {
                    id: 'refresh',
                    src: 'refresh',
                    input: ({context}) => ({mode: context.recovery.intent}),
                    onDone: [
                      {
                        guard: ({event}) => event.output.kind === 'redial-failed',
                        target: '#session.open.recovery.none',
                        actions: assign(({context, event}) =>
                          event.output.kind === 'redial-failed'
                            ? withProblem(context, 'connection', event.output.error)
                            : context,
                        ),
                      },
                      {
                        guard: ({context, event}) => refreshEstablishes(context, event.output),
                        target: 'establishing',
                        actions: {type: 'recoverStream', params: {mode: 'rebootstrap'}},
                      },
                      {
                        target: '#session.open.recovery.none',
                        actions: [
                          {
                            type: 'applySnapshot',
                            params: ({context, event}) => ({
                              snapshot: applies(context, refreshSettled(context, event.output)),
                            }),
                          },
                          {
                            type: 'settleSnapshot',
                            params: ({context, event}) => refreshSettled(context, event.output),
                          },
                          {type: 'recoverStream', params: {mode: 'retry'}},
                        ],
                      },
                    ],
                    onError: {
                      target: '#session.open.recovery.none',
                      actions: assign(({context, event}) =>
                        withProblem(context, 'connection', toError(event.error)),
                      ),
                    },
                  },
                },
                /** A fresh stream is dialing; its first batch hands the flight to `snapshot`. */
                establishing: {
                  on: {'session.stream-failed': {target: '#session.open.recovery.none'}},
                },
                /** The flight waits on the fenced load of the store its stream established. */
                snapshot: {},
              },
            },
          },
        },
        replacement: {
          initial: 'none',
          states: {
            none: {on: {'stream-established': ESTABLISH}},
            loading: {
              on: {'stream-established': ESTABLISH},
              invoke: {
                id: 'loadReplacementSnapshot',
                src: 'loadReplacementSnapshot',
                input: ({context}) => ({storeId: context.replacement.storeId}),
                onDone: [
                  {
                    // The authoritative boundary proved that a still-newer store won between the
                    // stream bootstrap and this query. Keep no candidate for the retired store and
                    // establish a new generation before asking again.
                    guard: ({event}) => storeReplaced(event.output),
                    target: 'none',
                    actions: [
                      raise(({context}) => ({
                        type: 'session.store-replaced' as const,
                        owner: context.replacement.owner,
                      })),
                      {type: 'recoverStream', params: {mode: 'rebootstrap'}},
                    ],
                  },
                  {
                    target: 'none',
                    actions: [
                      {
                        type: 'applySnapshot',
                        params: ({context, event}) => ({
                          snapshot: applies(context, replacementSettled(context, event.output)),
                        }),
                      },
                      // Applying higher post-replacement evidence changes whether a closed stream
                      // should reconnect: the stream owner reevaluates, even if its terminal socket
                      // closed while this snapshot was in flight.
                      {
                        type: 'reevaluateStream',
                        params: ({context, event}) => ({
                          applied:
                            context.replacement.owner !== 'startup' &&
                            applies(context, replacementSettled(context, event.output)) !== null,
                        }),
                      },
                      {
                        type: 'settleSnapshot',
                        params: ({context, event}) => replacementSettled(context, event.output),
                      },
                      raise(({context, event}) => ({
                        type: 'session.snapshot-settled' as const,
                        owner: context.replacement.owner,
                        ok: event.output.ok,
                      })),
                    ],
                  },
                ],
                onError: {
                  target: 'none',
                  actions: [
                    {
                      type: 'settleSnapshot',
                      params: ({context, event}) =>
                        replacementSettled(context, {ok: false, error: toError(event.error)}),
                    },
                    raise(({context}) => ({
                      type: 'session.snapshot-settled' as const,
                      owner: context.replacement.owner,
                      ok: false,
                    })),
                  ],
                },
              },
            },
          },
        },
        iterations: {
          initial: 'waiting',
          states: {
            /** Start-up has no stream-store identity to fence a query to yet. */
            waiting: {
              on: {
                'iterations-refresh': {
                  target: 'loading',
                  actions: 'beginIterationQuery',
                },
                // A cold-start stream failure proves there will be no initial query to await.
                'session.stream-failed': {target: 'ready'},
              },
            },
            ready: {
              on: {
                'iterations-refresh': {
                  target: 'loading',
                  actions: 'beginIterationQuery',
                },
                wake: {
                  guard: 'canRefreshIterations',
                  target: 'loading',
                  actions: 'beginCurrentIterationQuery',
                },
              },
            },
            loading: {
              on: {
                // Retain exactly one follow-up and let the owned actor settle physically first.
                'iterations-refresh': {actions: 'queueIterationQuery'},
                wake: {
                  guard: 'canRefreshIterations',
                  actions: 'queueCurrentIterationQuery',
                },
              },
              invoke: {
                id: 'loadIterations',
                src: 'loadIterations',
                input: ({context}) => ({storeId: context.iterations.loadingStoreId}),
                onDone: [
                  {
                    guard: 'iterationRefreshPending',
                    target: 'loading',
                    reenter: true,
                    actions: 'promoteIterationQuery',
                  },
                  {
                    target: 'ready',
                    actions: [
                      'clearIterationError',
                      {
                        type: 'publishIterations',
                        params: ({event}) => ({navigation: event.output}),
                      },
                    ],
                  },
                ],
                onError: [
                  {
                    guard: 'iterationRefreshPending',
                    target: 'loading',
                    reenter: true,
                    actions: 'promoteIterationQuery',
                  },
                  {
                    target: 'ready',
                    actions: {
                      type: 'reportIterationError',
                      params: ({event}) => ({error: toError(event.error)}),
                    },
                  },
                ],
              },
            },
          },
        },
      },
    },
    /** Closed: every later report and reply is dropped. */
    closed: {type: 'final'},
  },
});

export type SessionSnapshot = SnapshotFrom<typeof sessionMachine>;

/** Whether start-up has either completed its fenced handoff or failed (or the session ended). */
export function sessionStartupSettled(snapshot: SessionSnapshot): boolean {
  return (
    snapshot.status !== 'active' ||
    (snapshot.matches({open: {startup: 'done'}}) && snapshot.matches({open: {iterations: 'ready'}}))
  );
}

/** The stream's status and its error agree: stale exactly when there is an error. */
export function sessionInvariants(snapshot: SessionSnapshot): string[] {
  const {status, error} = snapshot.context.view;
  if ((status === 'stale') === (error !== null)) return [];
  return [`status ${status} with error ${error === null ? 'null' : 'set'}`];
}
