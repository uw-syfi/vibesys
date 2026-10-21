/**
 * The Run screen's shell: its store over the ports the app provides. It performs the reducer's
 * requests (commands through the app's run-control paths, the force-stop bound on the injected
 * clock, the stream listing) and keeps one log follower per stream the screen needs
 * (`followedStreams`), feeding each follower's view back as an action. It attaches the live run
 * when the reducer asks, and keeps the run's session open while the run is attached
 * (`SessionHolder`). `dispose` stops every follower, closes the session, lets the run go and cancels
 * the bound; a reply that arrives later is dropped by its token.
 */

import type {RunStatus} from '@vibesys/contracts';
import {type LogListOutcome, type LogTransport, startLogFollower} from '@vibesys/logs';
import type {ScheduleTimeout} from '@vibesys/machine';
import {createStore, type Store} from '@vibesys/store';
import {
  followedStreams,
  type HistoryReply,
  type ReaderReply,
  type RunPageAction,
  type RunPageRequest,
  type RunPageSeed,
  type RunPageState,
  readerHeld,
  runPageInitial,
  runPageStep,
  type SpanDetailReply,
  type SummaryReply,
} from '../../state/index.js';
import {type LiveChat, type OpenSession, SessionHolder} from './live-session.js';

/** The run's logs as the app reads them. */
export interface RunPageLogs {
  list(instanceId: string): Promise<LogListOutcome>;
  readonly transport: LogTransport;
}

/** What the Run screen asks of the app. */
export interface RunPagePorts {
  /** Stop the run (`force`: without waiting for a safe point); resolves false when it could not. */
  stop(force: boolean): Promise<boolean>;
  /** Attach the live run's server `instance` to this window; resolves null, or why it could not. */
  attach(instance: string): Promise<string | null>;
  /** Let the attached run go. */
  release(): void;
  /**
   * Attach, for Chat, the server that serves the ended run: the host starts a read-only one on
   * demand. The reader's session opens through `openSession` like a live run's.
   */
  attachReader(): Promise<ReaderReply>;
  /** Open the attached run's session. */
  readonly openSession: OpenSession;
  /** One page of the ended run's recorded events from `from`. */
  readHistory(from: number, max: number): Promise<HistoryReply>;
  /**
   * One page of the ended run's recorded events from `from`, with only the events the trace folds
   * kept: the scrubber's earlier positions come from them (`trace-history.ts`), and the rest of a
   * journal (most of its bytes) is dropped as the page is read.
   */
  readTrace(from: number, max: number): Promise<HistoryReply>;
  /** The run's summary: its run level without its history. */
  readSummary(status: RunStatus): Promise<SummaryReply>;
  readSpan(spanId: string, from: number, max: number): Promise<SpanDetailReply>;
  /** Resume an ended run that its end record says can resume. */
  resumeEnded(fromRound: number | null): void;
  openWindow(): void;
  /** This window's clock, in epoch milliseconds. */
  now(): number;
  archive(): void;
  runAgain(): void;
  /** The run's log streams, when the app can read them (`unavailable.logs` says when not). */
  readonly logs: RunPageLogs | null;
}

export interface RunPageStore extends Store<RunPageState, RunPageAction> {
  /** The attached run's chat. */
  readonly chat: LiveChat;
  dispose(): void;
}

type Dispatch = (action: RunPageAction) => void;

/** One follower per stream `followedStreams` names, started and stopped as the state changes. */
class Followers {
  readonly #followers = new Map<string, {readonly instanceId: string; stop(): void}>();

  constructor(
    private readonly logs: RunPageLogs,
    private readonly schedule: ScheduleTimeout,
    private readonly dispatch: Dispatch,
  ) {}

