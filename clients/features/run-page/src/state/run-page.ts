/**
 * Pure Run screen state and effects. Pause drains the epoch; Pause now confirms once,
 * records its pending request, and remains pending until durable state catches up.
 */

import type {RunEvent, RunStatus} from '@vibesys/contracts';
import type {LogStream} from '@vibesys/contracts/host-helper';
import type {HostRun, RunEnd} from '@vibesys/hosts';
import type {LogListOutcome, LogView} from '@vibesys/logs';
import {type CoreState, initialCoreState, reduceEventBatch} from '@vibesys/run-state';
import type {RunRef} from '@vibesys/runs';
import {createMachine, getInitialSnapshot, getNextSnapshot, type SnapshotFrom} from 'xstate';
import {type HistoryPage, type HistoryPager, receivePage, startHistory} from './history-pager.js';
import type {IdeasLayout} from './ideas-tree.js';
import {IDEAS_FILTER_DEFAULT, type IdeasFilter} from './ideas-view.js';
import {
  EMPTY_RUN_FOLD,
  heardAfter,
  type RunData,
  type RunFold,
  type RunLink,
  type SessionView,
  traceProblems,
  traceStep,
  withInputGates,
} from './live.js';
import {
  READER_IDLE,
  type ReaderAction,
  type ReaderRequest,
  type ReaderState,
  type ReaderStep,
  readerAttached,
  readerCore,
  readerWanted,
  reconcileReader,
} from './reader.js';
import type {RunSummaryState, SummaryReply} from './run-summary.js';
import {receiveTracePage, startTraceHistory, type TraceHistory} from './trace-history.js';
import {USAGE_CONTROLS_DEFAULT, type UsageControls} from './usage-view.js';

/** When a Resume takes effect: now, or once the provider's capacity returns. */

/** What the app could not do for this run, with why; an action named here is shown disabled. */
type RunCapability = 'open-window' | 'logs';
export type Unavailable = Readonly<Partial<Record<RunCapability, string>>>;

/**
 * Why a run is not in its host's listing: the machine gave no listing (`machine`), or its listing
 * holds rows that may be this run's server (`unconfirmed`), so absence proves nothing.
 */
type UnlistedWhy = 'machine' | 'unconfirmed';

/**
 * How long the page waits for its host's listing to place the run. The bound only guards against
 * a listing that never comes; it is not a timing rule, and a late listing still settles the page.
 */
export const LOCATE_BOUND_MS = 30_000;

/** Whether the page still waits for the listing to place the run, or has stopped waiting. */
type Locate =
  | {readonly kind: 'idle'}
  | {readonly kind: 'waiting'; readonly bound: number}
  | {readonly kind: 'unresolved'};

/** Why a machine's registry cannot be read, so no run on it can be found: it will not answer. */
export type MachineBlock = 'needs-update' | 'sign-in' | 'offline' | 'closed';

/**
 * The run as the host's registry lists it: live, ended, or not (yet) listed. `unlisted` names the
 * machine's `block` when the registry cannot be read for a reason that waiting does not end; it is
 * absent while the machine is only connecting.
 */
export type RunRecord =
  | {readonly kind: 'unlisted'; readonly why: UnlistedWhy; readonly block?: MachineBlock}
  /** A recent run whose server tombstone expired; its journal may still be readable. */
  | {
      readonly kind: 'historical';
      readonly status: Extract<RunStatus, 'stopped' | 'completed' | 'failed'> | null;
    }
  | {readonly kind: 'live'; readonly run: HostRun}
  | {readonly kind: 'ended'; readonly run: HostRun};

function isEndedRecord(record: RunRecord): boolean {
  return record.kind === 'ended' || record.kind === 'historical';
}

/** Whether the run is over (ended, or a saved run whose tombstone expired) rather than live. */
export function runHasEnded(state: RunPageState): boolean {
  return isEndedRecord(state.record);
}

/** The run's place in its lifecycle, as the screen shows it. */
export type RunPhase =
  | {readonly kind: 'unlisted'}
  | {readonly kind: 'historical'}
  | {readonly kind: 'starting'}
  | {readonly kind: 'running'; readonly pausing: boolean}
  | {readonly kind: 'paused'; readonly by: 'user' | 'quota'}
  | {readonly kind: 'stopping'}
  | {readonly kind: 'ended'; readonly run: HostRun; readonly end: RunEnd};

/**
 * The most events one history page asks for. The host cuts a page earlier at its byte bound, and
 * `@vibesys/hosts`' main-process reader asks an older helper for fewer.
 */
export const HISTORY_PAGE_EVENTS = 2000;

/** The sequence a run's journal starts at, where a history read starts (`runs.read_events`). */
export const FIRST_HISTORY_SEQUENCE = 1;

/**
 * How far an ended run's history read has come: the events and bytes folded so far, and the run's
 * total number of events once the host has said it (`HistoryReply.totalEvents`); null until then.
 */
export interface HistoryProgress {
  readonly events: number;
  readonly bytes: number;
  readonly total: number | null;
}

const NO_PROGRESS: HistoryProgress = {events: 0, bytes: 0, total: null};

/**
 * An ended run's recorded events, read (`runs.read_events`) into the run fold with several pages
 * in flight, folded in order (`history-pager.ts`): not asked yet, being read, all read, not served
 * by this host, or failed. While it reads, what is folded is a prefix of the run, never the run:
 * the screen shows the read's progress instead of it (`historyScreen`).
 */
export type RunHistory =
  | {readonly kind: 'idle'}
  | {
      readonly kind: 'reading';
      readonly pager: HistoryPager;
      readonly progress: HistoryProgress;
      /**
       * The fold of the events read so far. It replaces the run's core only when the history is
       * all read: a prefix of the transcript is no run's, and a summary-seeded core (which holds
       * the turns' last replies) would double its replies if the history folded onto it.
       */
      readonly core: CoreState | null;
    }
  | {readonly kind: 'read'}
  | {readonly kind: 'unsupported'}
  | {readonly kind: 'failed'; readonly message: string};

