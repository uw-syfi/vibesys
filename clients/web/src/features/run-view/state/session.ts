/**
 * The live session's connection core: start-up (event stream, then a store-fenced snapshot), what the stream
 * and the control channel report, and the single-flight recovery that a browser wake or an
 * explicit Reattach starts. Pure: the shell (`effects/session.ts`) performs each request through
 * the transport and dispatches the reply with the request's token.
 *
 * A loaded snapshot reaches the run fold only through this core: the reply carries it, and the core
 * asks the shell to apply it only for an awaited token newer than the last snapshot applied, so a
 * slow start-up snapshot cannot overwrite a newer refresh's.
 */
import type {
  ControlChannelState,
  RunSnapshot,
  StreamConnectionState,
} from '@vibesys/backend-client';
import {only, type Step} from '@vibesys/core-state';
import type {WebSessionState, WebSessionStatus} from '../../../entities/connection.js';

/** How a recovery restarts the event stream: resume where it was, or a fresh bootstrap. */
type StreamRecovery = 'retry' | 'rebootstrap';

/** Whether the page could reach anything now: a hidden tab or an offline browser waits. */
export interface Reachability {
  readonly visible: boolean;
  readonly online: boolean;
}

type Startup =
  | {readonly kind: 'idle'}
  | {
      readonly kind: 'subscribing';
      readonly token: number;
      readonly subscribed: boolean;
      readonly snapshot: 'waiting' | 'loaded' | 'failed';
    }
  | {readonly kind: 'done'};

/**
 * Recovery is single-flight, but its intent is monotone while that flight owns the control redial
 * and the snapshot: a lifecycle retry cannot weaken a fresh bootstrap the user asked for.
 */
type Recovery =
  | {readonly kind: 'none'}
  | {
      readonly kind: 'refreshing';
      readonly token: number;
      readonly intent: StreamRecovery;
      readonly phase: 'preparing' | 'establishing' | 'snapshot';
      /** Failure generation visible when this recovery began. */
      readonly problemEpoch: number;
    };

/** The snapshot that validates one reconciler-proven replacement stream generation. */
type ReplacementSnapshot =
  | {readonly kind: 'none'}
  | {
      readonly kind: 'loading';
      readonly token: number;
      readonly storeId: string;
      /** Flights whose completion depends on this fenced generation. */
      readonly owner: 'startup' | 'startup-recovery' | 'recovery' | 'replacement';
      /** Failure generation visible when this fenced query began. */
      readonly problemEpoch: number;
    };

type StreamProblem = 'none' | 'snapshot' | 'connection';

export type SessionState =
  | {
      readonly kind: 'open';
      readonly view: WebSessionState;
      readonly startup: Startup;
      readonly recovery: Recovery;
      readonly replacement: ReplacementSnapshot;
      /** Whether the current stream generation has connection proof. */
      readonly streamConnected: boolean;
      /** Whether the newest fenced snapshot proof is usable. */
      readonly streamSnapshot: 'none' | 'trusted' | 'failed';
      /** Owner and monotone order of the newest reason the stream is stale. */
      readonly streamProblem: StreamProblem;
      readonly problemEpoch: number;
      /** The newest snapshot token applied or superseded; 0 before the first. */
      readonly applied: number;
      readonly next: number;
    }
  /** Closed: every later report and reply is dropped. */
  | {readonly kind: 'closed'; readonly view: WebSessionState; readonly next: number};

/** A snapshot load's result: the snapshot, or why it could not be loaded. */
export type SnapshotResult =
  | {readonly ok: true; readonly snapshot: RunSnapshot}
  | {
      readonly ok: false;
      readonly error: Error;
      /** The requested stream store was replaced before the server acquired the snapshot. */
      readonly reason?: 'store-replaced';
    };

/** A refresh redials the control channel, then reloads the snapshot. */
type RefreshOutcome =
  | {readonly kind: 'redial-failed'; readonly error: Error}
  | {readonly kind: 'ready'}
  | {readonly kind: 'snapshot'; readonly result: SnapshotResult};