  reconcile(state: RunPageState): void {
    const instanceId =
      state.record.kind === 'unlisted' || state.record.kind === 'historical'
        ? null
        : state.record.run.id;
    const wanted = new Set(instanceId === null ? [] : followedStreams(state));
    for (const [streamId, follower] of this.#followers) {
      if (wanted.has(streamId) && follower.instanceId === instanceId) continue;
      follower.stop();
      this.#followers.delete(streamId);
    }
    if (instanceId === null) return;
    for (const streamId of wanted) {
      if (!this.#followers.has(streamId)) this.#start(instanceId, streamId);
    }
  }

  stop(): void {
    for (const follower of this.#followers.values()) follower.stop();
    this.#followers.clear();
  }

  #start(instanceId: string, streamId: string): void {
    const actor = startLogFollower(this.logs.transport, this.schedule, {instanceId, streamId});
    let last = actor.getSnapshot().context.view;
    actor.subscribe(snapshot => {
      const {view} = snapshot.context;
      if (view === last) return;
      last = view;
      this.dispatch({type: 'log-view', streamId, view});
    });
    this.#followers.set(streamId, {instanceId, stop: () => actor.stop()});
  }
}

/** Tells the screen the run did not take `request`: a refusal (a message), or the command failing to reach it. */
function refusal(
  request: {readonly kind: 'pause'} | {readonly kind: 'resume'; readonly when: string},
  refused: unknown,
  dispatch: Dispatch,
): void {
  if (refused === null) return;
  const message = refused instanceof Error ? refused.message : String(refused);
  const waited = request.kind === 'resume' && request.when === 'capacity_returns';
  dispatch({type: waited ? 'wait-refused' : 'command-refused', message});
}

/** How often the clock ticks at the live end: a second, the finest the screen writes. */
const TICK_MS = 1000;

/**
 * Ticks this window's clock into the store while an attached run is at its live end, where the
 * run clock moves on screen, and not otherwise: a tick re-renders the screen, so it runs only
 * where time passing is on screen.
 */
class Ticker {
  #cancel: (() => void) | null = null;

  constructor(
    private readonly schedule: ScheduleTimeout,
    private readonly now: () => number,
    private readonly dispatch: Dispatch,
  ) {}

  reconcile(state: RunPageState): void {
    // The run clock is on screen at every level and face (the time bar, the hero, ages), so it
    // ticks whenever an attached run is at its live end.
    const wanted = state.link.kind === 'attached' && state.view.at === 'live';
    if (wanted && this.#cancel === null) this.#arm();
    if (!wanted) this.stop();
  }

  stop(): void {
    this.#cancel?.();
    this.#cancel = null;
  }

  #arm(): void {
    this.#cancel = this.schedule(() => {
      this.#cancel = null;
      this.dispatch({type: 'tick', wallMs: this.now()});
    }, TICK_MS);
  }
}

/** Read one history page; a read that throws settles as a failed page. */
function readHistory(
  ports: RunPagePorts,
  request: Extract<RunPageRequest, {readonly kind: 'read-history'}>,
  dispatch: Dispatch,
  schedule: ScheduleTimeout,
  cancels: Set<() => void>,
): void {
  const cancel = schedule(() => settle({kind: 'failed', message: 'The read timed out.'}), 30_000);
  cancels.add(cancel);
  let settled = false;
  const settle = (reply: HistoryReply): void => {
    if (settled) return;
    settled = true;
    cancel();
    cancels.delete(cancel);
    dispatch({type: 'history', token: request.token, reply});
  };
  void ports
    .readHistory(request.from, request.max)
    .then(settle, (error: unknown) => settle({kind: 'failed', message: String(error)}));
}

/** Read one page of the trace's earlier events; a read that throws settles as a failed page. */
function readTrace(
  ports: RunPagePorts,
  request: Extract<RunPageRequest, {readonly kind: 'read-trace'}>,
  dispatch: Dispatch,
  schedule: ScheduleTimeout,
  cancels: Set<() => void>,
): void {
  const cancel = schedule(() => settle({kind: 'failed', message: 'The read timed out.'}), 30_000);
  cancels.add(cancel);
  let settled = false;
  const settle = (reply: HistoryReply): void => {
    if (settled) return;
    settled = true;
    cancel();
    cancels.delete(cancel);
    dispatch({type: 'trace-page', token: request.token, reply});
  };
  void ports
    .readTrace(request.from, request.max)
    .then(settle, (error: unknown) => settle({kind: 'failed', message: String(error)}));
}