/** What one history read settled to. */
export type HistoryReply =
  | ({readonly kind: 'page'} & HistoryPage)
  | {readonly kind: 'unsupported'}
  | {readonly kind: 'failed'; readonly message: string};

/** A bounded read of the opened step's transcript, separate from the run-level summary. */
type SpanDetail =
  | {readonly kind: 'idle'}
  | {
      readonly kind: 'reading';
      readonly spanId: string;
      readonly token: number;
      readonly from: number;
      readonly events: readonly RunEvent[];
      readonly total: number;
    }
  | {readonly kind: 'ready'; readonly spanId: string; readonly core: CoreState}
  | {readonly kind: 'failed'; readonly spanId: string; readonly message: string};

export type SpanDetailReply =
  | {
      readonly kind: 'page';
      readonly events: readonly RunEvent[];
      readonly next: number;
      readonly complete: boolean;
      readonly total: number;
    }
  | {readonly kind: 'unsupported'}
  | {readonly kind: 'failed'; readonly message: string};

/** Confirmation for cancellation; the backend record remains the lifecycle authority. */
const pauseNowMachine = createMachine({
  types: {} as {events: {type: 'ask' | 'cancel' | 'confirm' | 'failed'; allowed: boolean}},
  initial: 'idle',
  on: {failed: '.idle'},
  states: {
    idle: {on: {ask: {target: 'confirming', guard: ({event}) => event.allowed}}},
    confirming: {
      on: {
        cancel: 'idle',
        confirm: {target: 'sent', guard: ({event}) => event.allowed},
      },
    },
    sent: {},
  },
});

type PauseNowFlow = {readonly kind: SnapshotFrom<typeof pauseNowMachine>['value']};

function pauseNowNext(
  state: PauseNowFlow,
  type: 'ask' | 'cancel' | 'confirm' | 'failed',
  allowed = true,
): PauseNowFlow {
  return {
    kind: getNextSnapshot(pauseNowMachine, pauseNowMachine.resolveState({value: state.kind}), {
      type,
      allowed,
    }).value,
  };
}

/** Where the scrubber stands: following the newest event, or at one event's sequence. */
export type RunView = {readonly at: 'live'} | {readonly at: number};

type RunLogs =
  | {readonly kind: 'idle'}
  | {
      readonly kind: 'listing';
      readonly token: number;
      readonly instanceId: string;
      readonly streams: readonly LogStream[];
    }
  | {readonly kind: 'listed'; readonly instanceId: string; readonly streams: readonly LogStream[]}
  | {readonly kind: 'failed'; readonly message: string; readonly unsupported: boolean};

export interface StageView {
  readonly id: string;
  readonly state: 'done' | 'active' | 'waiting' | 'failed';
  readonly label: string;
  readonly streamId: string | null;
}

export interface RunPageState {
  readonly startupStages?: readonly StageView[];
  readonly readOnly?: boolean;
  readonly lifecycleWord?: string | undefined;
  readonly positionWord?: string | undefined;
  readonly identity: RunRef;
  /** What the sidebar knows about the run's name: its project and task. */
  readonly title: {readonly project: string; readonly task: string | null};
  readonly hostLabel: string;
  readonly unavailable: Unavailable;
  readonly record: RunRecord;
  /** The wait for the record to leave `unlisted`. */
  readonly locate: Locate;
  /**
   * Whether the page follows the selected stream's lines: set once a Logs face, or View logs,
   * picks a stream.
   */
  readonly followingLogs: boolean;
  readonly pauseNow: PauseNowFlow;
  readonly logs: RunLogs;
  /** The latest view of each followed stream, by stream id. */
  readonly views: Readonly<Record<string, LogView>>;
  /** The stream the Logs face shows; null shows the first one. */
  readonly selected: string | null;
  /** Which ideas the Ideas face shows. */
  readonly ideas: IdeasFilter;
  /** The Usage face's controls. */
  readonly usage: UsageControls;
  /** The Ideas face's layout; null until the reader picks one (the face then picks by the run). */
  readonly ideasLayout: IdeasLayout | null;
  /** What the reader opened in the ideas tree (`ideaTree`'s keys). */
  readonly ideasOpen: ReadonlySet<string>;
  /** The idea the reader pinned (the header line, a pick): its ancestry is open and lit. */
  readonly ideasPinned: string | null;
  /** The idea the pointer or focus is on: its ancestry lights, nothing moves. */
  readonly ideasHovered: string | null;
  /**
   * The agents whose finished agents the Agents list shows rather than folds, by the parent's id.
   * The page keeps them, so a group stays open while the user opens agents elsewhere in the tree.
   */
  readonly agentsOpen: ReadonlySet<string>;
  /** The window's hold on the live run. */
  readonly link: RunLink;
  /** What the attached run's session reports; null while none runs. */
  readonly session: SessionView | null;
  /** The window's hold on the server Chat reads this run through while it has ended. */
  readonly reader: ReaderState;
  /** The run fold and trace so far. */
  readonly run: RunFold;
  /** This window's clock (epoch ms) at its latest tick; null before the first one. */
  readonly clockMs: number | null;
  /** The sequence the screen shows: the live end, or a past one the scrubber chose. */
  readonly view: RunView;
  /** Why the last command the run refused failed, until the next one. */
  readonly notice: string | null;
  /** The run's summary, which opens the run level (`run-summary.ts`). */
  readonly summary: RunSummaryState;
  /** A pane asked for the transcript, even if the summary has not arrived yet. */
  readonly wantsHistory: boolean;
  /** An ended run's recorded events, read into `run` for its transcript. */
  readonly history: RunHistory;
  /** An ended run's earlier span events, read into its trace so the scrubber has positions. */
  readonly traceHistory: TraceHistory;
  readonly detail: SpanDetail;
  readonly next: number;
}

