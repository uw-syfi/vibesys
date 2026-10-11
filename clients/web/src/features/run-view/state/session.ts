/**
 * The live session's connection core: start-up (snapshot, then the event stream), what the stream
 * and the control channel report, and the single-flight recovery that a browser wake or an
 * explicit Reattach starts. Pure: the shell (`effects/session.ts`) performs each request through
 * the transport and dispatches the reply with the request's token.
 */
import type {ControlChannelState, StreamConnectionState} from '@vibesys/backend-client';
import type {WebSessionState, WebSessionStatus} from '../../../entities/connection.js';
import {only, type Step} from '../../../shared/step.js';

/** How a recovery restarts the event stream: resume where it was, or a fresh bootstrap. */
type StreamRecovery = 'retry' | 'rebootstrap';

/** Whether the page could reach anything now: a hidden tab or an offline browser waits. */
export interface Reachability {
  readonly visible: boolean;
  readonly online: boolean;
}

type Startup =
  | {readonly kind: 'idle'}
  | {readonly kind: 'snapshot'; readonly token: number}
  | {readonly kind: 'subscribing'; readonly token: number; readonly snapshotFailed: boolean}
  | {readonly kind: 'done'};

/**
 * Recovery is single-flight, but its intent is monotone while that flight owns the control redial
 * and the snapshot: a lifecycle retry cannot weaken a fresh bootstrap the user asked for.
 */
type Recovery =
  | {readonly kind: 'none'}
  | {readonly kind: 'refreshing'; readonly token: number; readonly intent: StreamRecovery};

export type SessionState =
  | {
      readonly kind: 'open';
      readonly view: WebSessionState;
      readonly startup: Startup;
      readonly recovery: Recovery;
      readonly next: number;
    }
  /** Closed: every later report and reply is dropped. */
  | {readonly kind: 'closed'; readonly view: WebSessionState; readonly next: number};

/** A refresh redials the control channel, then reloads the snapshot. */
type RefreshOutcome =
  | {readonly kind: 'redial-failed'; readonly error: Error}
  | {readonly kind: 'snapshot'; readonly error: Error | null};

export type SessionAction =
  | {readonly type: 'start'}
  | {readonly type: 'snapshot-reply'; readonly token: number; readonly error: Error | null}
  | {readonly type: 'subscribe-reply'; readonly token: number; readonly error: Error | null}
  | {readonly type: 'refresh-reply'; readonly token: number; readonly outcome: RefreshOutcome}
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
  | {readonly kind: 'load-snapshot'; readonly token: number}
  | {readonly kind: 'subscribe'; readonly token: number}
  /** Redial the control channel (inert when healthy), then reload the snapshot. */
  | {readonly kind: 'refresh'; readonly token: number}
  | {readonly kind: 'recover-stream'; readonly mode: StreamRecovery}
  | {readonly kind: 'redial-controls'}
  | {readonly kind: 'close'};

type Open = Extract<SessionState, {kind: 'open'}>;
type SessionStep = Step<SessionState, SessionRequest>;

export const SESSION_INITIAL: SessionState = {
  kind: 'open',
  view: {hasSnapshot: false, status: 'connecting', error: null, controls: {status: 'connected'}},
  startup: {kind: 'idle'},
  recovery: {kind: 'none'},
  next: 1,
};

/** The state with the stream's status (and its error) set; the same state when unchanged. */
function withStatus(state: Open, status: WebSessionStatus, error: Error | null): Open {
  if (state.view.status === status && state.view.error === error) return state;
  return {...state, view: {...state.view, status, error}};
}

function withSnapshot(state: Open): Open {
  return state.view.hasSnapshot ? state : {...state, view: {...state.view, hasSnapshot: true}};
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
    state: {...state, next: token + 1, recovery: {kind: 'refreshing', token, intent: mode}},
    requests: [{kind: 'refresh', token}],
  };
}

type StartupAction = Extract<SessionAction, {type: 'start' | 'snapshot-reply' | 'subscribe-reply'}>;

