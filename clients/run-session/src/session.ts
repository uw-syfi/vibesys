/**
 * The live session's lifecycle as a machine: start-up (the snapshot, then the event stream), what
 * the stream and the control channel report, and the single-flight recovery that a browser wake or
 * an explicit Reattach starts. Control state, so a machine (docs/contributing/clients-architecture.md,
 * "State"); the shell (`web-session.ts`) provides its operations and actions.
 *
 * Start-up and recovery are two parallel regions of `open`, and each flight is an invoked
 * operation, so a reply can only come from the flight its region is in: leaving the region stops
 * the operation, and a stopped operation never reports. One ordering fact the regions share
 * remains: a snapshot is applied only when it was issued after the last one applied, so a slow
 * start-up snapshot cannot overwrite a newer refresh's. Snapshot loads carry an issue sequence for
 * that, which orders loads and is not a staleness token.
 */

import type {RunSnapshot} from '@vibesys/contracts';
import {unprovided} from '@vibesys/machine';
import type {ControlChannelState, StreamConnectionState} from '@vibesys/transport';
import {assign, type SnapshotFrom, setup} from 'xstate';
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
  | {readonly ok: false; readonly error: Error};

/** A refresh redials the control channel, then reloads the snapshot. */
export type RefreshOutcome =
  | {readonly kind: 'redial-failed'; readonly error: Error}
  | {readonly kind: 'snapshot'; readonly result: SnapshotResult};

export type SessionEvent =
  | {readonly type: 'start'}
  /** The event stream changed state. */
  | {readonly type: 'stream'; readonly state: StreamConnectionState}
  /** The stream reported a fault in band (a protocol error), or could not be recovered. */
  | {readonly type: 'fault'; readonly error: Error}
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

export interface SessionContext {
  readonly view: WebSessionState;
  /** The issue sequence of the latest snapshot load; `applied` is the newest one applied. */
  readonly issued: number;
  readonly applied: number;
  /** The issue sequence of start-up's load and of the refresh in flight. */
  readonly startupLoad: number;
  readonly refreshLoad: number;
  readonly snapshotFailed: boolean;
  /** The refresh in flight's intent: monotone, so a retry cannot weaken a Reattach. */
  readonly intent: StreamRecovery;
}

interface Settled {
  readonly load: number;
  readonly result: SnapshotResult;
}

const INITIAL_CONTEXT: SessionContext = {
  view: {hasSnapshot: false, status: 'connecting', error: null, controls: {status: 'connected'}},
  issued: 0,
  applied: 0,
  startupLoad: 0,
  refreshLoad: 0,
  snapshotFailed: false,
  intent: 'retry',
};

/** The view with the stream's status (and its error) set; the same view when unchanged. */
function withStatus(
  context: SessionContext,
  status: WebSessionStatus,
  error: Error | null,
): Partial<SessionContext> {
  if (context.view.status === status && context.view.error === error) return {};
  return {view: {...context.view, status, error}};
}

/** The snapshot to apply: a loaded one issued after the last one applied, else null. */
function newer(context: SessionContext, {load, result}: Settled): {snapshot: RunSnapshot | null} {
  return {snapshot: result.ok && load > context.applied ? result.snapshot : null};
}

/** A refresh that reached its snapshot load, as a settled load. */
function refreshed(context: SessionContext, outcome: RefreshOutcome): Settled {
  const result: SnapshotResult =
    outcome.kind === 'snapshot' ? outcome.result : {ok: false, error: outcome.error};
  return {load: context.refreshLoad, result};
}

const reachable = (reach: Reachability): boolean => reach.visible && reach.online;

/** A healthy ended stream stays inert; a stale one can always ask for a fresh bootstrap. */
function mayReattach(context: SessionContext, event: SessionEvent): boolean {
  if (event.type !== 'reattach' || !reachable(event.reach)) return false;
  return context.view.status === 'stale' || !event.runEnded;
}

function issueRefresh(context: SessionContext, intent: StreamRecovery): Partial<SessionContext> {
  return {intent, issued: context.issued + 1, refreshLoad: context.issued + 1};
}

