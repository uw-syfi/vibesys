/**
 * The Run screen's store: a pure reducer over what the app learns about one run (its registry
 * record, from the host's watch, and its log streams) and what the user does on the screen.
 *
 * The run's lifecycle is the record's: the page never guesses a phase the host did not report,
 * except that a Stop it sent shows as stopping until the record catches up. Stopping arms a bound
 * (`FORCE_STOP_BOUND_MS`) on the shell's injected clock through a request token; only when that
 * token comes back while the run is still stopping is "Force stop" offered, and a reply for an
 * earlier stop is dropped. Commands are requests the shell performs through the existing
 * run-control paths; the reducer does no I/O and reads no clock.
 */
import type {LogStream} from '@vibesys/contracts/host-helper';
import type {HostRun, RunEnd} from '@vibesys/hosts';
import type {LogListOutcome, LogView} from '@vibesys/logs';
import type {RunRef} from '@vibesys/runs';

/** How long a Stop may take before "Force stop" is offered. */
export const FORCE_STOP_BOUND_MS = 2 * 60 * 1000;

export type RunTab = 'overview' | 'map' | 'work' | 'logs';
export const RUN_TABS: readonly RunTab[] = ['overview', 'map', 'work', 'logs'];

/** What the app could not do for this run, with why; an action named here is shown disabled. */
export type RunCapability = 'pause' | 'open-window' | 'logs';
export type Unavailable = Readonly<Partial<Record<RunCapability, string>>>;

/** The run as the host's registry lists it: live, ended, or not (yet) listed. */
export type RunRecord =
  | {readonly kind: 'unlisted'}
  | {readonly kind: 'live'; readonly run: HostRun}
  | {readonly kind: 'ended'; readonly run: HostRun};

/** The run's place in its lifecycle, as the screen shows it. */
export type RunPhase =
  | {readonly kind: 'unlisted'}
  | {readonly kind: 'starting'}
  | {readonly kind: 'running'; readonly pausing: boolean}
  | {readonly kind: 'paused'; readonly by: 'user' | 'quota'}
  | {readonly kind: 'stopping'}
  | {readonly kind: 'ended'; readonly run: HostRun; readonly end: RunEnd};

/** The page's own stop flow: confirming, or a stop sent and waiting for the run to end. */
export type StopFlow =
  | {readonly kind: 'idle'}
  | {readonly kind: 'confirming'}
  | {
      readonly kind: 'stopping';
      /** The armed bound's token, until it fires or the stop ends. */
      readonly bound: number | null;
      readonly forceOffered: boolean;
      readonly forced: boolean;
    };

export type RunLogs =
  | {readonly kind: 'idle'}
  | {readonly kind: 'listing'; readonly token: number; readonly streams: readonly LogStream[]}
  | {readonly kind: 'listed'; readonly streams: readonly LogStream[]}
  | {readonly kind: 'failed'; readonly message: string; readonly unsupported: boolean};

export interface RunPageState {
  readonly identity: RunRef;
  /** What the sidebar knows about the run's name: its project and task. */
  readonly title: {readonly project: string; readonly task: string | null};
  readonly hostLabel: string;
  readonly unavailable: Unavailable;
  readonly record: RunRecord;
  readonly tab: RunTab;
  readonly stop: StopFlow;
  readonly logs: RunLogs;
  /** The latest view of each followed stream, by stream id. */
  readonly views: Readonly<Record<string, LogView>>;
  /** The stream the Logs tab shows; null shows the first one. */
  readonly selected: string | null;
  readonly next: number;
}

export type RunPageAction =
  | {readonly type: 'record'; readonly record: RunRecord}
  | {readonly type: 'tab'; readonly tab: RunTab}
  | {readonly type: 'select-stream'; readonly streamId: string}
  | {readonly type: 'view-logs'}
  | {readonly type: 'ask-stop'}
  | {readonly type: 'cancel-stop'}
  | {readonly type: 'confirm-stop'}
  | {readonly type: 'force-stop'}
  | {readonly type: 'stop-failed'}
  | {readonly type: 'bound-elapsed'; readonly token: number}
  | {readonly type: 'streams'; readonly token: number; readonly outcome: LogListOutcome}
  | {readonly type: 'log-view'; readonly streamId: string; readonly view: LogView}
  | {readonly type: 'pause'}
  | {readonly type: 'resume'}
  | {readonly type: 'resume-ended'}
  | {readonly type: 'open-window'}
  | {readonly type: 'archive'}
  | {readonly type: 'run-again'};

export type RunPageRequest =
  | {readonly kind: 'stop'; readonly force: boolean}
  | {readonly kind: 'arm-bound'; readonly token: number; readonly ms: number}
  | {readonly kind: 'list-logs'; readonly token: number}
  | {readonly kind: 'pause'}
  | {readonly kind: 'resume'}
  | {readonly kind: 'resume-ended'; readonly fromRound: number | null}
  | {readonly kind: 'open-window'}
  | {readonly kind: 'archive'}
  | {readonly kind: 'run-again'};

