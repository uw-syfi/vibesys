import type {
  DesignPatch,
  ProtocolResponse,
  RequestInput,
  RunEvent,
  ServerMessage,
  ServerTransport,
  StreamTransport,
} from '@vibesys/backend-client';
import {
  type CoreState,
  DEFAULT_CHAT_THREAD_ID,
  hasRunEnded,
  initialCoreState,
  reduceEvent,
  reduceEventBatch,
  reduceEventPrefix,
  reduceEventRebootstrap,
  reduceSnapshot,
} from '@vibesys/core-state';
import {isServerRejection, PersistentEventStream, ServerError} from './browser-entry.js';

export type WorkspaceClient = ServerTransport;

export interface BrowserLifecycle {
  readonly visibilityState: DocumentVisibilityState;
  readonly online: boolean;
  addEventListener(type: 'visibilitychange' | 'online', listener: () => void): void;
  removeEventListener(type: 'visibilitychange' | 'online', listener: () => void): void;
}

export interface QueryState {
  response: ProtocolResponse | null;
  loading: boolean;
  error: string | null;
}

export type QueryName = 'experiments' | 'design' | 'performance' | 'chat_options';
export type CommandAction = 'pause' | 'resume' | 'steer' | 'stop';
type Command = Extract<
  RequestInput,
  {type?: 'command.pause' | 'command.resume' | 'command.steer' | 'command.stop'}
>;

/**
 * A steer the backend acknowledged as pending: a client-side id that stays with it until it is
 * consumed, and the core sequence when it was sent (journal events after it may be its own).
 */
export interface SentSteer {
  id: string;
  text: string;
  afterSequence: number;
}

/**
 * A question sent to experiment chat. It leaves `asks` once its recorded `chat` event is captured;
 * `answer` is set only when the backend answered without recording (a thread that cannot answer
 * right now), `error` when the request failed.
 */
export interface SentAsk {
  id: string;
  threadId: string;
  text: string;
  /** The core sequence when it was sent: a recorded question after it may be this one. */
  afterSequence: number;
  answer: string | null;
  error: string | null;
}

export interface WorkspaceState {
  core: CoreState;
  runId: string | null;
  connection: 'connecting' | 'connected' | 'disconnected' | 'error';
  connectionError: string | null;
  /** The reconnect schedule is exhausted (or the boot dial failed): only `reconnect()` helps. */
  canRetry: boolean;
  snapshotError: string | null;
  queries: Record<QueryName, QueryState>;
  /** Enablement comes from `core.status`; this only guards double sends and keeps the last failure. */
  command: {sending: CommandAction | null; error: {action: CommandAction; message: string} | null};
  /** Steers acknowledged as pending, oldest first; the transcript shows them until consumed. */
  sent: readonly SentSteer[];
  /** Questions to experiment chat not yet seen recorded, oldest first. */
  asks: readonly SentAsk[];
  /** Events core-state drops or does not keep (see `CAPTURED_TYPES`), ascending by sequence. */
  captured: readonly RunEvent[];
  historyLoading: boolean;
  historyError: string | null;
}

export interface WorkspaceSessionOptions {
  reconnectDelaysMs?: readonly number[];
  /** Wakes a dropped stream when the page becomes visible or the network returns. */
  lifecycle?: BrowserLifecycle;
}

/**
 * Events core-state drops or does not keep, recorded before each fold: steers (`control`), judge
 * verdicts, round results, agent executions (prompts and results), chat answers, and the run's
 * start and end.
 */
export const CAPTURED_TYPES: ReadonlySet<string> = new Set([
  'control',
  'chat',
  'judge_result',
  'round_finished',
  'agent_execution_started',
  'agent_execution_finished',
  'invocation_started',
  'run_started',
  'run_finished',
  'run_failed',
  'run_interrupted',
  'configuration_failed',
]);
const RECONNECT_DELAYS_MS: readonly number[] = [500, 1_000, 2_000, 4_000, 8_000];
const emptyQuery = (): QueryState => ({response: null, loading: true, error: null});
const emptyQueries = (): WorkspaceState['queries'] => ({
  experiments: emptyQuery(),
  design: emptyQuery(),
  performance: emptyQuery(),
  // Asked for where it is shown (Ask, Notes, the palette), never per bootstrap.
  chat_options: {response: null, loading: false, error: null},
});
const idleCommand = (): WorkspaceState['command'] => ({sending: null, error: null});