/** Read the run's summary; a read that throws settles as a failed one. */
function readSummary(
  ports: RunPagePorts,
  request: Extract<RunPageRequest, {readonly kind: 'read-summary'}>,
  dispatch: Dispatch,
  schedule: ScheduleTimeout,
  cancels: Set<() => void>,
): void {
  const cancel = schedule(() => settle({kind: 'failed', message: 'The read timed out.'}), 30_000);
  cancels.add(cancel);
  let settled = false;
  const settle = (reply: SummaryReply): void => {
    if (settled) return;
    settled = true;
    cancel();
    cancels.delete(cancel);
    dispatch({type: 'summary', token: request.token, reply, wallMs: ports.now()});
  };
  void ports
    .readSummary(request.status)
    .then(settle, (error: unknown) => settle({kind: 'failed', message: String(error)}));
}

function readSpan(
  ports: RunPagePorts,
  request: Extract<RunPageRequest, {readonly kind: 'read-span'}>,
  dispatch: Dispatch,
): void {
  const settle = (reply: SpanDetailReply): void =>
    dispatch({type: 'span', token: request.token, reply});
  void ports
    .readSpan(request.spanId, request.from, request.max)
    .then(settle, (error: unknown) => settle({kind: 'failed', message: String(error)}));
}

/** List the run's log streams; a listing that throws settles as a transport failure. */
function listLogs(
  ports: RunPagePorts,
  request: Extract<RunPageRequest, {readonly kind: 'list-logs'}>,
  dispatch: Dispatch,
): void {
  const settle = (outcome: LogListOutcome): void =>
    dispatch({type: 'streams', token: request.token, outcome});
  void ports.logs
    ?.list(request.instanceId)
    .then(settle, (error: unknown) =>
      settle({ok: false, failure: {code: 'transport', message: String(error)}}),
    );
}

function performCommand(ports: RunPagePorts, request: RunPageRequest, dispatch: Dispatch): void {
  switch (request.kind) {
    case 'read-span':
      readSpan(ports, request, dispatch);
      return;
    case 'resume-ended':
      ports.resumeEnded(request.fromRound);
      return;
    case 'open-window':
      ports.openWindow();
      return;
    case 'archive':
      ports.archive();
      return;
    case 'run-again':
      ports.runAgain();
      return;
    default:
      return;
  }
}

/** The timers armed by `arm-bound` requests, by the token each answers with; `live` holds them all. */
class Bounds {
  readonly #armed = new Map<number, () => void>();

  constructor(
    private readonly schedule: ScheduleTimeout,
    private readonly live: Set<() => void>,
  ) {}

  perform(
    request: Extract<RunPageRequest, {readonly kind: 'arm-bound' | 'cancel-bound'}>,
    dispatch: Dispatch,
  ): void {
    const {token} = request;
    const armed = this.#armed.get(token);
    if (armed !== undefined) this.#drop(token, armed);
    if (request.kind === 'cancel-bound') {
      armed?.();
      return;
    }
    const cancel = this.schedule(() => {
      this.#drop(token, cancel);
      dispatch({type: 'bound-elapsed', token});
    }, request.ms);
    this.#armed.set(token, cancel);
    this.live.add(cancel);
  }

  #drop(token: number, cancel: () => void): void {
    this.#armed.delete(token);
    this.live.delete(cancel);
  }
}

/** The holder of the live run's session. */
function liveHolder(ports: RunPagePorts, dispatch: Dispatch): SessionHolder {
  return new SessionHolder(ports.openSession, dispatch, ports.now, state =>
    state.link.kind === 'attached' ? state.link.instance : null,
  );
}

/** Attach the live run's server to this window and say how it went. */
function attachLive(
  ports: RunPagePorts,
  request: Extract<RunPageRequest, {readonly kind: 'attach'}>,
  dispatch: Dispatch,
): void {
  const settle = (error: string | null): void =>
    dispatch({type: 'attached', token: request.token, error});
  void ports.attach(request.instance).then(settle, (error: unknown) => settle(String(error)));
}

/**
 * The holder of the reader's session, which folds the ended run for Chat: what else it reports
 * (its stream, its data) the page already has from the run's history.
 */
function readerHolder(ports: RunPagePorts, dispatch: Dispatch): SessionHolder {
  return new SessionHolder(
    ports.openSession,
    action => {
      if (action.type === 'run-core') dispatch({type: 'reader-core', core: action.core});
    },
    ports.now,
    state => (state.reader.link.kind === 'attached' ? 'reader' : null),
  );
}