export interface RunPageStep {
  readonly state: RunPageState;
  readonly requests: readonly RunPageRequest[];
}

export interface RunPageSeed {
  readonly identity: RunRef;
  readonly title: {readonly project: string; readonly task: string | null};
  readonly hostLabel: string;
  readonly unavailable: Unavailable;
}

export function runPageInitial(seed: RunPageSeed): RunPageState {
  return {
    ...seed,
    record: {kind: 'unlisted'},
    tab: 'overview',
    stop: {kind: 'idle'},
    logs: {kind: 'idle'},
    views: {},
    selected: null,
    next: 1,
  };
}

const STOP_IDLE: StopFlow = {kind: 'idle'};

/** The phase the record reports, before the page's own stop is laid over it. */
function recordPhase(record: RunRecord): RunPhase {
  if (record.kind === 'unlisted') return {kind: 'unlisted'};
  const {run} = record;
  if (record.kind === 'ended') return {kind: 'ended', run, end: run.end};
  switch (run.phase) {
    case 'starting':
      return {kind: 'starting'};
    case 'paused':
      return {kind: 'paused', by: run.paused ?? 'user'};
    case 'stopping':
      return {kind: 'stopping'};
    case 'pausing':
      return {kind: 'running', pausing: true};
    default:
      return {kind: 'running', pausing: false};
  }
}

/** The run's phase as the screen shows it. */
export function runPhase(state: RunPageState): RunPhase {
  const phase = recordPhase(state.record);
  if (phase.kind === 'ended' || phase.kind === 'unlisted') return phase;
  return state.stop.kind === 'stopping' ? {kind: 'stopping'} : phase;
}

function isLive(state: RunPageState): boolean {
  return state.record.kind === 'live';
}

/** Arm the force bound for a stop that has none and has not offered force yet. */
function armed(state: RunPageState, forced: boolean): RunPageStep {
  const token = state.next;
  return {
    state: {
      ...state,
      next: token + 1,
      stop: {kind: 'stopping', bound: token, forceOffered: false, forced},
    },
    requests: [{kind: 'arm-bound', token, ms: FORCE_STOP_BOUND_MS}],
  };
}

/** Ask for the stream listing again, unless the app cannot read logs or one is in flight. */
function relisted(state: RunPageState): RunPageStep {
  if (state.unavailable.logs !== undefined || state.logs.kind === 'listing') {
    return {state, requests: []};
  }
  const token = state.next;
  const streams = state.logs.kind === 'listed' ? state.logs.streams : [];
  return {
    state: {...state, next: token + 1, logs: {kind: 'listing', token, streams}},
    requests: [{kind: 'list-logs', token}],
  };
}

function withRecord(state: RunPageState, record: RunRecord): RunPageStep {
  const next: RunPageState = {...state, record};
  if (record.kind !== 'live') {
    // The run ended (or left the listing): no stop is pending and no force is offered.
    const settled = {...next, stop: STOP_IDLE};
    return record.kind === 'ended' ? relisted(settled) : {state: settled, requests: []};
  }
  const listing = relisted(next);
  // A stop sent by anyone (this page, another window, the CLI) gets the same bound.
  if (record.run.phase === 'stopping' && listing.state.stop.kind !== 'stopping') {
    const bound = armed(listing.state, false);
    return {state: bound.state, requests: [...listing.requests, ...bound.requests]};
  }
  return listing;
}

function withStreams(state: RunPageState, token: number, outcome: LogListOutcome): RunPageStep {
  if (state.logs.kind !== 'listing' || state.logs.token !== token) return {state, requests: []};
  if (!outcome.ok) {
    const {failure} = outcome;
    return {
      state: {
        ...state,
        logs: {
          kind: 'failed',
          message: failure.message,
          unsupported: failure.code === 'feature_not_negotiated',
        },
      },
      requests: [],
    };
  }
  return {state: {...state, logs: {kind: 'listed', streams: outcome.streams}}, requests: []};
}

function unchanged(state: RunPageState): RunPageStep {
  return {state, requests: []};
}

