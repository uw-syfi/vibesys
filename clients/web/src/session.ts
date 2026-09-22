import {
  PersistentEventStream,
  type ProtocolResponse,
  type RequestInput,
  type RunEvent,
  ServerError,
  type ServerMessage,
  type StreamTransport,
} from '@vibesys/backend-client/browser';
import {
  type CoreState,
  hasRunEnded,
  initialCoreState,
  reduceEvent,
  reduceEventBatch,
  reduceEventPrefix,
  reduceEventRebootstrap,
  reduceSnapshot,
} from '@vibesys/core-state';

export interface WorkspaceClient extends StreamTransport {
  request(input: RequestInput): Promise<ProtocolResponse>;
  close(): Promise<void>;
}

export interface QueryState {
  response: ProtocolResponse | null;
  loading: boolean;
  error: string | null;
}

export type QueryName = 'experiments' | 'design' | 'performance';
export type CommandAction = 'pause' | 'resume' | 'steer';
type Command = Extract<RequestInput, {type?: 'command.pause' | 'command.resume' | 'command.steer'}>;

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
  command: {sending: boolean; error: {action: CommandAction; message: string} | null};
  /** Events core-state drops or does not keep (see `CAPTURED_TYPES`), ascending by sequence. */
  captured: readonly RunEvent[];
  historyLoading: boolean;
  historyError: string | null;
}

export interface WorkspaceSessionOptions {
  reconnectDelaysMs?: readonly number[];
}

/** Steers (`control`), judge verdicts, and the run's start and end, recorded before each fold. */
export const CAPTURED_TYPES: ReadonlySet<string> = new Set([
  'control',
  'judge_result',
  'run_started',
  'run_finished',
  'run_failed',
  'run_interrupted',
  'configuration_failed',
]);
const RECONNECT_DELAYS_MS: readonly number[] = [500, 1_000, 2_000, 4_000, 8_000];
const emptyQuery = (): QueryState => ({response: null, loading: true, error: null});
const idleCommand = (): WorkspaceState['command'] => ({sending: false, error: null});

/** Owns browser subscriptions and query state; core-state owns the event projection. */
export class WorkspaceSession {
  #state: WorkspaceState = {
    core: initialCoreState(),
    runId: null,
    connection: 'connecting',
    connectionError: null,
    canRetry: false,
    snapshotError: null,
    queries: {experiments: emptyQuery(), design: emptyQuery(), performance: emptyQuery()},
    command: idleCommand(),
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
  #fetches = new Map<QueryName, Promise<void>>();
  #refreshPending = new Set<QueryName>();
  readonly #reconnectDelaysMs: readonly number[];

  constructor(
    private readonly client: WorkspaceClient,
    options: WorkspaceSessionOptions = {},
  ) {
    this.#reconnectDelaysMs = options.reconnectDelaysMs ?? RECONNECT_DELAYS_MS;
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
            // A tail dial is the boot probe that falls back within the same attempt,
            // so only dials without `tail` count against the reconnect schedule.
            if (options?.tail === undefined && this.#stream === stream) {
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
          // Only `performance_context` (objective, baseline) is used; the series is dropped.
          const kept = name === 'performance' ? {...response, performance: []} : response;
          this.#query(name, {response: kept, loading: false, error: null});
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
    if (this.#state.command.sending || this.#state.connection !== 'connected') return false;
    const action: CommandAction =
      input.type === 'command.pause'
        ? 'pause'
        : input.type === 'command.resume'
          ? 'resume'
          : 'steer';
    const generation = this.#runGeneration;
    this.#set({command: {sending: true, error: null}});
    try {
      const response = await this.client.request(input);
      if (generation !== this.#runGeneration) return false;
      if (!response.ack) throw new Error('The backend returned no command acknowledgment.');
      // An acknowledgment is not a lifecycle transition. Only events/snapshots set status.
      this.#set({command: idleCommand()});
      return true;
    } catch (error) {
      if (generation !== this.#runGeneration) return false;
      this.#set({command: {sending: false, error: {action, message: commandMessage(error)}}});
      return false;
    }
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
    this.#listeners.clear();
    await this.#stream?.close();
    await this.client.close();
  }

  #onMessage(message: ServerMessage, resumed: boolean): void {
    if (message.type === 'subscribed') {
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
          queries: {experiments: emptyQuery(), design: emptyQuery(), performance: emptyQuery()},
          command: idleCommand(),
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
    } else if (message.type === 'protocol_error') {
      this.#set({connection: 'error', connectionError: `${message.code}: ${message.message}`});
    } else if (message.type === 'event') {
      this.#set({
        captured: capture(this.#state.captured, [message.event]),
        core: reduceEvent(this.#state.core, message.event),
      });
      this.#invalidate([message.event]);
    } else if (message.type === 'event_batch') {
      const declared = message.history_after_sequence ?? 0;
      const reset = !resumed && (this.#declaredFloor === null || declared > this.#declaredFloor);
      let floor = this.#state.core.historyAfterSequence;
      if (!resumed) {
        this.#declaredFloor = declared;
        floor = reset ? declared : Math.min(floor, declared);
        if (reset) {
          this.#historyGeneration += 1;
          this.#spine.clear();
        }
        for (const event of message.events) {
          if (event.sequence !== undefined && event.sequence <= declared)
            this.#spine.add(event.sequence);
        }
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
      });
      // A bootstrap is the one time every query runs; its replayed invalidations are history.
      if (reset) void this.refresh();
      else this.#invalidate(message.events);
    }
  }

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

function commandMessage(error: unknown): string {
  return error instanceof ServerError
    ? (error.diagnostic?.summary ?? error.message)
    : errorMessage(error);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