export type RunPageAction =
  | {readonly type: 'record'; readonly record: RunRecord}
  | {readonly type: 'select-stream'; readonly streamId: string}
  | {readonly type: 'ideas-filter'; readonly filter: IdeasFilter}
  | {readonly type: 'usage-controls'; readonly controls: UsageControls}
  | {readonly type: 'ideas-layout'; readonly layout: IdeasLayout}
  | {readonly type: 'ideas-open'; readonly key: string; readonly open: boolean}
  | {readonly type: 'ideas-pin'; readonly idea: string | null}
  | {readonly type: 'ideas-hover'; readonly idea: string | null}
  | {readonly type: 'agents-open'; readonly id: string; readonly open: boolean}
  | {readonly type: 'view-logs'}
  | {readonly type: 'ask-pause-now'}
  | {readonly type: 'cancel-pause-now'}
  | {readonly type: 'confirm-pause-now'}
  | {readonly type: 'pause-now-failed'}
  | {readonly type: 'bound-elapsed'; readonly token: number}
  /** The page opened: start waiting for the listing to place the run. */
  | {readonly type: 'locate'}
  | {readonly type: 'streams'; readonly token: number; readonly outcome: LogListOutcome}
  | {readonly type: 'log-view'; readonly streamId: string; readonly view: LogView}
  | {readonly type: 'pause'}
  | {readonly type: 'resume-ended'}
  | {readonly type: 'open-window'}
  | {readonly type: 'archive'}
  | {readonly type: 'run-again'}
  | {readonly type: 'attached'; readonly token: number; readonly error: string | null}
  | ReaderAction
  | {readonly type: 'session'; readonly session: SessionView | null}
  | {readonly type: 'run-core'; readonly core: CoreState}
  /** The run's spans changed: `data` arrived at `wallMs` on this window's clock. */
  | {readonly type: 'run-data'; readonly data: RunData; readonly wallMs: number}
  /** This window's clock, ticked while the screen shows time passing on a live run. */
  | {readonly type: 'tick'; readonly wallMs: number}
  | {readonly type: 'command-refused'; readonly message: string}
  | {readonly type: 'view'; readonly view: RunView}
  | {readonly type: 'history'; readonly token: number; readonly reply: HistoryReply}
  /** A page of the ended run's earlier span events arrived (or did not). */
  | {readonly type: 'trace-page'; readonly token: number; readonly reply: HistoryReply}
  /** The run's summary arrived (or did not) at `wallMs` on this window's clock. */
  | {
      readonly type: 'summary';
      readonly token: number;
      readonly reply: SummaryReply;
      readonly wallMs: number;
    }
  /** A pane needs the run's transcript, which only its history holds: read it. */
  | {readonly type: 'need-history'}
  | {readonly type: 'need-span'; readonly spanId: string}
  | {readonly type: 'retry-load'}
  | {readonly type: 'span'; readonly token: number; readonly reply: SpanDetailReply};

export type RunPageRequest =
  | {readonly kind: 'pause-now'}
  | {readonly kind: 'arm-bound'; readonly token: number; readonly ms: number}
  /** Cancel the bound armed under `token`, when its wait ended first. */
  | {readonly kind: 'cancel-bound'; readonly token: number}
  | {readonly kind: 'list-logs'; readonly token: number; readonly instanceId: string}
  | {readonly kind: 'pause'}
  | {readonly kind: 'resume-ended'; readonly fromRound: number | null}
  | {readonly kind: 'open-window'}
  | {readonly kind: 'archive'}
  | {readonly kind: 'run-again'}
  /** Attach the live run's server `instance` to this window, answering `attached` with `token`. */
  | {readonly kind: 'attach'; readonly token: number; readonly instance: string}
  /** Let the attached run go (its session closes with it). */
  | {readonly kind: 'release'}
  | ReaderRequest
  /** Read the run's summary, answering `summary` with `token`. */
  | {readonly kind: 'read-summary'; readonly token: number; readonly status: RunStatus}
  /** Read the ended run's recorded events from `from`, answering `history` with `token`. */
  | {
      readonly kind: 'read-history';
      readonly token: number;
      readonly from: number;
      readonly max: number;
    }
  /** Read a page of the ended run's events and keep its span events, answering `trace-page` with `token`. */
  | {
      readonly kind: 'read-trace';
      readonly token: number;
      readonly from: number;
      readonly max: number;
    }
  | {
      readonly kind: 'read-span';
      readonly token: number;
      readonly spanId: string;
      readonly from: number;
      readonly max: number;
    };

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
    record: {kind: 'unlisted', why: 'machine'},
    locate: {kind: 'idle'},
    followingLogs: false,
    pauseNow: PAUSE_NOW_IDLE,
    logs: {kind: 'idle'},
    views: {},
    selected: null,
    ideas: IDEAS_FILTER_DEFAULT,
    usage: USAGE_CONTROLS_DEFAULT,
    ideasLayout: null,
    ideasOpen: new Set(),
    ideasPinned: null,
    ideasHovered: null,
    agentsOpen: new Set(),
    link: {kind: 'none'},
    session: null,
    reader: READER_IDLE,
    run: EMPTY_RUN_FOLD,
    clockMs: null,
    view: {at: 'live'},
    notice: null,
    summary: {kind: 'idle'},
    wantsHistory: false,
    history: {kind: 'idle'},
    traceHistory: {kind: 'idle'},
    detail: {kind: 'idle'},
    next: 1,
  };
}

const PAUSE_NOW_IDLE: PauseNowFlow = {kind: getInitialSnapshot(pauseNowMachine).value};