/** Asking to stop, cancelling, and confirming; the confirmation states what is lost. */
function confirmStep(state: RunPageState, action: RunPageAction): RunPageStep | null {
  const phase = runPhase(state);
  const stoppable =
    phase.kind === 'starting' || phase.kind === 'running' || phase.kind === 'paused';
  switch (action.type) {
    case 'ask-stop':
      return stoppable && state.stop.kind === 'idle'
        ? {state: {...state, stop: {kind: 'confirming'}}, requests: []}
        : unchanged(state);
    case 'cancel-stop':
      return state.stop.kind === 'confirming'
        ? {state: {...state, stop: STOP_IDLE}, requests: []}
        : unchanged(state);
    case 'confirm-stop': {
      if (state.stop.kind !== 'confirming' || !isLive(state)) return unchanged(state);
      const bound = armed(state, false);
      return {state: bound.state, requests: [{kind: 'stop', force: false}, ...bound.requests]};
    }
    default:
      return null;
  }
}

/** A stop under way: its bound elapsing, a force stop once offered, or the command failing. */
function stoppingStep(state: RunPageState, action: RunPageAction): RunPageStep | null {
  const {stop} = state;
  switch (action.type) {
    case 'force-stop':
      if (stop.kind !== 'stopping' || !stop.forceOffered) return unchanged(state);
      return {
        state: {...state, stop: {kind: 'stopping', bound: null, forceOffered: false, forced: true}},
        requests: [{kind: 'stop', force: true}],
      };
    case 'stop-failed':
      // The stop command could not run: the run goes on as the record says.
      return stop.kind === 'stopping' && state.record.kind === 'live'
        ? {state: {...state, stop: STOP_IDLE}, requests: []}
        : unchanged(state);
    case 'bound-elapsed':
      if (stop.kind !== 'stopping' || stop.bound !== action.token) return unchanged(state);
      return {
        state: {...state, stop: {...stop, bound: null, forceOffered: !stop.forced}},
        requests: [],
      };
    default:
      return null;
  }
}

/** A command the screen offers, sent only when the current state offers it. */
function commandStep(state: RunPageState, action: RunPageAction): RunPageStep | null {
  const phase = runPhase(state);
  const offered = (when: boolean, request: RunPageRequest): RunPageStep => ({
    state,
    requests: when ? [request] : [],
  });
  switch (action.type) {
    case 'pause':
      return offered(
        state.unavailable.pause === undefined && phase.kind === 'running' && !phase.pausing,
        {kind: 'pause'},
      );
    case 'resume':
      return offered(state.unavailable.pause === undefined && phase.kind === 'paused', {
        kind: 'resume',
      });
    case 'resume-ended': {
      const end = phase.kind === 'ended' ? phase.end : null;
      return offered(end?.resumable === true, {
        kind: 'resume-ended',
        fromRound: end?.resume_from_round ?? null,
      });
    }
    case 'open-window':
      return offered(state.unavailable['open-window'] === undefined, {kind: 'open-window'});
    case 'archive':
      return offered(phase.kind === 'ended', {kind: 'archive'});
    case 'run-again':
      return offered(true, {kind: 'run-again'});
    default:
      return null;
  }
}

/** The narration stream, which "View logs" opens; else the first stream. */
function narration(state: RunPageState): string | null {
  const streams =
    state.logs.kind === 'idle' || state.logs.kind === 'failed' ? [] : state.logs.streams;
  const found = streams.find(stream => stream.owner['kind'] === 'run_narration') ?? streams[0];
  return found?.id ?? null;
}

export function runPageStep(state: RunPageState, action: RunPageAction): RunPageStep {
  const stop = confirmStep(state, action) ?? stoppingStep(state, action);
  if (stop !== null) return stop;
  const command = commandStep(state, action);
  if (command !== null) return command;
  switch (action.type) {
    case 'record':
      return withRecord(state, action.record);
    case 'streams':
      return withStreams(state, action.token, action.outcome);
    case 'log-view':
      return {
        state: {...state, views: {...state.views, [action.streamId]: action.view}},
        requests: [],
      };
    case 'tab':
      return {state: {...state, tab: action.tab}, requests: []};
    case 'select-stream':
      return {state: {...state, tab: 'logs', selected: action.streamId}, requests: []};
    case 'view-logs':
      return {state: {...state, tab: 'logs', selected: narration(state)}, requests: []};
    default:
      return {state, requests: []};
  }
}

/** What must hold in every state; empty when all do. */
export function runPageInvariants(state: RunPageState): string[] {
  const problems: string[] = [];
  const phase = runPhase(state);
  if (state.stop.kind === 'stopping' && state.stop.forceOffered && phase.kind !== 'stopping') {
    problems.push('force stop is offered outside stopping');
  }
  if (state.stop.kind === 'stopping' && state.stop.forceOffered && state.stop.bound !== null) {
    problems.push('force stop is offered before its bound elapsed');
  }
  if (state.stop.kind !== 'idle' && state.record.kind !== 'live') {
    problems.push(`a ${state.stop.kind} stop outlives the live run`);
  }
  if (state.unavailable.logs !== undefined && state.logs.kind !== 'idle') {
    problems.push('logs are read although the app cannot read them');
  }
  return problems;
}