export type SessionAction =
  | {readonly type: 'start'}
  | {readonly type: 'subscribe-reply'; readonly token: number; readonly error: Error | null}
  | {readonly type: 'refresh-reply'; readonly token: number; readonly outcome: RefreshOutcome}
  /** A first or replacement batch established the store a snapshot can fence to. */
  | {readonly type: 'stream-established'; readonly storeId: string}
  | {
      readonly type: 'replacement-snapshot-reply';
      readonly token: number;
      readonly storeId: string;
      readonly result: SnapshotResult;
    }
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

export type SessionRequest =
  | /** Fold this snapshot into the run. */
  {readonly kind: 'apply-snapshot'; readonly snapshot: RunSnapshot}
  | {readonly kind: 'subscribe'; readonly token: number}
  /** Redial the control channel (inert when healthy), then reload the snapshot. */
  | {readonly kind: 'refresh'; readonly token: number; readonly mode: StreamRecovery}
  | {
      readonly kind: 'load-replacement-snapshot';
      readonly token: number;
      readonly storeId: string;
    }
  | {readonly kind: 'recover-stream'; readonly mode: 'retry'}
  | {readonly kind: 'recover-stream'; readonly mode: 'rebootstrap'}
  /** Retry only if the newly applied snapshot is ahead of the event fold. */
  | {readonly kind: 'reevaluate-stream'}
  | {readonly kind: 'redial-controls'}
  | {readonly kind: 'close'};

type Open = Extract<SessionState, {kind: 'open'}>;
type SessionStep = Step<SessionState, SessionRequest>;

export const SESSION_INITIAL: SessionState = {
  kind: 'open',
  view: {hasSnapshot: false, status: 'connecting', error: null, controls: {status: 'connected'}},
  startup: {kind: 'idle'},
  recovery: {kind: 'none'},
  replacement: {kind: 'none'},
  streamConnected: false,
  streamSnapshot: 'none',
  streamProblem: 'none',
  problemEpoch: 0,
  applied: 0,
  next: 1,
};

/** The state with the stream's status (and its error) set; the same state when unchanged. */
function withStatus(state: Open, status: WebSessionStatus, error: Error | null): Open {
  if (state.view.status === status && state.view.error === error) return state;
  return {...state, view: {...state.view, status, error}};
}

/** Record a new stale cause so older asynchronous evidence cannot erase it. */
function withProblem(state: Open, problem: Exclude<StreamProblem, 'none'>, error: Error): Open {
  return withStatus(
    {...state, streamProblem: problem, problemEpoch: state.problemEpoch + 1},
    'stale',
    error,
  );
}

/**
 * The awaited snapshot reply `token` settled: on success the snapshot is applied unless a newer one
 * already was; on failure the page goes stale.
 */
function snapshotSettled(
  state: Open,
  token: number,
  result: SnapshotResult,
  observedProblemEpoch: number,
): Step<Open, SessionRequest> {
  // A proven stream replacement or a newer snapshot reply can retire an older
  // request before it settles. Its failure belongs to the retired generation
  // just as much as its success does, so drop both before changing the view.
  if (token <= state.applied) return only(state);
  if (!result.ok) {
    return only(withProblem({...state, streamSnapshot: 'failed'}, 'snapshot', result.error));
  }
  const shown = state.view.hasSnapshot
    ? state
    : {...state, view: {...state.view, hasSnapshot: true}};
  // This response may retire only the stale evidence that existed when its query began. A stream
  // close or protocol fault reported afterward owns the view until newer connection evidence.
  const supersedesProblem =
    shown.streamProblem !== 'none' && shown.problemEpoch <= observedProblemEpoch;
  const trusted = {
    ...shown,
    streamSnapshot: 'trusted' as const,
    streamProblem: supersedesProblem ? ('none' as const) : shown.streamProblem,
    applied: token,
  };
  return {
    state:
      trusted.streamProblem === 'none' && trusted.streamConnected
        ? withStatus(trusted, 'connected', null)
        : trusted,
    requests: [{kind: 'apply-snapshot', snapshot: result.snapshot}],
  };
}