export const sessionMachine = setup({
  types: {context: {} as SessionContext, events: {} as SessionEvent},
  actors: {
    loadSnapshot: unprovided<SnapshotResult>('loadSnapshot'),
    subscribe: unprovided<Error | null>('subscribe'),
    refresh: unprovided<RefreshOutcome>('refresh'),
  },
  actions: {
    /** Fold `snapshot` into the run; null (an older or a failed load) does nothing. */
    applySnapshot: (_, _params: {readonly snapshot: RunSnapshot | null}) => undefined,
    recoverStream: (_, _params: {readonly mode: StreamRecovery}) => undefined,
    redialControls: () => undefined,
    closeShell: () => undefined,
    /** A load settled: the view, and the newest load applied (after `applySnapshot` read it). */
    snapshotSettled: assign(({context}, {load, result}: Settled): Partial<SessionContext> => {
      if (!result.ok) return withStatus(context, 'stale', result.error);
      const view = context.view.hasSnapshot ? context.view : {...context.view, hasSnapshot: true};
      return {view, applied: Math.max(load, context.applied)};
    }),
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
        stream: {
          // Every drop is reported, on an ended run too: `connectionBanners` decides what to show,
          // and suppressing it here left an ended run's empty transcript unexplained (#1044).
          actions: assign(({context, event}) =>
            event.state.status === 'connected'
              ? withStatus(context, 'connected', null)
              : withStatus(context, 'stale', event.state.error),
          ),
        },
        fault: {actions: assign(({context, event}) => withStatus(context, 'stale', event.error))},
        controls: {
          actions: assign(({context, event}) => ({view: {...context.view, controls: event.state}})),
        },
        offline: {
          // Offline is this browser's reachability: it cannot make an ended run's complete
          // transcript incomplete.
          guard: ({event}) => !event.runEnded,
          actions: assign(({context}) =>
            withStatus(context, 'stale', new Error('Network is offline')),
          ),
        },
        // Not a refresh: a channel that reported a drop has a redial armed already, and only the
        // transport's `reconnect` cancels that backoff and dials now.
        'reconnect-controls': {actions: 'redialControls'},
      },
      states: {
        startup: {
          initial: 'idle',
          states: {
            idle: {
              on: {
                start: {
                  target: 'snapshot',
                  actions: assign(({context}) => ({
                    issued: context.issued + 1,
                    startupLoad: context.issued + 1,
                  })),
                },
              },
            },
            snapshot: {
              invoke: {
                id: 'loadSnapshot',
                src: 'loadSnapshot',
                onDone: {
                  target: 'subscribing',
                  actions: [
                    {
                      type: 'applySnapshot',
                      params: ({context, event}) =>
                        newer(context, {load: context.startupLoad, result: event.output}),
                    },
                    {
                      type: 'snapshotSettled',
                      params: ({context, event}) => ({
                        load: context.startupLoad,
                        result: event.output,
                      }),
                    },
                    assign({snapshotFailed: ({event}) => !event.output.ok}),
                  ],
                },
              },
            },
            subscribing: {
              invoke: {
                id: 'subscribe',
                src: 'subscribe',
                onDone: [
                  {
                    guard: ({event}) => event.output !== null,
                    target: 'done',
                    actions: assign(({context, event}) =>
                      withStatus(context, 'stale', event.output),
                    ),
                  },
                  {
                    guard: ({context}) =>
                      context.view.status === 'connecting' && !context.snapshotFailed,
                    target: 'done',
                    actions: assign(({context}) => withStatus(context, 'connected', null)),
                  },
                  {target: 'done'},
                ],
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
              },
            },
            refreshing: {
              on: {
                // Single flight, monotone intent: a Reattach upgrades the flight in progress.
                reattach: {
                  guard: ({context, event}) => mayReattach(context, event),
                  actions: assign({intent: 'rebootstrap'}),
                },
              },
              invoke: {
                id: 'refresh',
                src: 'refresh',
                onDone: [
                  {
                    guard: ({event}) => event.output.kind === 'redial-failed',
                    target: 'none',
                    actions: {
                      type: 'snapshotSettled',
                      params: ({context, event}) => refreshed(context, event.output),
                    },
                  },
                  {
                    // The flight is released in the same step that hands recovery to the stream,
                    // so an intent arriving after it starts a new flight instead.
                    target: 'none',
                    actions: [
                      {
                        type: 'applySnapshot',
                        params: ({context, event}) =>
                          newer(context, refreshed(context, event.output)),
                      },
                      {
                        type: 'snapshotSettled',
                        params: ({context, event}) => refreshed(context, event.output),
                      },
                      {type: 'recoverStream', params: ({context}) => ({mode: context.intent})},
                    ],
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

/** The stream's status and its error agree: stale exactly when there is an error. */
export function sessionInvariants(snapshot: SessionSnapshot): string[] {
  const {status, error} = snapshot.context.view;
  if ((status === 'stale') === (error !== null)) return [];
  return [`status ${status} with error ${error === null ? 'null' : 'set'}`];
}