/** Start-up: the snapshot first, then the event stream. */
function startupStep(state: Open, action: StartupAction): SessionStep {
  const {startup} = state;
  if (action.type === 'start') {
    if (startup.kind !== 'idle') return only(state);
    const token = state.next;
    return {
      state: {...state, next: token + 1, startup: {kind: 'snapshot', token}},
      requests: [{kind: 'load-snapshot', token}],
    };
  }
  if (action.type === 'snapshot-reply') {
    if (startup.kind !== 'snapshot' || startup.token !== action.token) return only(state);
    const loaded =
      action.error === null ? withSnapshot(state) : withStatus(state, 'stale', action.error);
    const token = state.next;
    return {
      state: {
        ...loaded,
        next: token + 1,
        startup: {kind: 'subscribing', token, snapshotFailed: action.error !== null},
      },
      requests: [{kind: 'subscribe', token}],
    };
  }
  if (startup.kind !== 'subscribing' || startup.token !== action.token) return only(state);
  const done: Open = {...state, startup: {kind: 'done'}};
  if (action.error !== null) return only(withStatus(done, 'stale', action.error));
  if (state.view.status === 'connecting' && !startup.snapshotFailed) {
    return only(withStatus(done, 'connected', null));
  }
  return only(done);
}

/** A refresh settled: release the flight and hand recovery to the stream. */
function refreshSettled(state: Open, token: number, outcome: RefreshOutcome): SessionStep {
  if (state.recovery.kind !== 'refreshing' || state.recovery.token !== token) return only(state);
  const settled: Open = {...state, recovery: {kind: 'none'}};
  if (outcome.kind === 'redial-failed') return only(withStatus(settled, 'stale', outcome.error));
  const refreshed =
    outcome.error === null ? withSnapshot(settled) : withStatus(settled, 'stale', outcome.error);
  // The flight is released in the same step that hands recovery to the stream, so an intent
  // arriving after it starts a new flight instead of being folded into a dispatched one.
  return {state: refreshed, requests: [{kind: 'recover-stream', mode: state.recovery.intent}]};
}

type Report = Extract<SessionAction, {type: 'stream' | 'fault' | 'controls' | 'offline'}>;

/** What the stream, the control channel, or the browser reported. */
function reported(state: Open, action: Report): Open {
  switch (action.type) {
    case 'stream':
      // Every drop is reported, on an ended run too: `connectionBanners` decides what to show,
      // and suppressing it here left an ended run's empty transcript unexplained (#1044).
      return action.state.status === 'connected'
        ? withStatus(state, 'connected', null)
        : withStatus(state, 'stale', action.state.error);
    case 'fault':
      return withStatus(state, 'stale', action.error);
    case 'controls':
      return {...state, view: {...state.view, controls: action.state}};
    case 'offline':
      // Offline is this browser's reachability: it cannot make an ended run's complete
      // transcript incomplete.
      return action.runEnded ? state : withStatus(state, 'stale', new Error('Network is offline'));
  }
}

export function sessionStep(state: SessionState, action: SessionAction): SessionStep {
  if (state.kind === 'closed') return only(state);
  switch (action.type) {
    case 'start':
    case 'snapshot-reply':
    case 'subscribe-reply':
      return startupStep(state, action);
    case 'refresh-reply':
      return refreshSettled(state, action.token, action.outcome);
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

/** The token each slot awaits; closed awaits nothing. */
function awaitedTokens(state: SessionState): {
  readonly startup: number | null;
  readonly recovery: number | null;
} {
  if (state.kind === 'closed') return {startup: null, recovery: null};
  const startup =
    state.startup.kind === 'snapshot' || state.startup.kind === 'subscribing'
      ? state.startup.token
      : null;
  return {startup, recovery: state.recovery.kind === 'refreshing' ? state.recovery.token : null};
}

export function sessionInvariants(state: SessionState): string[] {
  const problems: string[] = [];
  const {status, error} = state.view;
  if ((status === 'stale') !== (error !== null)) {
    problems.push(`status ${status} with error ${error === null ? 'null' : 'set'}`);
  }
  const {startup, recovery} = awaitedTokens(state);
  for (const token of [startup, recovery]) {
    if (token !== null && token >= state.next) problems.push(`token ${token} not issued`);
  }
  if (startup !== null && startup === recovery) problems.push('two slots await one token');
  return problems;
}