/** Start a snapshot load only after the stream has established the store that fences it. */
function streamEstablished(state: Open, storeId: string): SessionStep {
  if (state.replacement.kind === 'loading' && state.replacement.storeId === storeId) {
    return only(state);
  }
  const startupWaiting =
    state.startup.kind === 'subscribing' && state.startup.snapshot === 'waiting';
  const owner = startupWaiting
    ? state.recovery.kind === 'refreshing'
      ? 'startup-recovery'
      : 'startup'
    : state.recovery.kind === 'refreshing'
      ? 'recovery'
      : 'replacement';
  const token = state.next;
  return {
    state: {
      ...state,
      next: token + 1,
      // Every earlier load began before this replacement was observed. Fence
      // those replies without pretending that their snapshots describe it.
      applied: Math.max(state.applied, token - 1),
      recovery:
        (owner === 'recovery' || owner === 'startup-recovery') &&
        state.recovery.kind === 'refreshing'
          ? {...state.recovery, phase: 'snapshot'}
          : state.recovery,
      replacement: {kind: 'loading', token, storeId, owner, problemEpoch: state.problemEpoch},
    },
    requests: [{kind: 'load-replacement-snapshot', token, storeId}],
  };
}

function replacementRecovery(
  state: Open,
  owner: ReplacementSnapshot & {kind: 'loading'},
): Recovery {
  if (owner.owner === 'startup' || owner.owner === 'startup-recovery') return state.recovery;
  if (state.recovery.kind === 'refreshing') {
    return {...state.recovery, intent: 'rebootstrap', phase: 'establishing'};
  }
  return {
    kind: 'refreshing',
    token: owner.token,
    intent: 'rebootstrap',
    phase: 'establishing',
    problemEpoch: state.problemEpoch,
  };
}

function settleReplacementOwner(
  state: Open,
  owner: ReplacementSnapshot & {kind: 'loading'},
  result: SnapshotResult,
): Open {
  // A later fenced success supersedes a failed startup snapshot even when its recovery owner was
  // acquired after startup. The pending subscription handle can then finish startup from this
  // trusted generation. A failure owned by another flight must not erase an earlier success; the
  // stream proof markers still prevent that handle from clearing the new failure.
  const settlesStartup =
    state.startup.kind === 'subscribing' &&
    (result.ok || owner.owner === 'startup' || owner.owner === 'startup-recovery');
  if (settlesStartup || owner.owner === 'startup-recovery' || owner.owner === 'recovery') {
    return {
      ...state,
      startup: settlesStartup
        ? {...state.startup, snapshot: result.ok ? 'loaded' : 'failed'}
        : state.startup,
      // A recovery that supplied startup's first usable generation is not a
      // second flight. Retire both owners together so the next wake or
      // explicit Reattach can start another recovery.
      recovery:
        owner.owner === 'startup-recovery' || owner.owner === 'recovery'
          ? {kind: 'none'}
          : state.recovery,
    };
  }
  return state;
}

function finishStartupSnapshot(state: Open, result: SnapshotResult): Open {
  if (
    state.startup.kind !== 'subscribing' ||
    !state.startup.subscribed ||
    state.startup.snapshot === 'waiting'
  ) {
    return state;
  }
  const done: Open = {...state, startup: {kind: 'done'}};
  if (!result.ok) return done;
  return state.streamConnected && state.streamProblem === 'none'
    ? withStatus(done, 'connected', null)
    : done;
}