/** Chat's way to the run: a live run's session answers it, and for an ended run the reader's. */
function chatRoute(live: SessionHolder, reader: SessionHolder): LiveChat {
  const route = (): LiveChat => (live.isOpen() ? live.chat : reader.chat);
  return {
    ask: (input, signal) => route().ask(input, signal),
    createThread: (harness, signal) => route().createThread(harness, signal),
    control: request => route().control(request),
  };
}

/** Attach the ended run's reader and say how it went, or let it go with its session. */
function performReader(
  ports: RunPagePorts,
  sessions: SessionHolder,
  request: Extract<RunPageRequest, {readonly kind: 'attach-reader' | 'release-reader'}>,
  dispatch: Dispatch,
): void {
  if (request.kind === 'release-reader') {
    sessions.close();
    ports.release();
    return;
  }
  const settle = (reply: ReaderReply): void =>
    dispatch({type: 'reader-attached', token: request.token, reply});
  void ports
    .attachReader()
    .then(settle, (error: unknown) => settle({kind: 'failed', message: String(error)}));
}

/** Brings each reconciler in step with the state after every change. */
function reconcile(
  state: RunPageState,
  reconcilers: readonly (null | {reconcile(state: RunPageState): void})[],
): void {
  for (const each of reconcilers) each?.reconcile(state);
}

export function createRunPageStore(
  seed: RunPageSeed,
  ports: RunPagePorts,
  schedule: ScheduleTimeout,
): RunPageStore {
  const cancels = new Set<() => void>();
  const bounds = new Bounds(schedule, cancels);
  let disposed = false;
  // Reports reach the store only while it lives; `store` is assigned before any can arrive.
  const sessions = liveHolder(ports, action => store.dispatch(action));
  const readerSessions = readerHolder(ports, action => store.dispatch(action));
  const live =
    (dispatch: Dispatch): Dispatch =>
    action => {
      if (!disposed) dispatch(action);
    };
  const perform = (request: RunPageRequest, raw: Dispatch): void => {
    const dispatch = live(raw);
    if (disposed) return;
    switch (request.kind) {
      case 'stop':
        void ports.stop(request.force).then(
          stopped => (stopped ? undefined : dispatch({type: 'stop-failed'})),
          () => dispatch({type: 'stop-failed'}),
        );
        return;
      case 'arm-bound':
      case 'cancel-bound':
        bounds.perform(request, dispatch);
        return;
      case 'list-logs':
        listLogs(ports, request, dispatch);
        return;
      case 'attach':
        attachLive(ports, request, dispatch);
        return;
      case 'release':
        sessions.close();
        ports.release();
        return;
      case 'attach-reader':
      case 'release-reader':
        performReader(ports, readerSessions, request, dispatch);
        return;
      case 'read-history':
        readHistory(ports, request, dispatch, schedule, cancels);
        return;
      case 'read-trace':
        readTrace(ports, request, dispatch, schedule, cancels);
        return;
      case 'read-summary':
        readSummary(ports, request, dispatch, schedule, cancels);
        return;
      case 'pause':
      case 'resume':
        void sessions.command(request).then(
          refused => refusal(request, refused, dispatch),
          (error: unknown) => refusal(request, error, dispatch),
        );
        return;
      default:
        performCommand(ports, request, dispatch);
    }
  };
  const store = createStore<RunPageState, RunPageAction, RunPageRequest>(
    runPageStep,
    runPageInitial(seed),
    perform,
  );
  const ticker = new Ticker(schedule, ports.now, live(store.dispatch));
  const {logs} = ports;
  const followers = logs === null ? null : new Followers(logs, schedule, live(store.dispatch));
  store.dispatch({type: 'locate'}); // bounds the wait for the listing to place the run
  const unsubscribe = store.subscribe(() => {
    reconcile(store.getState(), [followers, sessions, readerSessions, ticker]);
  });
  return {
    ...store,
    chat: chatRoute(sessions, readerSessions),
    dispose: () => {
      disposed = true;
      unsubscribe();
      followers?.stop();
      sessions.close();
      readerSessions.close();
      ticker.stop();
      const final = store.getState();
      if (final.link.kind !== 'none' || readerHeld(final.reader)) ports.release();
      for (const cancel of cancels) cancel();
      cancels.clear();
    },
  };
}