/** The phase the record reports, before the page's own stop is laid over it. */
function recordPhase(record: RunRecord): RunPhase {
  if (record.kind === 'unlisted') return {kind: 'unlisted'};
  if (record.kind === 'historical') return {kind: 'historical'};
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

/**
 * The run clock's time now: where the run's newest event put it (`Heard`), carried forward by
 * this window's clock since the window heard it. Null at a past view or without a live attachment,
 * where the latest time the spans carry is the run's time (`latestTime`).
 */
export function runNow(state: RunPageState): number | null {
  const {heard} = state.run;
  if (heard === null || state.view.at !== 'live' || state.link.kind !== 'attached') return null;
  return heard.run + Math.max(0, (state.clockMs ?? heard.wallMs) - heard.wallMs) / 1000;
}

/**
 * The host's wall time (epoch seconds) at the run clock's time now, for the clock the scrubber
 * shows: the run clock mapped onto the events' timestamps, or the run clock itself while no event
 * has told how they differ. Null where `runNow` is.
 */
export function runWallNow(state: RunPageState): number | null {
  const now = runNow(state);
  return now === null ? null : now + (state.run.heard?.offset?.seconds ?? 0);
}

/** The run's phase as the screen shows it. */
export function runPhase(state: RunPageState): RunPhase {
  const phase = recordPhase(state.record);
  if (phase.kind === 'ended' || phase.kind === 'unlisted' || phase.kind === 'historical')
    return phase;
  return state.pauseNow.kind === 'sent' ? {kind: 'stopping'} : phase;
}

function isLive(state: RunPageState): boolean {
  return state.record.kind === 'live';
}

/** Ask for the stream listing again, unless the app cannot read logs or one is in flight. */
function relisted(state: RunPageState): RunPageStep {
  const instanceId = recordInstance(state.record);
  if (
    state.unavailable.logs !== undefined ||
    instanceId === null ||
    (state.logs.kind === 'listing' && state.logs.instanceId === instanceId)
  ) {
    return {state, requests: []};
  }
  const token = state.next;
  const streams =
    state.logs.kind === 'listed' && state.logs.instanceId === instanceId ? state.logs.streams : [];
  return {
    state: {...state, next: token + 1, logs: {kind: 'listing', token, instanceId, streams}},
    requests: [{kind: 'list-logs', token, instanceId}],
  };
}

function recordInstance(record: RunRecord): string | null {
  return record.kind === 'unlisted' || record.kind === 'historical' ? null : record.run.id;
}

/**
 * The link the record calls for: a live run's server attached to this window, nothing otherwise.
 * A new server (a resume, a restart) is attached afresh; leaving a live run releases it.
 */
function linked(state: RunPageState): RunPageStep {
  const {record, link} = state;
  const wanted = record.kind === 'live' ? record.run.id : null;
  const held = link.kind === 'none' ? null : link.instance;
  if (wanted === held) return {state, requests: []};
  const release: RunPageRequest[] = link.kind === 'none' ? [] : [{kind: 'release'}];
  if (wanted === null) {
    return {state: {...state, link: {kind: 'none'}, session: null}, requests: release};
  }
  const token = state.next;
  return {
    state: {
      ...state,
      next: token + 1,
      link: {kind: 'attaching', token, instance: wanted},
      session: null,
      run: EMPTY_RUN_FOLD,
    },
    requests: [...release, {kind: 'attach', token, instance: wanted}],
  };
}

/** Read the history from its first event. */
function startReading(state: RunPageState): RunPageStep {
  const started = startHistory(FIRST_HISTORY_SEQUENCE, HISTORY_PAGE_EVENTS, state.next);
  return {
    state: {
      ...state,
      next: started.nextToken,
      // The history read feeds the trace every event in order, so the trace read has no use.
      traceHistory: {kind: 'done'},
      history: {kind: 'reading', pager: started.pager, progress: NO_PROGRESS, core: null},
    },
    requests: started.requests.map(asked => ({kind: 'read-history', ...asked})),
  };
}

/** The tokens of the history pages asked and not yet answered. */
export function historyAwaiting(history: RunHistory): readonly number[] {
  if (history.kind !== 'reading') return [];
  return history.pager.spans.flatMap(span =>
    span.token !== null && span.page === null ? [span.token] : [],
  );
}

/** `state` with a read of the trace's earlier events let go: a late page for it is dropped. */
function withoutTraceRead(state: RunPageState): RunPageState {
  return state.traceHistory.kind === 'reading' ? {...state, traceHistory: {kind: 'idle'}} : state;
}

/** Whether the run fold holds anything yet: a session's snapshot or events, or a summary. */
function folded(state: RunPageState): boolean {
  return state.run.core !== null || state.run.trace !== EMPTY_RUN_FOLD.trace;
}

/** Ask for the run's summary, which opens the run level. */
function startSummary(state: RunPageState, status: RunStatus): RunPageStep {
  const token = state.next;
  return {
    state: {...state, next: token + 1, summary: {kind: 'reading', token}},
    requests: [{kind: 'read-summary', token, status}],
  };
}

/**
 * A run opened with nothing folded asks for its summary, which holds the run level whole. A live
 * run takes it for its first paint until its session's snapshot arrives; an ended run's history (its
 * transcript) is read only when a pane needs it (`need-history`), or at once when its host serves
 * no summary. A run watched to its end already holds what its session folded. A run that is live
 * again (a resume) gets its data from its session, and drops a history read in flight.
 */
function historyFor(state: RunPageState): RunPageStep {
  switch (state.record.kind) {
    case 'live': {
      const idle: RunHistory = {kind: 'idle'};
      const cleared: RunPageState = withoutTraceRead(
        state.history.kind === 'idle' ? state : {...state, history: idle},
      );
      // Asked even when the session folded the run first: only the summary knows the gates that
      // failed before the first idea.
      return cleared.summary.kind === 'idle'
        ? startSummary(cleared, state.record.run.phase)
        : {state: cleared, requests: []};
    }
    case 'unlisted':
      return state.history.kind === 'reading'
        ? {state: withoutTraceRead({...state, history: {kind: 'idle'}}), requests: []}
        : {state: withoutTraceRead(state), requests: []};
    case 'historical':
      // The tombstone no longer supplies a terminal status. The header stays neutral; this
      // fallback only fills the snapshot contract until a persisted status is available.
      return state.history.kind === 'idle'
        ? endedHistoryFor(state, state.record.status ?? 'stopped')
        : {state, requests: []};
    case 'ended':
      return state.history.kind === 'idle'
        ? endedHistoryFor(state, state.record.run.phase)
        : {state, requests: []};
  }
}

/** What an ended run whose history is not asked yet needs first: its summary, or its history. */
function endedHistoryFor(state: RunPageState, status: RunStatus): RunPageStep {
  const read: RunPageStep = {state: {...state, history: {kind: 'read'}}, requests: []};
  switch (state.summary.kind) {
    case 'ready':
      return state.wantsHistory ? startReading(state) : {state, requests: []};
    case 'reading':
      // A session that watched the run to its end folded it: the summary's answer is not needed.
      return folded(state)
        ? {state: {...read.state, summary: {kind: 'superseded'}}, requests: []}
        : {state, requests: []};
    case 'idle':
      return folded(state) ? read : startSummary(state, status);
    case 'unsupported':
      return folded(state) ? read : startReading(state);
    case 'failed':
      return folded(state) ? read : startReading(state);
    default:
      return {state, requests: []};
  }
}

function withRecord(opened: RunPageState, record: RunRecord): RunPageStep {
  // A run that is going again (a resume) is not gone, whatever its host once said of it.
  const state: RunPageState =
    opened.summary.kind === 'gone' && !isEndedRecord(record)
      ? {...opened, summary: {kind: 'idle'}}
      : opened;
  const instanceChanged = recordInstance(state.record) !== recordInstance(record);
  const fenced: RunPageState = instanceChanged
    ? {...state, record, logs: {kind: 'idle'}, views: {}, selected: null, detail: {kind: 'idle'}}
    : record.kind === 'live'
      ? {...state, record, detail: {kind: 'idle'}}
      : {...state, record};
  // The reader is let go before a live run is attached, which takes the window's binding over.
  const reading = withReader(fenced, isEndedRecord(record), reconcileReader);
  const link = linked(reading.state);
  const step = recordStep(link.state, record);
  const history = historyFor(step.state);
  const located = locating(history.state);
  return {
    state: located.state,
    requests: [
      ...reading.requests,
      ...link.requests,
      ...step.requests,
      ...history.requests,
      ...located.requests,
    ],
  };
}

/**
 * Wait for the listing to place a run it does not list yet, with a bound against a listing that
 * never comes; stop waiting once the run is listed.
 */
function locating(state: RunPageState): RunPageStep {
  if (state.record.kind !== 'unlisted') {
    if (state.locate.kind === 'idle') return {state, requests: []};
    const cancel: RunPageRequest[] =
      state.locate.kind === 'waiting' ? [{kind: 'cancel-bound', token: state.locate.bound}] : [];
    return {state: {...state, locate: {kind: 'idle'}}, requests: cancel};
  }
  if (state.locate.kind !== 'idle') return {state, requests: []};
  const token = state.next;
  return {
    state: {...state, next: token + 1, locate: {kind: 'waiting', bound: token}},
    requests: [{kind: 'arm-bound', token, ms: LOCATE_BOUND_MS}],
  };
}

function withHistory(state: RunPageState, token: number, reply: HistoryReply): RunPageStep {
  const {history} = state;
  if (history.kind !== 'reading') return {state, requests: []};
  // A run opened from its summary keeps its run level when the transcript cannot be read; one
  // opened from its history has only a prefix of the run to drop.
  const kept = state.summary.kind === 'ready' ? state.run : EMPTY_RUN_FOLD;
  if (reply.kind === 'unsupported') {
    return {state: {...state, run: kept, history: reply}, requests: []};
  }
  if (reply.kind === 'failed') {
    return {
      state: {...state, run: kept, history: {kind: 'failed', message: reply.message}},
      requests: [],
    };
  }
  const step = receivePage(history.pager, token, reply, HISTORY_PAGE_EVENTS, state.next);
  // A reply for a reading that is over (or one page twice) is dropped.
  if (step === null) return {state, requests: []};
  const core =
    step.events.length === 0
      ? history.core
      : reduceEventBatch(history.core ?? initialCoreState(), step.events);
  // The events are the journal's, so a trace already seeded from the summary skips them; one that
  // was not (an older host) grows with them.
  const trace =
    step.events.length === 0
      ? state.run.trace
      : traceStep(state.run.trace, {kind: 'events', events: step.events});
  const before = history.progress;
  const progress: HistoryProgress = {
    events: before.events + step.events.length,
    bytes: before.bytes + (reply.bytes ?? 0),
    total: reply.totalEvents ?? before.total,
  };
  return {
    state: {
      ...state,
      run: step.done ? {...state.run, trace, core} : {...state.run, trace},
      next: step.nextToken,
      history: step.done ? {kind: 'read'} : {kind: 'reading', pager: step.pager, progress, core},
    },
    requests: step.requests.map(asked => ({kind: 'read-history', ...asked})),
  };
}

/**
 * The state when the summary arrives after the page's own data: that data stands. A live session's
 * snapshot knows nothing of the gates that failed before the first idea, which the summary does, so
 * those join the trace.
 */
function supersededByData(state: RunPageState, gates: readonly RunEvent[]): RunPageState {
  const run = isLive(state)
    ? {...state.run, trace: withInputGates(state.run.trace, gates)}
    : state.run;
  return {...state, run, summary: {kind: 'superseded'}};
}

/** The run's summary arrived: seed the run fold from it, unless something else already did. */
function withSummary(
  state: RunPageState,
  token: number,
  reply: SummaryReply,
  wallMs: number,
): RunPageStep {
  const {summary} = state;
  if (summary.kind !== 'reading' || summary.token !== token) return {state, requests: []};
  if (reply.kind === 'unsupported') {
    const unsupported = {...state, summary: reply};
    return isEndedRecord(state.record) && state.history.kind === 'idle'
      ? startReading(unsupported)
      : {state: unsupported, requests: []};
  }
  if (reply.kind === 'gone')
    return {state: {...state, summary: summaryOfGone(state)}, requests: []};
  if (reply.kind === 'failed') {
    // A live run has its session; an ended one can still read the full history.
    const failed = {...state, summary: reply};
    return isEndedRecord(state.record) && state.history.kind === 'idle'
      ? startReading(failed)
      : {state: failed, requests: []};
  }
  if (folded(state)) return {state: supersededByData(state, reply.gates), requests: []};
  const data: RunData = {kind: 'snapshot', snapshot: reply.snapshot, gates: reply.gates};
  const ready: RunPageState = {
    ...state,
    summary: {
      kind: 'ready',
      through: reply.through,
      eventCount: reply.eventCount,
      journalBytes: reply.journalBytes,
      trace: reply.trace,
      roundsFinished: reply.roundsFinished,
      records: reply.records,
      terminal: reply.terminal,
    },
    run: {
      core: reduceEventBatch(initialCoreState(), reply.seed),
      trace: traceStep(state.run.trace, data),
      heard: heardAfter(state.run.heard, data, wallMs),
    },
  };
  if (!isEndedRecord(ready.record)) return {state: ready, requests: []};
  return ready.wantsHistory ? startReading(ready) : startTraceRead(ready, reply.through);
}

/**
 * An ended run opened from its summary has one position to scrub to: read the span events before
 * it, one page at a time (`trace-history.ts`), unless the history read does it.
 */
function startTraceRead(state: RunPageState, through: number): RunPageStep {
  if (state.traceHistory.kind !== 'idle' || state.history.kind === 'reading') {
    return {state, requests: []};
  }
  const token = state.next;
  const started = startTraceHistory(through, HISTORY_PAGE_EVENTS, token);
  return {
    state: {...state, next: token + 1, traceHistory: started.load},
    requests: started.request === null ? [] : [{kind: 'read-trace', ...started.request}],
  };
}

/** A page of the trace's earlier span events arrived: fold it, and ask for the next. */
function withTracePage(state: RunPageState, token: number, reply: HistoryReply): RunPageStep {
  const step = receiveTracePage(
    state.traceHistory,
    state.run.trace,
    token,
    reply,
    HISTORY_PAGE_EVENTS,
    state.next,
  );
  // A reply for a read that is over (or one page twice) is dropped.
  if (step === null) return {state, requests: []};
  return {
    state: {
      ...state,
      next: step.nextToken,
      run: step.trace === state.run.trace ? state.run : {...state.run, trace: step.trace},
      traceHistory: step.load,
    },
    requests: step.request === null ? [] : [{kind: 'read-trace', ...step.request}],
  };
}

/**
 * What a host's "no record of this run" settles the summary to. It is a fact only about an ended run
 * nothing else has folded: a run still going has no journal on its host until its first events
 * land, and a run its session folded needs no summary, so neither is gone, and the summary is asked
 * again if the run ends with nothing folded.
 */
function summaryOfGone(state: RunPageState): RunSummaryState {
  if (folded(state)) return {kind: 'superseded'};
  return isEndedRecord(state.record) ? {kind: 'gone'} : {kind: 'idle'};
}

/** A pane needs the transcript: an ended run opened from its summary reads its history now. */
function withNeedHistory(state: RunPageState): RunPageStep {
  const wanted = state.wantsHistory ? state : {...state, wantsHistory: true};
  if (!isEndedRecord(wanted.record) || wanted.summary.kind !== 'ready') {
    return {state: wanted, requests: []};
  }
  return wanted.history.kind === 'idle' ? startReading(wanted) : {state: wanted, requests: []};
}

/** Read only the opened step's transcript. A legacy host takes the full-history path. */
function withNeedSpan(state: RunPageState, spanId: string): RunPageStep {
  if (!isEndedRecord(state.record) || state.summary.kind !== 'ready') {
    return {state, requests: []};
  }
  if (state.history.kind !== 'idle') return {state, requests: []};
  if (
    state.detail.kind !== 'idle' &&
    state.detail.kind !== 'failed' &&
    state.detail.spanId === spanId
  ) {
    return {state, requests: []};
  }
  const token = state.next;
  return {
    state: {
      ...state,
      next: token + 1,
      detail: {kind: 'reading', spanId, token, from: FIRST_HISTORY_SEQUENCE, events: [], total: 0},
    },
    requests: [
      {kind: 'read-span', token, spanId, from: FIRST_HISTORY_SEQUENCE, max: HISTORY_PAGE_EVENTS},
    ],
  };
}

function withSpan(state: RunPageState, token: number, reply: SpanDetailReply): RunPageStep {
  const {detail} = state;
  if (detail.kind !== 'reading' || detail.token !== token) return {state, requests: []};
  if (reply.kind === 'unsupported') return withNeedHistory({...state, detail: {kind: 'idle'}});
  if (reply.kind === 'failed') {
    return {
      state: {...state, detail: {kind: 'failed', spanId: detail.spanId, message: reply.message}},
      requests: [],
    };
  }
  const events = [...detail.events, ...reply.events];
  if (reply.complete) {
    return {
      state: {
        ...state,
        detail: {
          kind: 'ready',
          spanId: detail.spanId,
          core: reduceEventBatch(initialCoreState(), events),
        },
      },
      requests: [],
    };
  }
  if (reply.next <= detail.from || reply.events.length === 0) {
    return {
      state: {
        ...state,
        detail: {kind: 'failed', spanId: detail.spanId, message: 'the span page made no progress'},
      },
      requests: [],
    };
  }
  const nextToken = state.next;
  return {
    state: {
      ...state,
      next: nextToken + 1,
      detail: {
        ...detail,
        token: nextToken,
        from: reply.next,
        events,
        total: reply.total,
      },
    },
    requests: [
      {
        kind: 'read-span',
        token: nextToken,
        spanId: detail.spanId,
        from: reply.next,
        max: HISTORY_PAGE_EVENTS,
      },
    ],
  };
}

function recordStep(next: RunPageState, record: RunRecord): RunPageStep {
  if (record.kind !== 'live') {
    // The run ended (or left the listing): no stop is pending and no force is offered.
    const settled = {...next, pauseNow: PAUSE_NOW_IDLE};
    return isEndedRecord(record) ? relisted(settled) : {state: settled, requests: []};
  }
  const listing = relisted(next);
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
  if (outcome.instanceId !== state.logs.instanceId) {
    return {
      state: {
        ...state,
        logs: {
          kind: 'failed',
          message: `the app listed logs for ${outcome.instanceId}, not ${state.logs.instanceId}`,
          unsupported: false,
        },
      },
      requests: [],
    };
  }
  return {
    state: {
      ...state,
      logs: {kind: 'listed', instanceId: outcome.instanceId, streams: outcome.streams},
    },
    requests: [],
  };
}

function unchanged(state: RunPageState): RunPageStep {
  return {state, requests: []};
}

/** Pause now is available while Pause drains and cancels through the live session. */
function confirmStep(state: RunPageState, action: RunPageAction): RunPageStep | null {
  const phase = runPhase(state);
  switch (action.type) {
    case 'ask-pause-now':
      return {
        state: {
          ...state,
          pauseNow: pauseNowNext(state.pauseNow, 'ask', isLive(state) && phase.kind !== 'ended'),
        },
        requests: [],
      };
    case 'cancel-pause-now':
      return {state: {...state, pauseNow: pauseNowNext(state.pauseNow, 'cancel')}, requests: []};
    case 'confirm-pause-now': {
      const pauseNow = pauseNowNext(state.pauseNow, 'confirm', commandable(state));
      return {
        state: {...state, pauseNow},
        requests:
          state.pauseNow.kind === 'confirming' && pauseNow.kind === 'sent'
            ? [{kind: 'pause-now'}]
            : [],
      };
    }
    case 'pause-now-failed':
      return {state: {...state, pauseNow: pauseNowNext(state.pauseNow, 'failed')}, requests: []};
    case 'bound-elapsed':
      return state.locate.kind === 'waiting' && state.locate.bound === action.token
        ? {state: {...state, locate: {kind: 'unresolved'}}, requests: []}
        : unchanged(state);
    default:
      return null;
  }
}

/** Whether a live pause command can reach the run now: its session's controls are up. */
export function commandable(state: RunPageState): boolean {
  return state.link.kind === 'attached' && state.session?.controls === true;
}

/** A command the screen offers, sent only when the current state offers it. */
function commandStep(state: RunPageState, action: RunPageAction): RunPageStep | null {
  const phase = runPhase(state);
  // A command sent is a new attempt: what the last one reported no longer stands.
  const offered = (when: boolean, request: RunPageRequest): RunPageStep => ({
    state: when ? {...state, notice: null} : state,
    requests: when ? [request] : [],
  });
  switch (action.type) {
    case 'pause':
      return offered(commandable(state) && phase.kind === 'running' && !phase.pausing, {
        kind: 'pause',
      });
    case 'resume-ended': {
      const end = phase.kind === 'ended' ? phase.end : null;
      return offered(phase.kind === 'ended' || phase.kind === 'historical', {
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

function toggled(
  open: ReadonlySet<string>,
  action: {readonly id: string; readonly open: boolean},
): ReadonlySet<string> {
  if (open.has(action.id) === action.open) return open;
  const next = new Set(open);
  if (action.open) next.add(action.id);
  else next.delete(action.id);
  return next;
}

export function runPageStep(state: RunPageState, action: RunPageAction): RunPageStep {
  const cancellation = confirmStep(state, action);
  if (cancellation !== null) return cancellation;
  const command = commandStep(state, action);
  if (command !== null) return command;
  const live = liveStep(state, action);
  if (live !== null) return live;
  switch (action.type) {
    case 'record':
      return withRecord(state, action.record);
    case 'locate':
      return locating(state);
    case 'streams':
      return withStreams(state, action.token, action.outcome);
    case 'log-view':
      return {
        state: {...state, views: {...state.views, [action.streamId]: action.view}},
        requests: [],
      };
    case 'select-stream':
      return {state: {...state, followingLogs: true, selected: action.streamId}, requests: []};
    case 'ideas-filter':
      return {state: {...state, ideas: action.filter}, requests: []};
    case 'usage-controls':
      return {state: {...state, usage: action.controls}, requests: []};
    case 'ideas-layout':
      return {state: {...state, ideasLayout: action.layout}, requests: []};
    case 'ideas-open':
      return {
        state: {
          ...state,
          ideasOpen: toggled(state.ideasOpen, {id: action.key, open: action.open}),
        },
        requests: [],
      };
    case 'ideas-pin':
      return {state: {...state, ideasPinned: action.idea}, requests: []};
    case 'ideas-hover':
      return state.ideasHovered === action.idea
        ? {state, requests: []}
        : {state: {...state, ideasHovered: action.idea}, requests: []};
    case 'agents-open':
      return {state: {...state, agentsOpen: toggled(state.agentsOpen, action)}, requests: []};
    case 'view-logs':
      return {state: {...state, followingLogs: true, selected: narration(state)}, requests: []};
    case 'chat':
      return withReader(state, isEndedRecord(state.record), (reader, ended, next) =>
        readerWanted(reader, action.open, ended, next),
      );
    case 'reader-attached':
      return withReader(state, isEndedRecord(state.record), (reader, _ended, next) =>
        readerAttached(reader, action.token, action.reply, next),
      );
    case 'reader-core':
      return {state: {...state, reader: readerCore(state.reader, action.core)}, requests: []};
    default:
      return {state, requests: []};
  }
}

/** Apply one step of the reader to the page, taking and returning the page's request tokens. */
function withReader(
  state: RunPageState,
  ended: boolean,
  step: (reader: ReaderState, ended: boolean, next: number) => ReaderStep,
): RunPageStep {
  const result = step(state.reader, ended, state.next);
  return {
    state: {...state, reader: result.reader, next: result.next},
    requests: result.requests,
  };
}

function withAttached(state: RunPageState, token: number, error: string | null): RunPageStep {
  const {link} = state;
  if (link.kind !== 'attaching' || link.token !== token) return {state, requests: []};
  const next: RunLink =
    error === null
      ? {kind: 'attached', instance: link.instance}
      : {kind: 'failed', instance: link.instance, message: error};
  return {state: {...state, link: next}, requests: []};
}

/** What the attached run's session reports and folds; nothing once the link is gone. */
function retryLoad(state: RunPageState): RunPageStep {
  if (state.record.kind !== 'ended' && state.record.kind !== 'historical')
    return {state, requests: []};
  if (state.summary.kind === 'ready' && state.history.kind === 'failed')
    return startReading({...state, history: {kind: 'idle'}});
  if (
    state.summary.kind === 'failed' ||
    state.summary.kind === 'unsupported' ||
    state.history.kind === 'failed' ||
    state.history.kind === 'unsupported'
  )
    return startSummary(
      {...state, summary: {kind: 'idle'}, history: {kind: 'idle'}},
      state.record.kind === 'ended' ? state.record.run.phase : (state.record.status ?? 'stopped'),
    );
  return {state, requests: []};
}

function liveStep(state: RunPageState, action: RunPageAction): RunPageStep | null {
  const attached = state.link.kind === 'attached';
  switch (action.type) {
    case 'attached':
      return withAttached(state, action.token, action.error);
    case 'session':
      return {state: attached ? {...state, session: action.session} : state, requests: []};
    case 'run-core':
      return {
        state: attached ? {...state, run: {...state.run, core: action.core}} : state,
        requests: [],
      };
    case 'run-data':
      return {
        state: attached
          ? {
              ...state,
              run: {
                ...state.run,
                trace: traceStep(state.run.trace, action.data),
                heard: heardAfter(state.run.heard, action.data, action.wallMs),
              },
            }
          : state,
        requests: [],
      };
    case 'tick':
      return {state: {...state, clockMs: action.wallMs}, requests: []};
    case 'command-refused':
      return {state: {...state, notice: action.message}, requests: []};
    case 'view':
      return {state: {...state, view: action.view}, requests: []};
    case 'history':
      return withHistory(state, action.token, action.reply);
    case 'trace-page':
      return withTracePage(state, action.token, action.reply);
    case 'summary':
      return withSummary(state, action.token, action.reply, action.wallMs);
    case 'retry-load':
      return retryLoad(state);
    case 'need-history':
      return withNeedHistory(state);
    case 'need-span':
      return withNeedSpan(state, action.spanId);
    case 'span':
      return withSpan(state, action.token, action.reply);
    default:
      return null;
  }
}

/** What must hold of the live link and the run data it brings. */
function liveProblems(state: RunPageState): string[] {
  const problems: string[] = [...traceProblems(state.run.trace)];
  const live = state.record.kind === 'live' ? state.record.run.id : null;
  const held = state.link.kind === 'none' ? null : state.link.instance;
  if (held !== live) problems.push(`the link holds ${held} while the live server is ${live}`);
  if (state.history.kind === 'reading' && !isEndedRecord(state.record)) {
    problems.push('history is read for a run that is not ended');
  }
  if (state.traceHistory.kind === 'reading' && !isEndedRecord(state.record)) {
    problems.push('the trace history is read for a run that is not ended');
  }
  if (state.traceHistory.kind === 'reading' && state.history.kind === 'reading') {
    problems.push('the trace history and the history are both being read');
  }
  if (state.session !== null && state.link.kind !== 'attached') {
    problems.push('a session reports without an attached run');
  }
  return problems;
}

/** What must hold in every state; empty when all do. */
export function runPageInvariants(state: RunPageState): string[] {
  const problems: string[] = liveProblems(state);
  if (state.pauseNow.kind !== 'idle' && state.record.kind !== 'live') {
    problems.push('a pending Pause now outlives the live run');
  }
  if (
    (state.logs.kind === 'listing' || state.logs.kind === 'listed') &&
    state.logs.instanceId !== recordInstance(state.record)
  ) {
    problems.push('log state belongs to a different run instance');
  }
  if (state.unavailable.logs !== undefined && state.logs.kind !== 'idle') {
    problems.push('logs are read although the app cannot read them');
  }
  if (state.summary.kind === 'gone' && !isEndedRecord(state.record)) {
    problems.push('a run that is not over is called gone');
  }
  if (state.locate.kind !== 'idle' && state.record.kind !== 'unlisted') {
    problems.push('the page waits to place a run its host lists');
  }
  return problems;
}