/** Accept the snapshot only while the replacement generation that requested it is current. */
function replacementSnapshotSettled(
  state: Open,
  action: Extract<SessionAction, {type: 'replacement-snapshot-reply'}>,
): SessionStep {
  const {replacement} = state;
  if (
    replacement.kind !== 'loading' ||
    replacement.token !== action.token ||
    replacement.storeId !== action.storeId
  ) {
    return only(state);
  }
  const settled: Open = {...state, replacement: {kind: 'none'}};
  if (!action.result.ok && action.result.reason === 'store-replaced') {
    // The authoritative boundary proved that a still-newer store won between
    // the stream bootstrap and this query. Keep no candidate for the retired
    // store and establish a new generation before asking again.
    return {
      state: {...settled, recovery: replacementRecovery(settled, replacement)},
      requests: [{kind: 'recover-stream', mode: 'rebootstrap'}],
    };
  }
  const ownerSettled = settleReplacementOwner(settled, replacement, action.result);
  const snapshot = snapshotSettled(
    ownerSettled,
    action.token,
    action.result,
    replacement.problemEpoch,
  );
  const finished = {
    ...snapshot,
    state: finishStartupSnapshot(snapshot.state, action.result),
  };
  // Applying higher post-replacement evidence changes shouldReconnect. Ask the
  // stream owner to reevaluate even if its terminal socket closed while this
  // snapshot was in flight. A failed or superseded load carries no such proof.
  const applied = finished.requests.some(request => request.kind === 'apply-snapshot');
  return applied && replacement.owner !== 'startup'
    ? {...finished, requests: [...finished.requests, {kind: 'reevaluate-stream'}]}
    : finished;
}

function recover(state: Open, mode: StreamRecovery, reach: Reachability): SessionStep {
  if (!reach.visible || !reach.online) return only(state);
  if (state.recovery.kind === 'refreshing') {
    if (mode === 'rebootstrap' && state.recovery.intent !== 'rebootstrap') {
      return only({...state, recovery: {...state.recovery, intent: mode}});
    }
    return only(state);
  }
  const token = state.next;
  return {
    state: {
      ...state,
      next: token + 1,
      recovery: {
        kind: 'refreshing',
        token,
        intent: mode,
        phase: 'preparing',
        problemEpoch: state.problemEpoch,
      },
    },
    requests: [{kind: 'refresh', token, mode}],
  };
}

type StartupAction = Extract<SessionAction, {type: 'start' | 'subscribe-reply'}>;

/** Start-up: establish the stream store, then load a snapshot fenced to it. */
function startupStep(state: Open, action: StartupAction): SessionStep {
  const {startup} = state;
  if (action.type === 'start') {
    if (startup.kind !== 'idle') return only(state);
    const token = state.next;
    return {
      state: {
        ...state,
        next: token + 1,
        startup: {kind: 'subscribing', token, subscribed: false, snapshot: 'waiting'},
      },
      requests: [{kind: 'subscribe', token}],
    };
  }
  if (startup.kind !== 'subscribing' || startup.token !== action.token) return only(state);
  if (action.error !== null) {
    return only(
      withProblem(
        {...state, startup: {kind: 'done'}, streamConnected: false},
        'connection',
        action.error,
      ),
    );
  }
  if (startup.snapshot === 'waiting') {
    return only({...state, startup: {...startup, subscribed: true}, streamConnected: true});
  }
  const done: Open = {...state, startup: {kind: 'done'}, streamConnected: true};
  return startup.snapshot === 'loaded' &&
    state.streamSnapshot === 'trusted' &&
    state.streamProblem === 'none'
    ? only(withStatus(done, 'connected', null))
    : only(done);
}

/** A refresh settled: release the flight and hand recovery to the stream. */
function refreshSettled(state: Open, token: number, outcome: RefreshOutcome): SessionStep {
  if (
    state.recovery.kind !== 'refreshing' ||
    state.recovery.token !== token ||
    state.recovery.phase !== 'preparing'
  ) {
    return only(state);
  }
  if (outcome.kind === 'redial-failed') {
    return only(withProblem({...state, recovery: {kind: 'none'}}, 'connection', outcome.error));
  }
  if (
    state.recovery.intent === 'rebootstrap' ||
    (outcome.kind === 'snapshot' &&
      !outcome.result.ok &&
      outcome.result.reason === 'store-replaced')
  ) {
    return {
      state: {...state, recovery: {...state.recovery, phase: 'establishing'}},
      requests: [{kind: 'recover-stream', mode: 'rebootstrap'}],
    };
  }
  if (outcome.kind === 'ready') {
    return {
      state: {...state, recovery: {...state.recovery, phase: 'establishing'}},
      requests: [{kind: 'recover-stream', mode: 'rebootstrap'}],
    };
  }
  const problemEpoch = state.recovery.problemEpoch;
  const settled: Open = {...state, recovery: {kind: 'none'}};
  const refreshed = snapshotSettled(settled, token, outcome.result, problemEpoch);
  return {
    state: refreshed.state,
    requests: [...refreshed.requests, {kind: 'recover-stream', mode: 'retry'}],
  };
}