/** Owns browser subscriptions and query state; core-state owns the event projection. */
export class WorkspaceSession {
  #state: WorkspaceState = {
    core: initialCoreState(),
    runId: null,
    connection: 'connecting',
    connectionError: null,
    canRetry: false,
    snapshotError: null,
    queries: emptyQueries(),
    command: idleCommand(),
    sent: [],
    asks: [],
    captured: [],
    historyLoading: false,
    historyError: null,
  };
  #listeners = new Set<() => void>();
  #stream: PersistentEventStream | null = null;
  #reconnecting = false;
  #closed = false;
  #declaredFloor: number | null = null;
  #historyGeneration = 0;
  #runGeneration = 0;
  #failedDials = 0;
  #spine = new Set<number>();
  #storeId = '';
  #sentCount = 0;
  #askCount = 0;
  #fetches = new Map<QueryName, Promise<void>>();
  #refreshPending = new Set<QueryName>();
  readonly #reconnectDelaysMs: readonly number[];
  readonly #lifecycle: BrowserLifecycle | null;

  constructor(
    private readonly client: WorkspaceClient,
    options: WorkspaceSessionOptions = {},
  ) {
    this.#reconnectDelaysMs = options.reconnectDelaysMs ?? RECONNECT_DELAYS_MS;
    this.#lifecycle = options.lifecycle ?? null;
    this.#lifecycle?.addEventListener('visibilitychange', this.#wake);
    this.#lifecycle?.addEventListener('online', this.#wake);
  }

  getSnapshot = (): WorkspaceState => this.#state;

  subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  };

  /** Subscribes; the bootstrap batch then triggers the one round of queries. */
  async start(): Promise<void> {
    await this.reconnect();
  }

  async reconnect(): Promise<void> {
    if (this.#closed || this.#reconnecting) return;
    this.#reconnecting = true;
    try {
      this.#failedDials = 0;
      this.#set({connection: 'connecting', connectionError: null, canRetry: false});
      const old = this.#stream;
      this.#stream = null;
      await old?.close();
      if (this.#closed) return;
      // Each new stream bootstraps; automatic reconnects resume its existing cursor.
      this.#declaredFloor = null;
      const transport: StreamTransport = {
        subscribe: async (after, onMessage, onDisconnect, options) => {
          try {
            return await this.client.subscribe(after, onMessage, onDisconnect, options);
          } catch (error) {
            // A server that refuses `tail` or `store_id` is retried without it in the same
            // attempt, so only other failures count against the reconnect schedule.
            const fallsBack =
              isServerRejection(error) &&
              (options?.tail !== undefined || options?.storeId !== undefined);
            if (!fallsBack && this.#stream === stream) {
              this.#failedDials += 1;
              if (this.#failedDials >= this.#reconnectDelaysMs.length) this.#set({canRetry: true});
            }
            throw error;
          }
        },
      };
      const stream = new PersistentEventStream(transport, {
        tail: 300,
        reconnectDelaysMs: this.#reconnectDelaysMs,
      });
      this.#stream = stream;
      await stream.subscribe({
        cursor: () => this.#state.core.sequence,
        storeId: () => this.#storeId,
        shouldReconnect: () =>
          !this.#closed && this.#state.connection !== 'error' && !hasRunEnded(this.#state.core),
        onMessage: (message, {resumed}) => {
          if (this.#stream === stream) this.#onMessage(message, resumed);
        },
        onConnectionState: state => {
          if (this.#stream !== stream || this.#state.connection === 'error') return;
          if (state.status === 'connected') this.#failedDials = 0;
          this.#set({
            connection: state.status,
            connectionError: state.status === 'disconnected' ? state.error.message : null,
            // A bootstrap dial reports its failure after the wrapper counted it, so derive, not reset.
            canRetry:
              state.status === 'disconnected' &&
              this.#failedDials >= this.#reconnectDelaysMs.length,
          });
        },
      });
      // A failed boot dial never arms the reconnect loop, so Retry is the only way on.
      if (this.#stream === stream && this.#state.connection === 'disconnected') {
        this.#set({canRetry: true});
      }
    } finally {
      this.#reconnecting = false;
    }
  }

  /** Snapshot, experiments, design, and performance: once per bootstrap batch, or from a Retry. */
  async refresh(): Promise<void> {
    await Promise.all([
      this.#snapshot(),
      this.load('experiments'),
      this.load('design'),
      this.load('performance'),
    ]);
  }

  async #snapshot(): Promise<void> {
    const generation = this.#runGeneration;
    try {
      const response = await this.client.request({type: 'query.snapshot'});
      if (generation !== this.#runGeneration) return;
      if (!response.snapshot) throw new Error('The backend returned no run snapshot.');
      // Subscription identity owns the event cursor. A query cannot switch it.
      if (this.#state.runId !== null && response.snapshot.run_id !== this.#state.runId) return;
      this.#set({
        core: reduceSnapshot(this.#state.core, response.snapshot),
        runId: response.snapshot.run_id,
        snapshotError: null,
      });
    } catch (error) {
      if (generation !== this.#runGeneration) return;
      this.#set({snapshotError: errorMessage(error)});
    }
  }

  load(name: QueryName): Promise<void> {
    const existing = this.#fetches.get(name);
    if (existing) {
      this.#refreshPending.add(name);
      return existing;
    }
    this.#query(name, {...this.#state.queries[name], loading: true, error: null});
    const generation = this.#runGeneration;
    const fetch = this.client
      .request({type: `query.${name}`})
      .then(
        response => {
          if (generation !== this.#runGeneration) return;
          this.#query(name, {response, loading: false, error: null});
        },
        error => {
          if (generation !== this.#runGeneration) return;
          this.#query(name, {
            ...this.#state.queries[name],
            loading: false,
            error: errorMessage(error),
          });
        },
      )
      .finally(() => {
        if (generation !== this.#runGeneration) return;
        this.#fetches.delete(name);
        if (this.#refreshPending.delete(name) && !this.#closed) void this.load(name);
      });
    this.#fetches.set(name, fetch);
    return fetch;
  }

  async command(input: Command): Promise<boolean> {
    if (this.#state.command.sending !== null || this.#state.connection !== 'connected')
      return false;
    const action = commandAction(input);
    const generation = this.#runGeneration;
    const afterSequence = this.#state.core.sequence;
    this.#set({command: {sending: action, error: null}});
    try {
      const response = await this.client.request(input);
      if (generation !== this.#runGeneration) return false;
      if (!response.ack) throw new Error('The backend returned no command acknowledgment.');
      // An acknowledgment is not a lifecycle transition. Only events/snapshots set status.
      const queued =
        input.type === 'command.steer' && response.ack.status === 'pending'
          ? {
              sent: [
                ...this.#state.sent,
                {id: `sent-${++this.#sentCount}`, text: input.text, afterSequence},
              ],
            }
          : {};
      this.#set({command: idleCommand(), ...queued});
      return true;
    } catch (error) {
      if (generation !== this.#runGeneration) return false;
      this.#set({command: {sending: null, error: {action, message: commandMessage(error)}}});
      return false;
    }
  }

  /** One file's patch for a round's commit range; null when the server is not attached to a run. */
  designPatch = async (base: string, head: string, path: string): Promise<DesignPatch | null> => {
    const response = await this.client.request({type: 'query.design_patch', base, head, path});
    return response.design_patch ?? null;
  };

  /**
   * Asks experiment chat on one thread; true once the question is on its way. The answer arrives
   * as the recorded `chat` event (captured from the response and deduplicated with the stream by
   * sequence). One question per thread at a time; a failed one does not hold its thread.
   */
  ask(text: string, threadId: string): boolean {
    const waiting = this.#state.asks.some(
      ask => ask.threadId === threadId && ask.answer === null && ask.error === null,
    );
    if (waiting || this.#state.connection !== 'connected') return false;
    const sent: SentAsk = {
      id: `ask-${++this.#askCount}`,
      threadId,
      text,
      afterSequence: this.#state.core.sequence,
      answer: null,
      error: null,
    };
    this.#set({asks: [...this.#state.asks, sent]});
    void this.#answer(sent, this.#runGeneration);
    return true;
  }

  /** Creates a chat thread on `selection`, or on the run's own agent; resolves its id. */
  async createThread(selection: {provider: string; model: string} | null): Promise<string> {
    const response = await this.client.request({
      type: 'query.chat_thread_create',
      ...(selection ?? {}),
    });
    const id = response.chat_thread?.thread_id;
    if (id === undefined) throw new Error('The backend returned no chat thread.');
    return id;
  }

  async #answer(sent: SentAsk, generation: number): Promise<void> {
    try {
      const response = await this.client.request({
        type: 'query.chat',
        text: sent.text,
        ...(sent.threadId === DEFAULT_CHAT_THREAD_ID ? {} : {thread_id: sent.threadId}),
      });
      if (generation !== this.#runGeneration) return;
      const events = response.events ?? [];
      if (events.some(event => event.type === 'chat')) {
        this.#set({
          captured: capture(this.#state.captured, events),
          asks: this.#state.asks.filter(ask => ask.id !== sent.id),
        });
      } else {
        this.#settleAsk(sent.id, {answer: response.chat?.answer ?? 'No answer was returned.'});
      }
    } catch (error) {
      if (generation !== this.#runGeneration) return;
      // The stream delivered the recorded answer before the connection dropped: the thread shows
      // it, so the question is answered, not failed (a failed one goes back into the composer).
      if (recordedAfter(this.#state.captured, sent)) {
        this.#set({asks: this.#state.asks.filter(ask => ask.id !== sent.id)});
      } else {
        this.#settleAsk(sent.id, {error: commandMessage(error)});
      }
    }
  }

  #settleAsk(id: string, patch: Pick<SentAsk, 'answer'> | Pick<SentAsk, 'error'>): void {
    this.#set({asks: this.#state.asks.map(ask => (ask.id === id ? {...ask, ...patch} : ask))});
  }

  async loadOlder(): Promise<void> {
    const floor = this.#state.core.historyAfterSequence;
    if (floor === 0 || this.#state.historyLoading) return;
    const generation = this.#historyGeneration;
    const nextFloor = Math.max(0, floor - 500);
    this.#set({historyLoading: true, historyError: null});
    try {
      const response = await this.client.request({
        type: 'query.events',
        after_sequence: nextFloor,
        before_sequence: floor + 1,
      });
      if (generation !== this.#historyGeneration) return;
      const events = (response.events ?? []).filter(
        event => event.sequence === undefined || !this.#spine.has(event.sequence),
      );
      this.#set({
        captured: capture(this.#state.captured, events),
        core: reduceEventPrefix(this.#state.core, events, nextFloor),
      });
    } catch (error) {
      if (generation !== this.#historyGeneration) return;
      this.#set({historyError: errorMessage(error)});
    } finally {
      if (generation === this.#historyGeneration) this.#set({historyLoading: false});
    }
  }

  async close(): Promise<void> {
    this.#closed = true;
    this.#lifecycle?.removeEventListener('visibilitychange', this.#wake);
    this.#lifecycle?.removeEventListener('online', this.#wake);
    this.#listeners.clear();
    await this.#stream?.close();
    await this.client.close();
  }

  #onMessage(message: ServerMessage, resumed: boolean): void {
    if (message.type === 'subscribed') {
      this.#onSubscribed(message, resumed);
    } else if (message.type === 'protocol_error') {
      this.#set({connection: 'error', connectionError: `${message.code}: ${message.message}`});
    } else if (message.type === 'event') {
      this.#set({
        captured: capture(this.#state.captured, [message.event]),
        core: reduceEvent(this.#state.core, message.event),
      });
      this.#invalidate([message.event]);
    } else if (message.type === 'event_batch') {
      this.#onEventBatch(message, resumed);
    }
  }

  #onSubscribed(message: Extract<ServerMessage, {type?: 'subscribed'}>, resumed: boolean): void {
    const changed = this.#state.runId !== null && this.#state.runId !== message.run_id;
    if (changed) {
      this.#runGeneration += 1;
      this.#historyGeneration += 1;
      this.#declaredFloor = null;
      this.#spine.clear();
      this.#fetches.clear();
      this.#refreshPending.clear();
      this.#set({
        runId: message.run_id,
        core: initialCoreState(),
        captured: [],
        snapshotError: null,
        queries: emptyQueries(),
        command: idleCommand(),
        sent: [],
        asks: [],
        historyLoading: false,
        historyError: null,
      });
      // A resume requested the old run's cursor and can omit the entire new run.
      // Replacing the stream also rejects remaining messages from that dial; its
      // bootstrap batch then runs the queries.
      if (resumed) {
        void this.reconnect();
        return;
      }
    }
    this.#set({runId: message.run_id, connection: 'connected', connectionError: null});
  }

  #onEventBatch(message: Extract<ServerMessage, {type?: 'event_batch'}>, resumed: boolean): void {
    const declared = message.history_after_sequence ?? 0;
    const storeId = message.store_id ?? '';
    // A resume that lands on a replaced store carries that store's full replay.
    const replaced = this.#storeId !== '' && storeId !== '' && storeId !== this.#storeId;
    if (storeId !== '') this.#storeId = storeId;
    const reset =
      replaced || (!resumed && (this.#declaredFloor === null || declared > this.#declaredFloor));
    let floor = this.#state.core.historyAfterSequence;
    if (!resumed || replaced) {
      floor = this.#reconcileHistoryFloor(message.events, declared, reset);
    }
    const fold = reset ? reduceEventRebootstrap : reduceEventBatch;
    this.#set({
      // core-state drops control events, so record them before folding.
      captured: capture(reset ? [] : this.#state.captured, message.events),
      core: fold(
        this.#state.core,
        message.events,
        message.active_executions,
        message.through_sequence,
        floor,
      ),
      // The bump above orphans an in-flight chunk, whose `finally` then skips the unlock.
      ...(reset ? {historyLoading: false, historyError: null} : {}),
    });
    // A bootstrap is the one time every query runs; its replayed invalidations are history.
    if (reset) void this.refresh();
    else this.#invalidate(message.events);
  }

  #reconcileHistoryFloor(events: readonly RunEvent[], declared: number, reset: boolean): number {
    this.#declaredFloor = declared;
    const floor = reset ? declared : Math.min(this.#state.core.historyAfterSequence, declared);
    if (reset) {
      this.#historyGeneration += 1;
      this.#spine.clear();
    }
    for (const event of events) {
      if (event.sequence !== undefined && event.sequence <= declared)
        this.#spine.add(event.sequence);
    }
    return floor;
  }

  #wake = (): void => {
    const lifecycle = this.#lifecycle;
    if (lifecycle === null || lifecycle.visibilityState === 'hidden' || !lifecycle.online) return;
    if (this.#state.connection === 'disconnected') this.#stream?.retry();
  };

  #invalidate(events: readonly RunEvent[]): void {
    if (!events.some(event => event.type === 'experiments_changed')) return;
    void this.load('experiments');
    void this.load('design');
    void this.load('performance');
  }

  #query(name: QueryName, query: QueryState): void {
    this.#set({queries: {...this.#state.queries, [name]: query}});
  }

  #set(patch: Partial<WorkspaceState>): void {
    if (this.#closed) return;
    this.#state = {...this.#state, ...patch};
    for (const listener of this.#listeners) listener();
  }
}

export function webSocketUrlFromLocation(location: Pick<Location, 'href'>): string {
  const page = new URL(location.href);
  const gateway = page.searchParams.get('gateway');
  const url = gateway === null ? page : new URL(gateway, page.origin);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  url.pathname = '/ws';
  const token = url.searchParams.get('token') ?? page.searchParams.get('token') ?? '';
  url.search = new URLSearchParams({token}).toString();
  return url.toString();
}

export const browserLifecycle: BrowserLifecycle = {
  get visibilityState() {
    return document.visibilityState;
  },
  get online() {
    return navigator.onLine;
  },
  addEventListener(type, listener) {
    window.addEventListener(type, listener);
  },
  removeEventListener(type, listener) {
    window.removeEventListener(type, listener);
  },
};

/** A `chat` event recorded for `sent`: same thread and text, after it was sent. */
function recordedAfter(captured: readonly RunEvent[], sent: SentAsk): boolean {
  return captured.some(
    event =>
      event.type === 'chat' &&
      event.text === sent.text &&
      (event.sequence ?? 0) > sent.afterSequence &&
      (event.chat_thread_id ?? DEFAULT_CHAT_THREAD_ID) === sent.threadId,
  );
}

/** Adds captured event types not yet held, keeping sequence order. Returns `held` when nothing is new. */
function capture(held: readonly RunEvent[], events: readonly RunEvent[]): readonly RunEvent[] {
  const seen = new Set(held.map(event => event.sequence));
  const added = events.filter(
    event =>
      CAPTURED_TYPES.has(event.type) && event.sequence !== undefined && !seen.has(event.sequence),
  );
  if (added.length === 0) return held;
  return [...held, ...added].sort((left, right) => (left.sequence ?? 0) - (right.sequence ?? 0));
}

function commandAction(input: Command): CommandAction {
  switch (input.type) {
    case 'command.pause':
      return 'pause';
    case 'command.resume':
      return 'resume';
    case 'command.stop':
      return 'stop';
    default:
      return 'steer';
  }
}

function commandMessage(error: unknown): string {
  return error instanceof ServerError
    ? (error.diagnostic?.summary ?? error.message)
    : errorMessage(error);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