type Report = Extract<SessionAction, {type: 'stream' | 'fault' | 'controls' | 'offline'}>;

/** What the stream, the control channel, or the browser reported. */
function reported(state: Open, action: Report): Open {
  switch (action.type) {
    case 'stream':
      // Every drop is reported, on an ended run too: `connectionBanners` decides what to show,
      // and suppressing it here left an ended run's empty transcript unexplained (#1044).
      if (action.state.status === 'connected') {
        const connected = state.streamConnected ? state : {...state, streamConnected: true};
        return state.streamSnapshot === 'trusted' && state.streamProblem !== 'snapshot'
          ? withStatus({...connected, streamProblem: 'none'}, 'connected', null)
          : connected;
      }
      return withProblem({...state, streamConnected: false}, 'connection', action.state.error);
    case 'fault':
      return withProblem({...state, streamConnected: false}, 'connection', action.error);
    case 'controls':
      return {...state, view: {...state.view, controls: action.state}};
    case 'offline':
      // Offline is this browser's reachability: it cannot make an ended run's complete
      // transcript incomplete.
      return action.runEnded
        ? state
        : withProblem(
            {...state, streamConnected: false},
            'connection',
            new Error('Network is offline'),
          );
  }
}

export function sessionStep(state: SessionState, action: SessionAction): SessionStep {
  if (state.kind === 'closed') return only(state);
  switch (action.type) {
    case 'start':
    case 'subscribe-reply':
      return startupStep(state, action);
    case 'refresh-reply':
      return refreshSettled(state, action.token, action.outcome);
    case 'stream-established':
      return streamEstablished(state, action.storeId);
    case 'replacement-snapshot-reply':
      return replacementSnapshotSettled(state, action);
    case 'stream':
    case 'fault':
    case 'controls':
    case 'offline':
      return only(reported(state, action));
    case 'wake':
      return recover(state, 'retry', action.reach);
    case 'reattach':
      // A healthy ended stream stays inert; a stale one can always ask for a fresh bootstrap,
      // because the fault is evidence the transcript is incomplete.
      if (state.view.status !== 'stale' && action.runEnded) return only(state);
      return recover(state, 'rebootstrap', action.reach);
    case 'reconnect-controls':
      // Not a refresh: a channel that reported a drop has a redial armed already, and only the
      // transport's `reconnect` cancels that backoff and dials now.
      return {state, requests: [{kind: 'redial-controls'}]};
    case 'close':
      return {
        state: {kind: 'closed', view: state.view, next: state.next},
        requests: [{kind: 'close'}],
      };
  }
}

/** The token each reply slot awaits, by the reply that answers it; closed awaits nothing. */
export function sessionAwaited(state: SessionState): {
  readonly subscribe: number | null;
  readonly refresh: number | null;
  readonly replacement: number | null;
} {
  if (state.kind === 'closed') {
    return {subscribe: null, refresh: null, replacement: null};
  }
  const {startup, recovery, replacement} = state;
  return {
    subscribe: startup.kind === 'subscribing' ? startup.token : null,
    refresh: recovery.kind === 'refreshing' ? recovery.token : null,
    replacement: replacement.kind === 'loading' ? replacement.token : null,
  };
}

/** Whether start-up has either completed its fenced handoff or failed. */
export function sessionStartupSettled(state: SessionState): boolean {
  return state.kind === 'closed' || state.startup.kind === 'done';
}

export function sessionInvariants(state: SessionState): string[] {
  const {status, error} = state.view;
  if ((status === 'stale') === (error !== null)) return [];
  return [`status ${status} with error ${error === null ? 'null' : 'set'}`];
}
